import hashlib
import io
import json
import os
import re
import string
import uuid
import zipfile
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, jsonify, render_template, request, send_file, session
from PIL import Image, ImageOps
import qrcode

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "photo-wall-admin-secret-key-2026")
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Constants
PHOTO_PREFIX = "photos/"
THUMB_PREFIX = "thumbs/"
ROOM_PREFIX = "rooms/"
THUMB_SIZE = 480
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "simha_admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Simha_Admin_Pass")

ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

KEY_REGEX = re.compile(r"^photos/[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$", re.IGNORECASE)

# Environment variables
BUCKET_NAME = os.getenv("BUCKET_NAME", "photo-wall-bucket")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
PUBLIC_URL = os.getenv("PUBLIC_URL")


def get_s3_client():
    """Return boto3 S3 client configured for regional SigV4 endpoint."""
    config = Config(
        region_name=AWS_REGION,
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    )
    return boto3.client("s3", region_name=AWS_REGION, config=config)


def sanitize_str(raw_str: Optional[str], max_len: int = 80) -> str:
    """Sanitize string for S3 metadata (ASCII printable, max_len chars)."""
    if not raw_str:
        return ""
    allowed = set(string.printable) - set("\r\n\t\x0b\x0c")
    cleaned = "".join(c for c in raw_str if c in allowed)
    return cleaned.strip()[:max_len]


def hash_password(password: str) -> str:
    """Return SHA-256 hash of room password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def is_admin():
    """Check if current session is authenticated as admin."""
    return session.get("admin_logged_in") is True


@app.route("/")
def index():
    """Wall page."""
    return render_template("wall.html")


@app.route("/upload")
def upload():
    """Phone upload page."""
    return render_template("upload.html")


@app.route("/admin")
def admin_page():
    """Admin dashboard page."""
    return render_template("admin.html")


@app.route("/healthz")
def healthz():
    """Health check endpoint for ALB."""
    return jsonify({"ok": True})


@app.route("/qr.png")
def qr_code():
    """Generate QR code PNG pointing at PUBLIC_URL/upload or host/upload."""
    base_url = PUBLIC_URL
    if not base_url:
        base_url = request.host_url.rstrip("/")

    upload_url = f"{base_url}/upload"

    img = qrcode.make(upload_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return send_file(buf, mimetype="image/png")


# Admin Auth API
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    """Authenticate admin credentials."""
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        return jsonify({"ok": True, "message": "Admin login successful"})

    return jsonify({"error": "Invalid admin username or password"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    """Clear admin session."""
    session.pop("admin_logged_in", None)
    return jsonify({"ok": True})


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    """Get admin analytics stats & list of all photos."""
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    s3 = get_s3_client()
    try:
        resp = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=THUMB_PREFIX)
        room_resp = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=ROOM_PREFIX)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    contents = resp.get("Contents", [])
    rooms = room_resp.get("Contents", [])

    total_likes = 0
    total_size = 0
    photos_list = []

    for item in contents:
        thumb_key = item["Key"]
        photo_id = thumb_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        size = item.get("Size", 0)
        total_size += size

        orig_key = None
        orig_head = None
        for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
            test_key = f"{PHOTO_PREFIX}{photo_id}.{ext}"
            try:
                head_res = s3.head_object(Bucket=BUCKET_NAME, Key=test_key)
                orig_key = test_key
                orig_head = head_res
                total_size += head_res.get("ContentLength", 0)
                break
            except ClientError:
                continue

        if not orig_key or not orig_head:
            continue

        meta = orig_head.get("Metadata", {})
        likes = int(meta.get("likes", "0"))
        total_likes += likes

        try:
            thumb_url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": BUCKET_NAME, "Key": thumb_key},
                ExpiresIn=3600,
            )
            full_url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": BUCKET_NAME, "Key": orig_key},
                ExpiresIn=3600,
            )
        except ClientError:
            continue

        photos_list.append({
            "id": photo_id,
            "thumb": thumb_url,
            "full": full_url,
            "caption": meta.get("caption", ""),
            "location": meta.get("location", ""),
            "roomId": meta.get("roomid", "public"),
            "likes": likes,
            "lastModified": item["LastModified"].isoformat(),
        })

    return jsonify({
        "ok": True,
        "totalPhotos": len(photos_list),
        "totalRooms": len(rooms),
        "totalLikes": total_likes,
        "totalStorageMB": round(total_size / (1024 * 1024), 2),
        "photos": photos_list,
    })


@app.route("/api/admin/delete-photo", methods=["POST"])
def admin_delete_photo():
    """Admin-only instant batch deletion of a photo from S3."""
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    photo_id = data.get("photoId", "").strip()

    if not photo_id:
        return jsonify({"error": "Missing photoId"}), 400

    s3 = get_s3_client()
    delete_keys = [{"Key": f"{THUMB_PREFIX}{photo_id}.jpg"}]
    for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
        delete_keys.append({"Key": f"{PHOTO_PREFIX}{photo_id}.{ext}"})

    try:
        s3.delete_objects(
            Bucket=BUCKET_NAME,
            Delete={"Objects": delete_keys, "Quiet": True},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "message": f"Photo {photo_id} deleted successfully"})


@app.route("/api/admin/delete-batch", methods=["POST"])
def admin_delete_batch():
    """Admin-only chunked bulk deletion of multiple selected photos from S3."""
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    photo_ids = data.get("photoIds", [])

    if not photo_ids:
        return jsonify({"error": "No photoIds provided"}), 400

    s3 = get_s3_client()
    delete_keys = []
    for photo_id in photo_ids:
        delete_keys.append({"Key": f"{THUMB_PREFIX}{photo_id}.jpg"})
        for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
            delete_keys.append({"Key": f"{PHOTO_PREFIX}{photo_id}.{ext}"})

    # Chunk into 1000 items max per S3 request
    for i in range(0, len(delete_keys), 1000):
        chunk = delete_keys[i : i + 1000]
        try:
            s3.delete_objects(
                Bucket=BUCKET_NAME,
                Delete={"Objects": chunk, "Quiet": True},
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "count": len(photo_ids)})


@app.route("/api/presign", methods=["POST"])
def presign():
    """Generate presigned PUT URL for direct-to-S3 upload."""
    data = request.get_json() or {}
    content_type = data.get("contentType", "").lower()
    raw_caption = data.get("caption", "")
    raw_location = data.get("location", "")
    room_id = sanitize_str(data.get("roomId", "public"), max_len=40) or "public"

    if content_type not in ALLOWED_TYPES:
        return jsonify({"error": f"Disallowed content type: {content_type}"}), 400

    ext = ALLOWED_TYPES[content_type]
    key = f"{PHOTO_PREFIX}{uuid.uuid4().hex}.{ext}"
    caption = sanitize_str(raw_caption, max_len=80)
    location = sanitize_str(raw_location, max_len=60)

    s3 = get_s3_client()
    metadata = {
        "caption": caption,
        "location": location,
        "roomid": room_id,
        "likes": "0",
    }

    params = {
        "Bucket": BUCKET_NAME,
        "Key": key,
        "ContentType": content_type,
        "Metadata": metadata,
    }

    headers = {
        "Content-Type": content_type,
        "x-amz-meta-caption": caption,
        "x-amz-meta-location": location,
        "x-amz-meta-roomid": room_id,
        "x-amz-meta-likes": "0",
    }

    try:
        presigned_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params=params,
            ExpiresIn=300,
        )
    except ClientError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "url": presigned_url,
        "key": key,
        "caption": caption,
        "location": location,
        "roomId": room_id,
        "headers": headers,
    })


@app.route("/api/complete", methods=["POST"])
def complete():
    """Validate upload key and size, then generate 480px JPEG thumbnail in S3."""
    data = request.get_json() or {}
    key = data.get("key", "")

    if not KEY_REGEX.match(key):
        return jsonify({"error": "Invalid key format"}), 400

    s3 = get_s3_client()

    try:
        head_resp = s3.head_object(Bucket=BUCKET_NAME, Key=key)
    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code")
        if err_code in ("404", "NoSuchKey"):
            return jsonify({"error": "Object not found in S3"}), 400
        return jsonify({"error": str(e)}), 500

    content_length = head_resp.get("ContentLength", 0)
    if content_length > MAX_UPLOAD_BYTES:
        try:
            s3.delete_object(Bucket=BUCKET_NAME, Key=key)
        except ClientError:
            pass
        return jsonify({"error": "File size exceeds 15MB limit"}), 400

    try:
        obj_resp = s3.get_object(Bucket=BUCKET_NAME, Key=key)
        body = obj_resp["Body"].read()
    except ClientError as e:
        return jsonify({"error": f"Failed to download original photo: {str(e)}"}), 500

    try:
        img = Image.open(io.BytesIO(body))
        img = ImageOps.exif_transpose(img)

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)

        thumb_buf = io.BytesIO()
        img.save(thumb_buf, format="JPEG", quality=85)
        thumb_bytes = thumb_buf.getvalue()
    except Exception as e:
        return jsonify({"error": f"Failed to process thumbnail: {str(e)}"}), 500

    filename = key.rsplit("/", 1)[-1]
    photo_id = filename.rsplit(".", 1)[0]
    thumb_key = f"{THUMB_PREFIX}{photo_id}.jpg"

    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=thumb_key,
            Body=thumb_bytes,
            ContentType="image/jpeg",
        )
    except ClientError as e:
        return jsonify({"error": f"Failed to upload thumbnail: {str(e)}"}), 500

    return jsonify({"ok": True})


@app.route("/api/photos", methods=["GET"])
def photos():
    """List photos newest-first, capped at 200, with location, likes & room filtering."""
    target_room = request.args.get("roomId", "public")
    target_location = request.args.get("location", "").strip().lower()

    try:
        s3 = get_s3_client()
        resp = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=THUMB_PREFIX)
    except Exception as e:
        return jsonify({"photos": [], "warning": str(e)})

    contents = resp.get("Contents", [])
    if not contents:
        return jsonify({"photos": []})

    contents.sort(key=lambda x: x["LastModified"], reverse=True)
    contents = contents[:200]

    result = []
    for item in contents:
        thumb_key = item["Key"]
        thumb_filename = thumb_key.rsplit("/", 1)[-1]
        photo_id = thumb_filename.rsplit(".", 1)[0]

        orig_key = None
        orig_head = None
        for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
            test_key = f"{PHOTO_PREFIX}{photo_id}.{ext}"
            try:
                head_res = s3.head_object(Bucket=BUCKET_NAME, Key=test_key)
                orig_key = test_key
                orig_head = head_res
                break
            except ClientError:
                continue

        if not orig_key or not orig_head:
            continue

        metadata = orig_head.get("Metadata", {})
        caption = metadata.get("caption", "")
        location = metadata.get("location", "")
        room_id = metadata.get("roomid", "public")
        likes = int(metadata.get("likes", "0"))

        if target_room and room_id != target_room:
            continue

        if target_location and target_location not in location.lower():
            continue

        try:
            thumb_url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": BUCKET_NAME, "Key": thumb_key},
                ExpiresIn=3600,
            )
            full_url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": BUCKET_NAME, "Key": orig_key},
                ExpiresIn=3600,
            )
        except ClientError:
            continue

        result.append({
            "id": photo_id,
            "thumb": thumb_url,
            "full": full_url,
            "caption": caption,
            "location": location,
            "roomId": room_id,
            "likes": likes,
        })

    return jsonify({"photos": result})


@app.route("/api/photos/<photo_id>/like", methods=["POST"])
def like_photo(photo_id):
    """Increment like count for a photo in S3 metadata."""
    s3 = get_s3_client()
    orig_key = None
    orig_head = None

    for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
        test_key = f"{PHOTO_PREFIX}{photo_id}.{ext}"
        try:
            orig_head = s3.head_object(Bucket=BUCKET_NAME, Key=test_key)
            orig_key = test_key
            break
        except ClientError:
            continue

    if not orig_key or not orig_head:
        return jsonify({"error": "Photo not found"}), 404

    metadata = orig_head.get("Metadata", {})
    current_likes = int(metadata.get("likes", "0"))
    new_likes = current_likes + 1
    metadata["likes"] = str(new_likes)

    try:
        s3.copy_object(
            Bucket=BUCKET_NAME,
            Key=orig_key,
            CopySource={"Bucket": BUCKET_NAME, "Key": orig_key},
            Metadata=metadata,
            MetadataDirective="REPLACE",
            ContentType=orig_head.get("ContentType", "image/jpeg"),
        )
    except ClientError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "likes": new_likes})


@app.route("/api/rooms/create", methods=["POST"])
def create_room():
    """Create a password-protected private folder/room."""
    data = request.get_json() or {}
    room_id = sanitize_str(data.get("roomId", ""), max_len=40).lower()
    name = sanitize_str(data.get("name", ""), max_len=60)
    password = data.get("password", "").strip()

    if not room_id or not re.match(r"^[a-z0-9\-]+$", room_id):
        return jsonify({"error": "Room ID must be alphanumeric and hyphens only"}), 400

    if not password or len(password) < 3:
        return jsonify({"error": "Password must be at least 3 characters"}), 400

    s3 = get_s3_client()
    room_key = f"{ROOM_PREFIX}{room_id}.json"

    room_data = {
        "roomId": room_id,
        "name": name or room_id,
        "passwordHash": hash_password(password),
    }

    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=room_key,
            Body=json.dumps(room_data),
            ContentType="application/json",
        )
    except ClientError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "roomId": room_id, "name": name})


@app.route("/api/rooms/auth", methods=["POST"])
def auth_room():
    """Authenticate room password."""
    data = request.get_json() or {}
    room_id = sanitize_str(data.get("roomId", ""), max_len=40).lower()
    password = data.get("password", "").strip()

    if room_id == "public":
        return jsonify({"ok": True, "authenticated": True})

    s3 = get_s3_client()
    room_key = f"{ROOM_PREFIX}{room_id}.json"

    try:
        res = s3.get_object(Bucket=BUCKET_NAME, Key=room_key)
        room_info = json.loads(res["Body"].read().decode("utf-8"))
    except Exception:
        return jsonify({"error": "Room not found"}), 404

    if room_info.get("passwordHash") == hash_password(password):
        return jsonify({"ok": True, "authenticated": True, "name": room_info.get("name")})

    return jsonify({"error": "Invalid room password"}), 401


@app.route("/api/download-all", methods=["GET"])
def download_all():
    """Package original photos into a downloadable Zip archive."""
    target_room = request.args.get("roomId", "public")
    s3 = get_s3_client()

    try:
        resp = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=PHOTO_PREFIX)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    contents = resp.get("Contents", [])
    zip_buf = io.BytesIO()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for item in contents:
            key = item["Key"]
            if key == PHOTO_PREFIX:
                continue

            try:
                head = s3.head_object(Bucket=BUCKET_NAME, Key=key)
                room_id = head.get("Metadata", {}).get("roomid", "public")
                if target_room and room_id != target_room:
                    continue

                obj = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                filename = key.rsplit("/", 1)[-1]
                zip_file.writestr(filename, obj["Body"].read())
            except Exception:
                continue

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"photo-wall-{target_room}.zip",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
