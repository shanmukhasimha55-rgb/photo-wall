import json
import io
import os
import boto3
from PIL import Image, ImageOps

s3 = boto3.client('s3')
rekognition = boto3.client('rekognition')

THUMB_SIZE = 480
THUMB_PREFIX = "thumbs/"
PHOTO_PREFIX = "photos/"

def lambda_handler(event, context):
    """
    AWS Lambda S3-triggered event handler:
    1. Triggers when an image is uploaded to S3 (photos/ prefix).
    2. Downloads original photo from S3.
    3. Analyzes safety and auto-tags with Amazon Rekognition.
    4. Generates a 480px JPEG thumbnail and uploads to thumbs/ prefix.
    """
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']
        
        # Only process original photos
        if not key.startswith(PHOTO_PREFIX):
            continue
            
        print(f"Processing image {key} from bucket {bucket}")
        
        # 1. Download image
        response = s3.get_object(Bucket=bucket, Key=key)
        image_bytes = response['Body'].read()
        content_type = response.get('ContentType', 'image/jpeg')
        metadata = response.get('Metadata', {})
        
        # 2. Rekognition Auto-Moderation & Label Detection
        ai_tags = []
        is_flagged = False
        mod_reason = ""
        
        try:
            mod_resp = rekognition.detect_moderation_labels(
                Image={'Bytes': image_bytes},
                MinConfidence=60.0
            )
            mod_items = mod_resp.get('ModerationLabels', [])
            if mod_items:
                is_flagged = True
                mod_reason = ",".join([m['Name'] for m in mod_items[:5]])
        except Exception as e:
            print(f"Rekognition moderation check failed: {e}")
            
        try:
            label_resp = rekognition.detect_labels(
                Image={'Bytes': image_bytes},
                MaxLabels=6,
                MinConfidence=70.0
            )
            ai_tags = [label['Name'] for label in label_resp.get('Labels', [])]
        except Exception as e:
            print(f"Rekognition label detection failed: {e}")
            
        # 3. Update original photo metadata with AI analysis
        metadata['ai_tags'] = ",".join(ai_tags)
        metadata['flagged'] = "true" if is_flagged else "false"
        if mod_reason:
            metadata['mod_reason'] = mod_reason
            
        try:
            s3.copy_object(
                Bucket=bucket,
                Key=key,
                CopySource={'Bucket': bucket, 'Key': key},
                Metadata=metadata,
                MetadataDirective='REPLACE',
                ContentType=content_type
            )
        except Exception as e:
            print(f"Metadata update failed: {e}")
            
        # 4. Generate Thumbnail
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img = ImageOps.exif_transpose(img)
            
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
                
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            thumb_buf = io.BytesIO()
            img.save(thumb_buf, format="JPEG", quality=85)
            thumb_bytes = thumb_buf.getvalue()
            
            filename = key.rsplit("/", 1)[-1]
            photo_id = filename.rsplit(".", 1)[0]
            thumb_key = f"{THUMB_PREFIX}{photo_id}.jpg"
            
            s3.put_object(
                Bucket=bucket,
                Key=thumb_key,
                Body=thumb_bytes,
                ContentType="image/jpeg"
            )
            print(f"Successfully generated and uploaded thumbnail {thumb_key}")
        except Exception as e:
            print(f"Thumbnail generation failed: {e}")
            
    return {"statusCode": 200, "body": json.dumps("Processing complete")}
