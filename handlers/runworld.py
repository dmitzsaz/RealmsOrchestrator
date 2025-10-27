from aiohttp import web
import docker
import asyncio
from mcrcon import MCRcon
import os
from utils import get_free_port, download_from_r2, extract_object_name, setup_admins_and_whitelist, zip_and_upload_world, fix_permissions
import zipfile
from database.crud import get_world, update_world
from handlers.stopworld import stopworld
from config import settings
import time
import json

DOCKER_IMAGE = "itzg/minecraft-server:latest"
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "minecraft")

WORLDS_DIR = "./worlds_tmp"
os.makedirs(WORLDS_DIR, exist_ok=True)

docker_client = docker.from_env()

async def wait_for_server(rcon_port, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with MCRcon("localhost", RCON_PASSWORD, port=rcon_port) as mcr:
                resp = mcr.command("list")
                if resp:
                    return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False  # Server did not start in time

async def monitor_players(rcon_port, container_name, world):
    ready = await wait_for_server(rcon_port)
    if not ready:
        print("Minecraft server did not start in time!")
        return

    try:
        admins = json.loads(world.admins) if isinstance(world.admins, str) and world.admins else (world.admins if isinstance(world.admins, list) else [])
        players = json.loads(world.players) if isinstance(world.players, str) and world.players else (world.players if isinstance(world.players, list) else [])

        setup_admins_and_whitelist(rcon_port, admins, players, RCON_PASSWORD)
    except Exception as e:
        print(f"Error setting up admins and whitelist: {e}")

    last_players = time.time()
    check_interval = 60
    empty_timeout = 5 * 60

    while True:
        try:
            with MCRcon("localhost", RCON_PASSWORD, port=rcon_port) as mcr:
                resp = mcr.command("list")
                if "There are 0" in resp:
                    if time.time() - last_players >= empty_timeout:
                        await stopworld(world.id)
                        break
                else:
                    last_players = time.time()
        except Exception as e:
            container = docker_client.containers.get(container_name)
            if not container or container.status == "exited":
                print(f"Container {container_name} is dead, uploading world {world.id} to R2")
                await stopworld(world.id)
                break
            else:
                print(f"Error monitoring players: {e}")
        await asyncio.sleep(check_interval)

async def getDockerContainer(world_name):
    containers = docker_client.containers.list(all=True)
    for c in containers:
        if c.name == world_name:
            return c
    return None

def prepareResponse(container):
    ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
    return web.json_response({
        "status": container.status,
        "container_id": container.id,
        "mc_port": ports.get("25565/tcp", [{}])[0].get("HostPort") if ports.get("25565/tcp") else None,
        "rcon_port": ports.get("25575/tcp", [{}])[0].get("HostPort") if ports.get("25575/tcp") else None,
        "status": container.status
    })

async def runworld(request):
    world_id = request.match_info.get("world_id")
    world = get_world(world_id)
    if not world or not world.s3URL:
        return web.json_response({"error": "World not found or not uploaded to S3"}, status=404)
    if world.status == "updating":
        return web.json_response({"error": "World is currently being updated. Please try again later."}, status=409)

    world_name = f"minecraft_{world_id}_{world.domainPrefix}"

    if world.status == "init" or world.status == "running":
        while True:
            container = await getDockerContainer(world_name)
            if not container or container.status == "exited":
                break
            return prepareResponse(container)
            await asyncio.sleep(1)

    update_world(world_id, status="init")

    container = await getDockerContainer(world_name)
    if container and container.status != "exited":
        return prepareResponse(container)

    s3_url = world.s3URL
    world_dir = os.path.join(WORLDS_DIR, world_name)
    os.makedirs(world_dir, exist_ok=True)

    archive_path = os.path.join(WORLDS_DIR, f"{world_name}.zip")
    object_name = extract_object_name(s3_url)
    download_from_r2(object_name, archive_path)

    with zipfile.ZipFile(archive_path, 'r') as zip_ref:
        zip_ref.extractall(world_dir)
    os.remove(archive_path)

    abs_world_dir = os.path.abspath(world_dir)

    fix_permissions(abs_world_dir)

    mc_port = get_free_port()
    rcon_port = get_free_port()

    params = {
        "TYPE": "FABRIC",
        "MODRINTH_PROJECTS": "fabric-api,lithium"
    }
    if world.params != {}:
        params.update(world.params)
    paramsFormatted = {k.upper(): v for k, v in params.items()}

    container = docker_client.containers.run(
        DOCKER_IMAGE,
        name=f"minecraft_{world_id}_{world.domainPrefix}",
        detach=True,
        ports={"25565/tcp": mc_port, "25575/tcp": rcon_port},
        environment={
            "EULA": "TRUE",
            "ENABLE_RCON": "true",
            "RCON_PASSWORD": RCON_PASSWORD,
            "RCON_PORT": 25575,
            "SERVER_PORT": 25565,
            "ENABLE_COMMAND_BLOCK": "true",
            "MAX_PLAYERS": 5,
            **paramsFormatted
        },
        volumes={
            abs_world_dir: {"bind": "/data/world", "mode": "rw"}
        }
    )

    update_world(world_id, status="running")

    asyncio.create_task(monitor_players(rcon_port, world_name, world))

    domain = None
    if world.domainPrefix != None and settings.BASE_DOMAIN != "undefined":
        domain = f"{world.domainPrefix}.{settings.BASE_DOMAIN}"

    return web.json_response({
        "status": "started",
        "container_id": container.id,
        "mc_port": mc_port,
        "rcon_port": rcon_port,
        "domain": domain
    })