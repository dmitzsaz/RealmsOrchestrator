"""World archives in R2.

Cold storage: mc_world_{id}.zip, referenced by worlds.s3URL and overwritten
only by a clean final save.
Temp: temp/mc_world_{id}_{session}.zip, overwritten by the agent every
TEMP_SAVE_INTERVAL_MINUTES while the world runs. After a crash it is promoted
to cold storage if it is newer; after a normal stop it is deleted.

Agents never receive R2 credentials, only presigned URLs for their own keys.
"""
import asyncio
from functools import lru_cache
from urllib.parse import urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from config import settings

TEMP_PREFIX = "temp/"


@lru_cache(maxsize=1)
def client():
    # boto3 clients are thread-safe; sessions are not, so build it once.
    return boto3.session.Session().client(
        service_name="s3",
        aws_access_key_id=settings.R2_ACCESS_KEY,
        aws_secret_access_key=settings.R2_SECRET_KEY,
        endpoint_url=settings.R2_ENDPOINT,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def main_key(world_id) -> str:
    return f"mc_world_{world_id}.zip"


def temp_key(world_id, session_id) -> str:
    return f"{TEMP_PREFIX}mc_world_{world_id}_{session_id}.zip"


def object_url(key: str) -> str:
    """The s3URL format stored in worlds.s3URL (see extract_object_name)."""
    return f"https://{settings.R2_BUCKET}.{settings.R2_ENDPOINT.replace('https://', '')}/{key}"


def object_key(s3_url: str) -> str:
    return urlparse(s3_url).path.lstrip("/")


def presign_get(key: str, expires_in: int = 6 * 3600) -> str:
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.R2_BUCKET, "Key": key}, ExpiresIn=expires_in)


def presign_put(key: str, expires_in: int = 12 * 3600) -> str:
    # R2 caps presigned URLs at 7 days and single PUTs at ~5 GB.
    return client().generate_presigned_url(
        "put_object", Params={"Bucket": settings.R2_BUCKET, "Key": key}, ExpiresIn=expires_in)


def _head(key: str):
    try:
        return client().head_object(Bucket=settings.R2_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


async def head(key: str):
    return await asyncio.to_thread(_head, key)


async def delete(key: str) -> None:
    await asyncio.to_thread(client().delete_object, Bucket=settings.R2_BUCKET, Key=key)


def _promote(src: str, dst: str) -> bool:
    temp = _head(src)
    if temp is None:
        return False
    current = _head(dst)
    # A final save that finished just before a crash is newer than the last
    # temp snapshot and must not be rolled back.
    if current is not None and current["LastModified"] >= temp["LastModified"]:
        return False
    client().copy_object(
        Bucket=settings.R2_BUCKET, Key=dst,
        CopySource={"Bucket": settings.R2_BUCKET, "Key": src})
    return True


async def promote_temp(world_id, session_id) -> bool:
    """Makes the session's temp snapshot the cold backup if it is newer."""
    return await asyncio.to_thread(_promote, temp_key(world_id, session_id), main_key(world_id))
