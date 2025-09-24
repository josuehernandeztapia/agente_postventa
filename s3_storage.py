import os
import uuid
import mimetypes
from typing import Optional

S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_BUCKET = os.getenv("S3_BUCKET")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_REGION = os.getenv("S3_REGION", "us-east-1")

def available() -> bool:
    return bool(S3_ENDPOINT and S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY)

def presign_upload(case_id: str, filename: str, kind: str) -> Optional[dict]:
    if not available():
        return None
    try:
        import boto3
        s3 = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name=S3_REGION,
        )
        key = f"postventa/{case_id}/raw/{uuid.uuid4()}_{filename}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=900,
        )
        return {"upload_url": url, "object_key": key, "content_type": content_type}
    except Exception:
        return None

def public_url(key: str) -> Optional[str]:
    if not available():
        return None
    return f"{S3_ENDPOINT.rstrip('/')}/{S3_BUCKET}/{key}"

