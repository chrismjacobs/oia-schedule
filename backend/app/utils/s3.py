"""Shared S3 upload helper (CLAUDE.md #11). Three distinct image kinds — task
reference photo, task completion/proof photo, time-card photo — all go
through this one helper but never share a key or column."""
import uuid

import boto3
from flask import current_app


def _client():
    cfg = current_app.config
    return boto3.client(
        "s3",
        aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"],
        region_name=cfg["AWS_S3_REGION"],
    )


def upload_object(file_storage, key_prefix):
    ext = "jpg"
    filename = file_storage.filename or ""
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
    key = f"{key_prefix}/{uuid.uuid4()}.{ext}"
    _client().upload_fileobj(
        file_storage, current_app.config["AWS_S3_BUCKET"], key,
        ExtraArgs={"ContentType": file_storage.mimetype or "application/octet-stream"},
    )
    return key


def presigned_view_url(s3_key, expires_seconds=600):
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": current_app.config["AWS_S3_BUCKET"], "Key": s3_key},
        ExpiresIn=expires_seconds,
    )
