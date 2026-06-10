"""MinIO client wrapper for preserved-evidence object storage."""
from __future__ import annotations

import io
from typing import Optional

from minio import Minio

from api.config import get_settings

_client: Optional[Minio] = None


def get_client() -> Minio:
    """Lazily create and cache the MinIO client."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_user,
            secret_key=settings.minio_password,
            secure=settings.minio_secure,
        )
    return _client


def ensure_bucket() -> str:
    """Create the evidence bucket if it does not exist; return its name."""
    settings = get_settings()
    client = get_client()
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
    return settings.minio_bucket


def put_bytes(object_path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload raw bytes to the evidence bucket; return the object path."""
    bucket = ensure_bucket()
    client = get_client()
    client.put_object(
        bucket,
        object_path,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return object_path


def get_object_bytes(object_path: str) -> bytes:
    """Download an object's bytes from the evidence bucket."""
    settings = get_settings()
    client = get_client()
    response = client.get_object(settings.minio_bucket, object_path)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def ping() -> bool:
    """Return True if MinIO answers a bucket listing."""
    try:
        get_client().list_buckets()
        return True
    except Exception:
        return False
