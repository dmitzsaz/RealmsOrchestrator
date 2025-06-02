from aiohttp import web
import docker
from database.crud import get_world

docker_client = docker.from_env()

async def currentworlds(request):
    containers = docker_client.containers.list()
    result = []
    for c in containers:
        if c.name.startswith('minecraft_'):
            # world_id — это всё после 'minecraft_'
            world_id = c.name[len('minecraft_'):]
            world = get_world(world_id)
            if world:
                result.append({
                    'id': world.id,
                    'name': world.name,
                    'status': c.status,
                    'created': c.attrs.get('Created'),
                    'ports': c.attrs.get('NetworkSettings', {}).get('Ports', {})
                })
    return web.json_response(result)