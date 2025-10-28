import boto3
from botocore.client import Config
from config import settings
import aiohttp
import os
import uuid
import asyncio
import socket
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from mcrcon import MCRcon
import zipfile
import subprocess

def extract_object_name(s3_url):
    return urlparse(s3_url).path.lstrip('/')

def upload_to_r2_sync(file_path: str, object_name: str) -> str:
    session = boto3.session.Session()
    client = session.client(
        service_name='s3',
        aws_access_key_id=settings.R2_ACCESS_KEY,
        aws_secret_access_key=settings.R2_SECRET_KEY,
        endpoint_url=settings.R2_ENDPOINT,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    client.upload_file(file_path, settings.R2_BUCKET, object_name)
    return f"https://{settings.R2_BUCKET}.{settings.R2_ENDPOINT.replace('https://', '')}/{object_name}"

async def upload_to_r2(file_path: str, object_name: str) -> str:
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, upload_to_r2_sync, file_path, object_name)

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
    session = boto3.session.Session()
    client = session.client(
        service_name='s3',
        aws_access_key_id=settings.R2_ACCESS_KEY,
        aws_secret_access_key=settings.R2_SECRET_KEY,
        endpoint_url=settings.R2_ENDPOINT,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    return client.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.R2_BUCKET, 'Key': object_name},
        ExpiresIn=expires_in
    )

def download_from_r2(object_name: str, dest_path: str) -> None:
    session = boto3.session.Session()
    client = session.client(
        service_name='s3',
        aws_access_key_id=settings.R2_ACCESS_KEY,
        aws_secret_access_key=settings.R2_SECRET_KEY,
        endpoint_url=settings.R2_ENDPOINT,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    client.download_file(settings.R2_BUCKET, object_name, dest_path)

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def setup_admins_and_whitelist(rcon_port, admins, players, rcon_password, world):
    if settings.OFFLINEMODE_ALTWHITELIST and (world.params.get("ONLINE_MODE", "true") == "false" or world.params.get("online_mode", "true") == "false"):
        return

    with MCRcon("localhost", rcon_password, port=rcon_port) as mcr:
        mcr.command("whitelist on")
        for admin in admins:
            mcr.command(f"whitelist add {admin}")
            mcr.command(f"op {admin}")
        for player in players:
            mcr.command(f"whitelist add {player}")

def givePlayerOp(rcon_port, player, rcon_password):
    if settings.OFFLINEMODE_ALTWHITELIST != True:
        return
        
    with MCRcon("localhost", rcon_password, port=rcon_port) as mcr:
        mcr.command(f"op {player}")

async def zip_and_upload_world(world_dir: str, r2_name: str = None) -> str:
    if not r2_name:
        r2_name = f"world_{uuid.uuid4()}.zip"
    temp_zip = os.path.join("/tmp", r2_name)

    with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(world_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, world_dir)
                zipf.write(abs_path, rel_path)
    # Загружаем архив на R2
    s3_url = await upload_to_r2(temp_zip, r2_name)
    os.remove(temp_zip)
    return s3_url

def fix_permissions(world_dir):
    subprocess.run(['chmod', '-R', '777', world_dir])