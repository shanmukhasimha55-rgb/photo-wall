import io
import os
import boto3
import pytest
from PIL import Image
from moto import mock_aws

import app as photo_wall_app

TEST_BUCKET = "photo-wall-test-bucket"


@pytest.fixture
def client():
    photo_wall_app.app.config["TESTING"] = True
    photo_wall_app.BUCKET_NAME = TEST_BUCKET
    with photo_wall_app.app.test_client() as client:
        yield client


@pytest.fixture
def s3_bucket():
    with mock_aws():
        s3 = boto3.client("s3", region_name=photo_wall_app.AWS_REGION)
        s3.create_bucket(
            Bucket=TEST_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": photo_wall_app.AWS_REGION},
        )
        yield s3


def create_dummy_jpeg_bytes(width=200, height=200):
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True}


def test_pages(client):
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert b"Photo Wall" in res_index.data

    res_upload = client.get("/upload")
    assert res_upload.status_code == 200
    assert b"Share Your Photos" in res_upload.data

    res_admin = client.get("/admin")
    assert res_admin.status_code == 200
    assert b"Admin Dashboard" in res_admin.data


def test_qr_code(client):
    res = client.get("/qr.png")
    assert res.status_code == 200
    assert res.content_type == "image/png"


def test_presign_with_location_and_room(client, s3_bucket):
    res = client.post(
        "/api/presign",
        json={
            "contentType": "image/jpeg",
            "caption": "Awesome party!",
            "location": "Paris, France",
            "roomId": "my-vip-room",
        },
    )
    assert res.status_code == 200
    data = res.get_json()

    assert "url" in data
    assert data["key"].startswith("photos/")
    assert data["caption"] == "Awesome party!"
    assert data["location"] == "Paris, France"
    assert data["roomId"] == "my-vip-room"


def test_like_photo(client, s3_bucket):
    key = "photos/0123456789abcdef0123456789abcdef.jpg"
    jpeg_bytes = create_dummy_jpeg_bytes()
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key=key,
        Body=jpeg_bytes,
        Metadata={"caption": "Test Photo", "likes": "2"},
    )

    res = client.post("/api/photos/0123456789abcdef0123456789abcdef/like")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "likes": 3}


def test_admin_login_and_delete(client, s3_bucket):
    # 1. Access stats unauthorized
    unauth_res = client.get("/api/admin/stats")
    assert unauth_res.status_code == 401

    # 2. Login invalid creds
    bad_login = client.post(
        "/api/admin/login",
        json={"username": "wrong", "password": "bad"},
    )
    assert bad_login.status_code == 401

    # 3. Login valid creds
    good_login = client.post(
        "/api/admin/login",
        json={"username": "simha_admin", "password": "Simha_Admin_Pass"},
    )
    assert good_login.status_code == 200

    # 4. Upload photo to S3
    photo_id = "11112222333344445555666677778888"
    orig_key = f"photos/{photo_id}.jpg"
    thumb_key = f"thumbs/{photo_id}.jpg"
    jpeg_bytes = create_dummy_jpeg_bytes()
    s3_bucket.put_object(Bucket=TEST_BUCKET, Key=orig_key, Body=jpeg_bytes)
    s3_bucket.put_object(Bucket=TEST_BUCKET, Key=thumb_key, Body=jpeg_bytes)

    # 5. Access stats authorized
    stats_res = client.get("/api/admin/stats")
    assert stats_res.status_code == 200
    assert stats_res.get_json()["totalPhotos"] == 1

    # 6. Admin delete photo
    del_res = client.post("/api/admin/delete-photo", json={"photoId": photo_id})
    assert del_res.status_code == 200
    assert del_res.get_json()["ok"] is True

    # 7. Verify deleted from S3
    with pytest.raises(Exception):
        s3_bucket.head_object(Bucket=TEST_BUCKET, Key=thumb_key)
