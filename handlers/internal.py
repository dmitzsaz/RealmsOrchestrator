"""API for mcproxy. Requires Authorization: Bearer INTERNAL_API_TOKEN."""
import hmac

from aiohttp import web

from config import settings
from worldmanager import WorldError, manager


def authorized(request) -> bool:
    expected = f"Bearer {settings.INTERNAL_API_TOKEN}"
    return hmac.compare_digest(request.headers.get("Authorization", ""), expected)


async def connect(request):
    if not settings.INTERNAL_API_TOKEN:
        return web.json_response({"error": "INTERNAL_API_TOKEN is not configured"}, status=503)
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        result = await manager.connect(request.match_info.get("world_id"), (body or {}).get("ticket"))
    except WorldError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    return web.json_response(result)
