from aiohttp import web
from database.crud import get_worlds, get_world, create_world, update_world, delete_world, SessionLocal
import json

# Get a list of all worlds
async def worlds(request):
    result = [
        {"id": w.id, "name": w.name, "status": w.status}
        for w in get_worlds()
    ]
    return web.json_response(result)