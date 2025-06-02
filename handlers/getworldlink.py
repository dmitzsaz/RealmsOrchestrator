from aiohttp import web

async def getworldlink(request):
    return web.Response(text='Hello, World!')