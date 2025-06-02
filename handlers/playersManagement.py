from mcrcon import MCRcon
from aiohttp import web
import docker
from database.crud import get_world, update_world
import json
import os

RCON_PASSWORD = os.getenv("RCON_PASSWORD", "minecraft")

docker_client = docker.DockerClient()

def get_running_world_container_and_rcon_port(world_id):
    world_name = f"minecraft_{world_id}"
    containers = docker_client.containers.list(all=True)
    for c in containers:
        if c.name == world_name and c.status == "running":
            ports = c.attrs.get('NetworkSettings', {}).get('Ports', {})
            rcon_port = None
            if "25575/tcp" in ports and ports["25575/tcp"]:
                rcon_port = int(ports["25575/tcp"][0]["HostPort"])
            return c, rcon_port
    return None, None

async def RCON_command(world_id, command):
    container, rcon_port = get_running_world_container_and_rcon_port(world_id)
    if not container or not rcon_port:
        return {"error": "Мир не запущен или RCON порт не найден"}
    try:
        with MCRcon("localhost", RCON_PASSWORD, port=rcon_port) as mcr:
            resp = mcr.command(command)
        return {"result": resp}
    except Exception as e:
        return {"error": str(e)}

async def addAdmin(world_id, admin_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    admins = json.loads(world.admins) if world.admins else []
    players = json.loads(world.players) if world.players else []

    if admin_nick not in admins:
        admins.append(admin_nick)
        update_world(world_id, admins=json.dumps(admins))

    if admin_nick not in players:
        await addPlayer(world_id, admin_nick)

    await RCON_command(world_id, f"op {admin_nick}")
    return {"result": f"{admin_nick} added to admins, whitelist and given op"}

async def removeAdmin(world_id, admin_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    admins = json.loads(world.admins) if world.admins else []
    if admin_nick in admins:
        admins.remove(admin_nick)
        update_world(world_id, admins=json.dumps(admins))

    await RCON_command(world_id, f"deop {admin_nick}")
    return {"result": f"{admin_nick} removed from admins"}

async def addPlayer(world_id, player_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    players = json.loads(world.players) if world.players else []
    if player_nick not in players:
        players.append(player_nick)
        update_world(world_id, players=json.dumps(players))

    await RCON_command(world_id, f"whitelist add {player_nick}")
    return {"result": f"{player_nick} added to whitelist"}

async def removePlayer(world_id, player_nick):
    world = get_world(world_id)
    if not world:
        return {"error": "Мир не найден"}
    players = json.loads(world.players) if world.players else []
    if player_nick in players:
        players.remove(player_nick)
        update_world(world_id, players=json.dumps(players))

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
    