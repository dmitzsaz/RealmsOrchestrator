from aiohttp import web
import docker
import os
import asyncio
from utils import zip_and_upload_world
from database.crud import get_world, update_world

WORLDS_DIR = "./worlds_tmp"
docker_client = docker.from_env()

async def background_save_world(world_id, world_name):
    world_dir = os.path.abspath(os.path.join(WORLDS_DIR, world_name))
    if os.path.exists(world_dir):
        s3_url = await zip_and_upload_world(world_dir, f"mc_world_{world_id}.zip")
        update_world(world_id, s3URL=s3_url, status="idle")
    else:
        update_world(world_id, status="idle")

async def stopworld(request):
    world_id = request.match_info.get("world_id")
    world_name = f"minecraft_{world_id}"
    world = get_world(world_id)
    if not world:
        return web.json_response({"error": "World not found"}, status=404)

    containers = docker_client.containers.list(all=True)
    for c in containers:
        if c.name == world_name:
            try:
                c.stop()
                update_world(world_id, status="updating")
                asyncio.create_task(background_save_world(world_id, world_name))
                c.remove()
                return web.json_response({"result": f"World {world_id} stopped, saving to R2 in background, status set to updating"})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"error": "Container not found"}, status=404)