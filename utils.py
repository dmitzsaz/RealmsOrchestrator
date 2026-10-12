import aiohttp
import os
import uuid
import asyncio
import json

import storage
from config import settings

DEFAULT_LEVEL_NAME = "world"

def extract_object_name(s3_url):
    return storage.object_key(s3_url)

def upload_to_r2_sync(file_path: str, object_name: str) -> str:
    storage.client().upload_file(file_path, settings.R2_BUCKET, object_name)
    return storage.object_url(object_name)

async def upload_to_r2(file_path: str, object_name: str) -> str:
    return await asyncio.to_thread(upload_to_r2_sync, file_path, object_name)

async def download_file(url: str, dest_folder: str = "/tmp") -> str:
    filename = os.path.basename(url)
    if not filename:
        filename = f"download_{uuid.uuid4()}"
    dest_path = os.path.join(dest_folder, filename)
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                while True:
                    chunk = await resp.content.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
    return dest_path

def generate_presigned_url(object_name: str, expires_in: int = 3600) -> str:
    return storage.presign_get(object_name, expires_in)

def download_from_r2(object_name: str, dest_path: str) -> None:
    storage.client().download_file(settings.R2_BUCKET, object_name, dest_path)

def player_list(value):
    if not value:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value) if isinstance(value, list) else []

def world_params(world):
    return world.params if isinstance(world.params, dict) else {}

def is_newauth(world):
    return world_params(world).get("newAuth") is True

def uses_proxy_whitelist(world):
    params = world_params(world)
    online_mode = params.get("ONLINE_MODE", params.get("online_mode", True))
    offline = online_mode == 0 or str(online_mode).strip().lower() in ("false", "0", "no")
    return offline and (params.get("newAuth") is True or settings.OFFLINEMODE_ALTWHITELIST)

def get_level_name(world):
    params = world_params(world)
    for key in ("LEVEL_NAME", "level_name", "LEVEL", "level"):
        if params.get(key):
            return str(params[key])
    return DEFAULT_LEVEL_NAME
