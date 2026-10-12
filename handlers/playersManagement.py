from aiohttp import web
from database.crud import get_world, update_world
from utils import player_list, uses_proxy_whitelist
from worldmanager import WorldError, manager

async def RCON_command(world_id, command):
    try:
        return {"result": await manager.rcon(world_id, command)}
    except WorldError as e:
        return {"error": str(e)}

async def addAdmin(world_id, admin_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    admins = player_list(world.admins)
    players = player_list(world.players)

    if admin_nick not in admins:
        admins.append(admin_nick)
        update_world(world_id, admins=admins)

    if admin_nick not in players:
        await addPlayer(world_id, admin_nick)

    await RCON_command(world_id, f"op {admin_nick}")
    return {"result": f"{admin_nick} added to admins, whitelist and given op"}

async def removeAdmin(world_id, admin_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    admins = player_list(world.admins)
    if admin_nick in admins:
        admins.remove(admin_nick)
        update_world(world_id, admins=admins)

    await RCON_command(world_id, f"deop {admin_nick}")
    return {"result": f"{admin_nick} removed from admins"}

async def addPlayer(world_id, player_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    players = player_list(world.players)
    if player_nick not in players:
        players.append(player_nick)
        update_world(world_id, players=players)

    if not uses_proxy_whitelist(world):
        await RCON_command(world_id, f"whitelist add {player_nick}")
    return {"result": f"{player_nick} added to whitelist"}

async def removePlayer(world_id, player_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    players = player_list(world.players)
    if player_nick in players:
        players.remove(player_nick)
        update_world(world_id, players=players)

    if not uses_proxy_whitelist(world):
        await RCON_command(world_id, f"whitelist remove {player_nick}")
    return {"result": f"{player_nick} removed from whitelist"}

async def add_admin(request):
    world_id = request.match_info.get("world_id")
    admin_nick = request.query.get("nick")
    if not admin_nick:
        return web.json_response({"error": "Nickname is required"}, status=400)
    result = await addAdmin(world_id, admin_nick)
    return web.json_response(result)

async def remove_admin(request):
    world_id = request.match_info.get("world_id")
    admin_nick = request.query.get("nick")
    if not admin_nick:
        return web.json_response({"error": "Nickname is required"}, status=400)
    result = await removeAdmin(world_id, admin_nick)
    return web.json_response(result)

async def add_player(request):
    world_id = request.match_info.get("world_id")
    player_nick = request.query.get("nick")
    if not player_nick:
        return web.json_response({"error": "Nickname is required"}, status=400)
    result = await addPlayer(world_id, player_nick)
    return web.json_response(result)

async def remove_player(request):
    world_id = request.match_info.get("world_id")
    player_nick = request.query.get("nick")
    if not player_nick:
        return web.json_response({"error": "Nickname is required"}, status=400)
    result = await removePlayer(world_id, player_nick)
    return web.json_response(result)

