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

async def stopworld(world_id):
    world = get_world(world_id)
    if not world:
        return "World not found"
        
    world_name = f"minecraft_{world.id}_{world.domainPrefix}"

    containers = docker_client.containers.list(all=True)
    for c in containers:
        if c.name == world_name:
            try:
                c.stop()
                update_world(world_id, status="updating")
                asyncio.create_task(background_save_world(world_id, world_name))
                c.remove()
                return True
            except Exception as e:
                return str(e)
    return False

async def stopworldRequest(request):
    world_id = request.match_info.get("world_id")
    result = await stopworld(world_id)
    if type(result) == bool:
        if result:
            return web.json_response({"result": f"World {world_id} stopped, saving to R2 in background, status set to updating"})
        else:
            return web.json_response({"error": "Container not found"}, status=404)
    else:
        return web.json_response({"error": result}, status=500)