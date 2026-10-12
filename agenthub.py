"""Control channel between the orchestrator and node agents.

Each agent keeps one outbound WebSocket to /agent/v1/ws. Messages are JSON:
  request       {"id": "7", "type": "rcon", ...}
  reply         {"re": "7", "ok": true, ...} or {"re": "7", "ok": false, "error": "..."}
  notification  {"type": "heartbeat", ...}
Both sides send requests. The token binds a connection to one world session,
so an agent from a previous run of the world is rejected with HTTP 410.
"""
import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import time

from aiohttp import WSMsgType, web

from config import settings
from database.crud import get_world

log = logging.getLogger("agenthub")


class AgentError(Exception):
    pass


class AgentDisconnected(AgentError):
    pass


def make_token(world_id, session_id) -> str:
    payload = f"{world_id}.{session_id}"
    signature = hmac.new(settings.agent_secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_token(token: str):
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        return None
    if not hmac.compare_digest(make_token(parts[0], parts[1]), token):
        return None
    return int(parts[0]), parts[1]


class AgentConnection:
    def __init__(self, world_id: int, session_id: str, ws: web.WebSocketResponse):
        self.world_id = world_id
        self.session_id = session_id
        self.ws = ws
        self.ids = itertools.count(1)
        self.pending: dict[str, asyncio.Future] = {}
        self.last_seen = time.monotonic()

    @property
    def closed(self) -> bool:
        return self.ws.closed

    async def send(self, message: dict) -> None:
        await self.ws.send_json(message)

    async def call(self, type_: str, timeout: float = 30, **params) -> dict:
        if self.closed:
            raise AgentDisconnected("agent is not connected")
        request_id = str(next(self.ids))
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "type": type_, **params})
            reply = await asyncio.wait_for(future, timeout)
        except ConnectionResetError as e:
            raise AgentDisconnected(str(e)) from e
        finally:
            self.pending.pop(request_id, None)
        if not reply.get("ok"):
            raise AgentError(reply.get("error") or f"{type_} failed")
        return reply

    def fail_pending(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(AgentDisconnected("agent disconnected"))


class AgentHub:
    def __init__(self):
        self.connections: dict[int, AgentConnection] = {}
        self.waiters: dict[int, list[asyncio.Future]] = {}
        self.handler = None  # WorldManager
        self.tasks: set[asyncio.Task] = set()

    def get(self, world_id: int):
        conn = self.connections.get(world_id)
        return conn if conn is not None and not conn.closed else None

    async def wait_for(self, world_id: int, timeout: float):
        conn = self.get(world_id)
        if conn is not None:
            return conn
        future = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(world_id, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            waiters = self.waiters.get(world_id, [])
            if future in waiters:
                waiters.remove(future)

    async def drop(self, world_id: int) -> None:
        conn = self.connections.pop(world_id, None)
        if conn is not None and not conn.closed:
            await conn.ws.close(code=4001, message=b"session ended")

    async def close_all(self) -> None:
        # aiohttp's graceful shutdown waits for open WebSocket handlers.
        for conn in list(self.connections.values()):
            await conn.ws.close(code=1001, message=b"orchestrator shutting down")
        self.connections.clear()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle_ws(self, request: web.Request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        identity = verify_token(token)
        if identity is None:
            return web.json_response({"error": "invalid token"}, status=401)
        world_id, session_id = identity
        world = get_world(world_id)
        if world is None or world.sessionId != session_id:
            return web.json_response({"error": "session superseded"}, status=410)

        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        conn = AgentConnection(world_id, session_id, ws)
        previous = self.connections.get(world_id)
        self.connections[world_id] = conn
        if previous is not None and not previous.closed:
            await previous.ws.close(code=4000, message=b"replaced")
        for future in self.waiters.pop(world_id, []):
            if not future.done():
                future.set_result(conn)
        log.info("Agent for world %s connected from %s", world_id, request.remote)

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                conn.last_seen = time.monotonic()
                if "re" in data:
                    future = conn.pending.get(str(data["re"]))
                    if future is not None and not future.done():
                        future.set_result(data)
                    continue
                # Handlers may call back into this agent (e.g. RCON on a
                # heartbeat), so they must not block this receive loop.
                self._spawn(self._dispatch(conn, data))
        finally:
            conn.fail_pending()
            if self.connections.get(world_id) is conn:
                del self.connections[world_id]
            log.info("Agent for world %s disconnected", world_id)
        return ws

    async def _dispatch(self, conn: AgentConnection, data: dict) -> None:
        request_id = data.get("id")
        try:
            result = await self.handler.on_agent_message(conn, data)
        except Exception as e:
            if request_id is None:
                log.exception("Agent message %s for world %s failed", data.get("type"), conn.world_id)
                return
            reply = {"re": request_id, "ok": False, "error": str(e)}
        else:
            if request_id is None:
                return
            reply = {"re": request_id, "ok": True, **(result or {})}
        try:
            await conn.send(reply)
        except (ConnectionResetError, RuntimeError):
            pass
