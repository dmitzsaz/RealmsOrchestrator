from aiohttp import web

from worldmanager import WorldError, manager


async def runworld(request):
    try:
        result = await manager.start(request.match_info.get("world_id"), request.query.get("provider"))
    except WorldError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    return web.json_response(result)
