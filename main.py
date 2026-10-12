import logging

from aiohttp import web

from handlers.worlds import worlds
from handlers.runworld import runworld
from handlers.stopworld import stopworldRequest, backupworld
from handlers.currentworlds import currentworlds
from handlers.getworldlink import getworldlink
from handlers.createworld import createworld
from handlers.internal import connect
from database.db import create_database
from config import settings
from worldmanager import hub, manager

from handlers.playersManagement import add_admin, remove_admin, add_player, remove_player

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

create_database()

app = web.Application()

async def index(request):
    return web.Response(text='Hello, World!')

routes = [
    web.get('/', index),
    web.get('/worlds', worlds),
    web.get('/worlds/{world_id}/start', runworld),
    web.get('/worlds/{world_id}/stop', stopworldRequest),
    web.post('/worlds/{world_id}/backup', backupworld),
    web.get('/worlds/active', currentworlds),
    web.get('/worlds/{world_id}/download', getworldlink),
    web.post('/createworld', createworld),
    web.post('/worlds/{world_id}/admins', add_admin),
    web.delete('/worlds/{world_id}/admins', remove_admin),
    web.post('/worlds/{world_id}/players', add_player),
    web.delete('/worlds/{world_id}/players', remove_player),
    web.post('/internal/worlds/{world_id}/connect', connect),
]

app.add_routes(routes)

# Agents on remote providers must reach this listener; keeping it separate
# lets the management API above stay internal.
agent_routes = [web.get('/agent/v1/ws', hub.handle_ws)]
separate_agent_listener = settings.AGENT_LISTEN_PORT not in (0, settings.LISTEN_PORT)
agent_app = web.Application()
if separate_agent_listener:
    agent_app.add_routes(agent_routes)
else:
    app.add_routes(agent_routes)
agent_runner_key = web.AppKey("agent_runner", web.AppRunner)

async def on_startup(app):
    # Reconcile before agents can connect, so every placed world is known.
    await manager.startup()
    if separate_agent_listener:
        runner = web.AppRunner(agent_app)
        await runner.setup()
        for host in settings.LISTEN_ADDR:
            await web.TCPSite(runner, host, settings.AGENT_LISTEN_PORT).start()
        app[agent_runner_key] = runner

async def on_shutdown(app):
    # Stop supervising first, so closing the agent connections is not taken
    # for lost worlds. The worlds keep running and are re-attached on start.
    await manager.cleanup()
    await hub.close_all()
    if agent_runner_key in app:
        await app[agent_runner_key].cleanup()

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)
web.run_app(app, host=settings.LISTEN_ADDR, port=settings.LISTEN_PORT)
