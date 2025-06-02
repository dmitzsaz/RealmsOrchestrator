from aiohttp import web

from handlers.worlds import worlds
from handlers.runworld import runworld
from handlers.stopworld import stopworld
from handlers.currentworlds import currentworlds
from handlers.getworldlink import getworldlink
from handlers.createworld import createworld
from database.db import create_database
from config import settings

from handlers.playersManagement import add_admin, remove_admin, add_player, remove_player

create_database()

app = web.Application()

async def index(request):
    return web.Response(text='Hello, World!')

routes = [
    web.get('/', index),
    web.get('/worlds', worlds),
    web.get('/worlds/{world_id}/start', runworld),
    web.get('/worlds/{world_id}/stop', stopworld),
    web.get('/worlds/active', currentworlds),
    web.get('/worlds/{world_id}/download', getworldlink),
    web.post('/createworld', createworld),
    web.post('/worlds/{world_id}/admins', add_admin),
    web.delete('/worlds/{world_id}/admins', remove_admin),
    web.post('/worlds/{world_id}/players', add_player),
    web.delete('/worlds/{world_id}/players', remove_player),
]

app.add_routes(routes)

listen_addr = settings.LISTEN_ADDR.split(":")
listen_port = int(listen_addr[1])
listen_host = listen_addr[0]
web.run_app(app, host=listen_host, port=listen_port)