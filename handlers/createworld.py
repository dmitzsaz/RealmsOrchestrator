from aiohttp import web
from database.crud import create_world, update_world
import os
import uuid
from utils import upload_to_r2, download_file
import asyncio
import zipfile
import shutil

TEMP_DIR = "./worlds_tmp"
os.makedirs(TEMP_DIR, exist_ok=True)

async def background_upload_and_update(world_id, final_path, final_name):
    try:
        s3_url = await upload_to_r2(final_path, final_name)
        print(f"Uploaded to R2: {s3_url}")
        update_world(world_id, s3URL=s3_url, status="idle")
    finally:
        if os.path.exists(final_path):
            os.remove(final_path)

async def prepare_and_pack_world(zip_path: str, out_zip_path: str):
    import glob

    temp_unpack_dir = os.path.join(TEMP_DIR, f"unpack_{uuid.uuid4()}")
    os.makedirs(temp_unpack_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(temp_unpack_dir)

    for root, dirs, files in os.walk(temp_unpack_dir):
        for d in dirs:
            if d == '__MACOSX' or d.startswith('.'):
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        for f in files:
            if f == '.DS_Store' or f.startswith('.'):
                os.remove(os.path.join(root, f))

    items = [i for i in os.listdir(temp_unpack_dir) if not i.startswith('.') and i != '__MACOSX']
    if len(items) == 1 and os.path.isdir(os.path.join(temp_unpack_dir, items[0])):
        inner = os.path.join(temp_unpack_dir, items[0])
        for f in os.listdir(inner):
            shutil.move(os.path.join(inner, f), temp_unpack_dir)
        os.rmdir(inner)

    with zipfile.ZipFile(out_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(temp_unpack_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, temp_unpack_dir)
                zipf.write(abs_path, rel_path)

    shutil.rmtree(temp_unpack_dir)

async def createworld(request):
    reader = await request.multipart()
    name = None
    temp_path = None
    filename = None

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "name":
            name = (await field.read(decode=True)).decode()
        elif field.name == "file":
            filename = f"world_{uuid.uuid4()}.zip"
            temp_path = os.path.join(TEMP_DIR, filename)
            with open(temp_path, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
        elif field.name == "url":
            url = (await field.read(decode=True)).decode()
            filename = os.path.basename(url) or f"world_{uuid.uuid4()}.zip"
            temp_path = await download_file(url, TEMP_DIR)

    if not name or not temp_path or not filename:
        return web.json_response({"error": "name and file or url are required"}, status=400)

    world = create_world(name=name, s3URL="", status="creating")

    final_name = f"mc_world_{world.id or uuid.uuid4()}.zip"
    final_path = os.path.join(TEMP_DIR, final_name)
    await prepare_and_pack_world(temp_path, final_path)
    os.remove(temp_path)

    asyncio.create_task(background_upload_and_update(world.id, final_path, final_name))

    return web.json_response({
        "id": world.id,
        "name": world.name
    }, status=201)