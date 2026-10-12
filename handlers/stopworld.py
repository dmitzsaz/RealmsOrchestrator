from aiohttp import web

from worldmanager import WorldError, manager


async def stopworldRequest(request):
    world_id = request.match_info.get("world_id")
    try:
        stopping = manager.request_stop(world_id)
    except WorldError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    if not stopping:
        return web.json_response({"error": "World is not running"}, status=404)
    return web.json_response({"result": f"World {world_id} stopping, saving to R2 in background, status set to updating"})


async def backupworld(request):
    try:
        await manager.backup(request.match_info.get("world_id"))
    except WorldError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    return web.json_response({"result": "Temp snapshot uploaded"})
