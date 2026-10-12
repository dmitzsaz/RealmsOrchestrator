from aiohttp import web

from worldmanager import manager


async def currentworlds(request):
    return web.json_response(manager.snapshot())
