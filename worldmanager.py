"""World lifecycle on any provider.

start    -> provider.launch(); the agent bootstraps the world from R2 and
            reports "ready"; status init -> running.
running  -> heartbeats with the player list; empty for EMPTY_STOP_MINUTES
            stops the world. The agent uploads temp snapshots on its own timer.
stop     -> the agent stops Minecraft and uploads the final save to cold
            storage; provider.destroy(); runtime columns -> NULL; status idle.
crash    -> Minecraft died, the instance vanished, or the agent went silent:
            provider.destroy(), the temp snapshot is promoted if newer.
restart  -> reconcile() re-attaches to live instances and recovers dead ones.
"""
import asyncio
import base64
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

import storage
from agenthub import AgentDisconnected, AgentError, AgentHub, make_token
from config import settings
from database.crud import clear_runtime, get_placed_worlds, get_world, update_world
from providers import ConnectMode, LaunchSpec, Provider, load_providers
from utils import get_level_name, is_newauth, player_list, uses_proxy_whitelist

log = logging.getLogger("worlds")

AUTH_RELAY_PORT = 25580
AUTH_RELAY_PATHS = ("/discovery", "/session/", "/services/", "/users/profiles/", "/profiles/minecraft", "/minecraft/profile/")
TEMP_URL_TTL = 12 * 3600
# World params that select orchestrator behaviour instead of server env.
ORCHESTRATOR_PARAMS = {"PROVIDER"}
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class WorldError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class LiveWorld:
    session_id: str
    ready: bool = False
    stopping: bool = False
    started: float = field(default_factory=time.monotonic)
    last_contact: float = field(default_factory=time.monotonic)
    last_players_at: float = field(default_factory=time.monotonic)
    players: list = field(default_factory=list)
    current_admins: set = field(default_factory=set)
    supervisor: Optional[asyncio.Task] = None
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def merge_placement(primary: Optional[dict], fallback: Optional[dict]) -> Optional[dict]:
    merged = dict(fallback or {})
    merged.update({k: v for k, v in (primary or {}).items() if v is not None})
    return merged or None


class WorldManager:
    def __init__(self, hub: AgentHub):
        self.hub = hub
        hub.handler = self
        self.providers: dict[str, Provider] = {}
        self.live: dict[int, LiveWorld] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()
        self.http: Optional[aiohttp.ClientSession] = None

    # ---- process lifecycle -------------------------------------------------

    async def startup(self) -> None:
        self.providers = load_providers(settings.enabled_providers)
        if settings.DEFAULT_PROVIDER not in self.providers:
            raise RuntimeError(f"DEFAULT_PROVIDER {settings.DEFAULT_PROVIDER!r} is not in ENABLED_PROVIDERS")
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        log.info("Providers: %s (default %s)", ", ".join(self.providers), settings.DEFAULT_PROVIDER)
        await self.reconcile()

    async def cleanup(self) -> None:
        # Worlds keep running; the next start re-attaches through reconcile().
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for provider in self.providers.values():
            await provider.close()
        if self.http is not None:
            await self.http.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _lock(self, world_id: int) -> asyncio.Lock:
        return self.locks.setdefault(world_id, asyncio.Lock())

    @staticmethod
    def _get(world_id):
        try:
            world_id = int(world_id)
        except (TypeError, ValueError):
            raise WorldError("Invalid world ID", 400)
        world = get_world(world_id)
        if world is None:
            raise WorldError("World not found", 404)
        return world

    # ---- public operations ---------------------------------------------------

    async def start(self, world_id, provider_name: Optional[str] = None) -> dict:
        world = self._get(world_id)
        if not world.s3URL:
            raise WorldError("World not found or not uploaded to S3", 404)
        if world.status == "updating":
            raise WorldError("World is currently being updated. Please try again later.", 409)

        async with self._lock(world.id):
            world = self._get(world.id)
            if world.status == "updating":
                raise WorldError("World is currently being updated. Please try again later.", 409)
            if world.instanceId:
                provider = self.providers.get(world.provider)
                if provider is None:
                    raise WorldError(f"World runs on provider {world.provider!r}, which is not enabled", 409)
                try:
                    state = await provider.inspect(world.instanceId)
                except Exception as e:
                    raise WorldError(f"{provider.name}: {e}", 502)
                if state.alive:
                    if world.id not in self.live:
                        self._attach(world)
                    return self.describe(world, world.status)
                await self._recover(world, f"instance {state.status.value} before start")
                world = self._get(world.id)

            params = world.params if isinstance(world.params, dict) else {}
            name = provider_name or params.get("provider") or settings.DEFAULT_PROVIDER
            provider = self.providers.get(name)
            if provider is None:
                raise WorldError(f"Provider {name!r} is not enabled", 400)

            session_id = secrets.token_hex(16)
            # The session is stored before launch: a fast agent may connect
            # before launch() returns.
            update_world(world.id, status="init", provider=provider.name, sessionId=session_id,
                         instanceId=None, ips=None, ports=None)
            live = self.live[world.id] = LiveWorld(session_id)
            try:
                instance = await provider.launch(self._spec(world, provider, session_id))
            except Exception as e:
                log.exception("Launch of world %s on %s failed", world.id, provider.name)
                self.live.pop(world.id, None)
                clear_runtime(world.id, status="idle")
                raise WorldError(f"Failed to launch on {provider.name}: {e}", 502)

            world = update_world(world.id, instanceId=instance.instance_id, ips=instance.ips, ports=instance.ports)
            live.supervisor = self._spawn(self._supervise(world.id, session_id))
            log.info("World %s launched on %s as %s", world.id, provider.name, instance.instance_id)
            return self.describe(world, "started")

    def request_stop(self, world_id) -> bool:
        world = self._get(world_id)
        if not world.instanceId or not world.provider:
            return False
        live = self.live.get(world.id)
        if live is not None:
            live.stopping = True
        if world.status != "updating":
            # Set synchronously so a start racing this request gets a 409.
            update_world(world.id, status="updating")
        # Stops serialize on the world lock; a duplicate finds nothing to do.
        self._spawn(self.stop(world.id, "request"))
        return True

    async def stop(self, world_id: int, reason: str) -> None:
        async with self._lock(world_id):
            world = get_world(world_id)
            if world is None or not world.instanceId:
                return
            log.info("Stopping world %s (%s)", world_id, reason)
            live = self.live.get(world_id)
            if live is not None:
                live.stopping = True
            update_world(world_id, status="updating")
            saved = await self._final_save(world, live)
            await self._destroy(world)
            await self._finish(world, saved_final=saved)

    async def rcon(self, world_id, command: str) -> str:
        world = self._get(world_id)
        conn = self.hub.get(world.id)
        if conn is None:
            raise WorldError("Мир не запущен или агент не подключен", 409)
        try:
            reply = await conn.call("rcon", command=command, timeout=15)
        except (AgentError, asyncio.TimeoutError) as e:
            raise WorldError(f"RCON: {str(e) or 'timeout'}", 502)
        return reply.get("result", "")

    async def connect(self, world_id, ticket: Optional[str]) -> dict:
        """Tells mcproxy how to reach a running world (see docs/providers.md)."""
        world = self._get(world_id)
        provider = self.providers.get(world.provider) if world.provider else None
        if provider is None or not world.instanceId or world.status != "running":
            raise WorldError("World is not running", 503)
        if provider.connect_mode != ConnectMode.TUNNEL:
            address = provider.direct_address(world.ips, world.ports)
            if not address:
                raise WorldError("World address is not known yet", 503)
            return {"mode": "direct", "address": address}
        if not ticket or not TICKET_RE.match(ticket):
            raise WorldError("A ticket ([A-Za-z0-9_-]{16,128}) is required for tunnel worlds", 400)
        if not settings.PROXY_TUNNEL_ADDR:
            raise WorldError("PROXY_TUNNEL_ADDR is not configured", 500)
        conn = self.hub.get(world.id)
        if conn is None:
            raise WorldError("World agent is not connected", 503)
        try:
            await conn.call("tunnel", ticket=ticket, addr=settings.PROXY_TUNNEL_ADDR,
                            tls=settings.PROXY_TUNNEL_TLS, timeout=15)
        except (AgentError, asyncio.TimeoutError) as e:
            raise WorldError(f"Tunnel failed: {str(e) or 'timeout'}", 502)
        return {"mode": "tunnel"}

    async def backup(self, world_id) -> None:
        """Uploads a temp snapshot now."""
        world = self._get(world_id)
        conn = self.hub.get(world.id)
        if conn is None:
            raise WorldError("World agent is not connected", 409)
        try:
            await conn.call("save", timeout=settings.WORLD_STOP_TIMEOUT)
        except (AgentError, asyncio.TimeoutError) as e:
            raise WorldError(f"Backup failed: {str(e) or 'timeout'}", 502)

    def describe(self, world, status: str) -> dict:
        provider = self.providers.get(world.provider) if world.provider else None
        domain = None
        if world.domainPrefix is not None and settings.BASE_DOMAIN != "undefined":
            domain = f"{world.domainPrefix}.{settings.BASE_DOMAIN}"
        ports = world.ports or {}
        return {
            "status": status,
            "provider": world.provider,
            "instance_id": world.instanceId,
            "container_id": world.instanceId,
            "connect_mode": provider.connect_mode.value if provider else None,
            # Direct connections only; proxy-whitelisted worlds go through mcproxy.
            "mc_port": None if uses_proxy_whitelist(world) else ports.get("public"),
            "rcon_port": None,  # RCON stays inside the container, use the agent
            "ips": world.ips,
            "ports": world.ports,
            "domain": domain,
        }

    def snapshot(self) -> list[dict]:
        result = []
        for world in get_placed_worlds():
            live = self.live.get(world.id)
            conn = self.hub.get(world.id)
            result.append({
                "id": world.id,
                "name": world.name,
                **self.describe(world, world.status),
                "agent_connected": conn is not None,
                "agent_last_seen": round(time.monotonic() - conn.last_seen, 1) if conn else None,
                "ready": bool(live and live.ready),
                "players": live.players if live else [],
            })
        return result

    # ---- launch spec -----------------------------------------------------------

    def _spec(self, world, provider: Provider, session_id: str) -> LaunchSpec:
        params = world.params if isinstance(world.params, dict) else {}
        server = {"TYPE": "FABRIC", "MODRINTH_PROJECTS": "fabric-api,lithium", **params}
        env = {"EULA": "TRUE", "ENABLE_COMMAND_BLOCK": "true", "MAX_PLAYERS": 5}
        env.update({k.upper(): v for k, v in server.items() if k.upper() not in ORCHESTRATOR_PARAMS})

        memory_mb = provider.settings.MEMORY_MB
        if memory_mb and not any(k in env for k in ("MEMORY", "MAX_MEMORY", "INIT_MEMORY")):
            env["MEMORY"] = f"{int(memory_mb * 0.75)}M"

        if is_newauth(world):
            # The proxy keeps world.params as its policy; the backend uses
            # online auth against mcproxy's session API, directly or via the
            # agent's relay on nodes that cannot reach mcproxy.
            auth_url = provider.auth_url
            if auth_url is None:
                auth_url = f"http://127.0.0.1:{AUTH_RELAY_PORT}"
                env["REALMS_AUTH_RELAY_PORT"] = AUTH_RELAY_PORT
            env["ONLINE_MODE"] = "true"
            env["JVM_OPTS"] = (
                f"{env.get('JVM_OPTS') or ''} "
                "-Dminecraft.api.env=custom "
                f"-Dminecraft.api.discovery.host={auth_url}/discovery "
                "-Dminecraft.api.auth.host=https://authserver.mojang.com "
                f"-Dminecraft.api.account.host={auth_url} "
                f"-Dminecraft.api.session.host={auth_url}/session "
                f"-Dminecraft.api.services.host={auth_url}/services "
                f"-Dminecraft.api.profiles.host={auth_url}"
            ).strip()
            online_mode = params.get("ONLINE_MODE", params.get("online_mode", True))
            if online_mode == 0 or str(online_mode).strip().lower() in ("false", "0", "no"):
                env["ENFORCE_SECURE_PROFILE"] = "false"

        # The agent depends on these, so world params cannot override them.
        env.update({
            "LEVEL": get_level_name(world),
            "SERVER_PORT": 25565,
            "ENABLE_RCON": "true",
            "RCON_PORT": 25575,
            "RCON_PASSWORD": secrets.token_urlsafe(18),
            "REALMS_ORCHESTRATOR_URL": provider.orchestrator_url,
            "REALMS_AGENT_TOKEN": make_token(world.id, session_id),
        })
        return LaunchSpec(
            world_id=world.id,
            session_id=session_id,
            name=f"minecraft_{world.id}_{world.domainPrefix}",
            image=provider.image,
            env={k: str(v) for k, v in env.items()},
            cpu=provider.settings.CPU,
            memory_mb=memory_mb,
            expose_minecraft=not uses_proxy_whitelist(world),
            labels={"realms.world": str(world.id), "realms.session": session_id},
        )

    # ---- supervision -------------------------------------------------------------

    async def _inspect(self, world_id: int):
        world = get_world(world_id)
        provider = self.providers.get(world.provider) if world and world.provider else None
        if provider is None or not world.instanceId:
            return None
        try:
            return await provider.inspect(world.instanceId)
        except Exception:
            # A provider API outage must not be mistaken for a dead world.
            log.exception("Inspecting world %s failed", world_id)
            return None

    async def _supervise(self, world_id: int, session_id: str) -> None:
        next_inspect = 0.0
        while True:
            await asyncio.sleep(5)
            live = self.live.get(world_id)
            if live is None or live.session_id != session_id or live.stopping:
                return
            try:
                now = time.monotonic()
                if not live.ready:
                    if now - live.started > settings.WORLD_BOOT_TIMEOUT:
                        await self._fail(world_id, session_id, "boot timeout")
                        return
                    # Until the agent connects, only the provider knows whether
                    # the instance failed (bad image, no capacity, ...).
                    if self.hub.get(world_id) is None and now >= next_inspect:
                        next_inspect = now + 30
                        state = await self._inspect(world_id)
                        if state is not None and not state.alive:
                            await self._fail(world_id, session_id, f"instance {state.status.value} during boot {state.detail}")
                            return
                    continue

                silent = now - live.last_contact
                if silent > settings.AGENT_HEARTBEAT_TIMEOUT:
                    if now >= next_inspect:
                        next_inspect = now + 30
                        state = await self._inspect(world_id)
                        if state is not None and not state.alive:
                            await self._fail(world_id, session_id, f"instance {state.status.value} {state.detail}")
                            return
                    if silent > settings.AGENT_LOST_TIMEOUT:
                        await self._fail(world_id, session_id, f"agent silent for {int(silent)}s")
                        return
                    continue

                empty_for = now - live.last_players_at
                if settings.EMPTY_STOP_MINUTES and not live.players and empty_for >= settings.EMPTY_STOP_MINUTES * 60:
                    await self.stop(world_id, "no players")
                    return
            except Exception:
                log.exception("Supervisor of world %s", world_id)

    async def _fail(self, world_id: int, session_id: str, reason: str) -> None:
        async with self._lock(world_id):
            world = get_world(world_id)
            if world is None or world.sessionId != session_id:
                return
            await self._recover(world, reason)

    # ---- teardown (callers hold the world lock) ---------------------------------

    async def _final_save(self, world, live: Optional[LiveWorld]) -> bool:
        """Asks the agent for the final save into cold storage."""
        if self.hub.get(world.id) is None and not (live and live.ready):
            return False  # never became ready: nothing newer than cold storage
        url = storage.presign_put(storage.main_key(world.id), settings.WORLD_STOP_TIMEOUT + 3600)
        deadline = time.monotonic() + settings.WORLD_STOP_TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0:
            conn = await self.hub.wait_for(world.id, min(60, remaining))
            if conn is None:
                log.warning("World %s: agent unreachable for the final save", world.id)
                return False
            try:
                reply = await conn.call("shutdown", upload_url=url, timeout=remaining)
                return bool(reply.get("saved"))
            except AgentDisconnected:
                continue  # shutdown is idempotent on the agent; ask again after reconnect
            except (AgentError, asyncio.TimeoutError) as e:
                log.warning("World %s: final save failed: %s", world.id, str(e) or "timeout")
                return False
        return False

    async def _destroy(self, world) -> None:
        provider = self.providers.get(world.provider)
        if provider is None or not world.instanceId:
            return
        for attempt in range(3):
            try:
                await provider.destroy(world.instanceId)
                return
            except Exception:
                log.exception("Destroying %s on %s (attempt %d)", world.instanceId, provider.name, attempt + 1)
                await asyncio.sleep(5 * (attempt + 1))
        log.error("World %s: instance %s on %s may be orphaned", world.id, world.instanceId, provider.name)

    async def _recover(self, world, reason: str) -> None:
        log.warning("World %s crashed (%s); recovering from the temp snapshot", world.id, reason)
        live = self.live.get(world.id)
        if live is not None:
            live.stopping = True
        update_world(world.id, status="updating")
        await self._destroy(world)
        await self._finish(world, saved_final=False)

    async def _finish(self, world, saved_final: bool) -> None:
        s3_url = world.s3URL
        if saved_final:
            s3_url = storage.object_url(storage.main_key(world.id))
        if world.sessionId:
            temp = storage.temp_key(world.id, world.sessionId)
            try:
                if not saved_final and await storage.promote_temp(world.id, world.sessionId):
                    s3_url = storage.object_url(storage.main_key(world.id))
                    log.info("World %s: promoted %s to cold storage", world.id, temp)
                await storage.delete(temp)
            except Exception:
                log.exception("World %s: keeping %s for manual recovery", world.id, temp)
        clear_runtime(world.id, status="idle", s3URL=s3_url)
        live = self.live.pop(world.id, None)
        if live is not None and live.supervisor is not None and live.supervisor is not asyncio.current_task():
            live.supervisor.cancel()
        await self.hub.drop(world.id)
        log.info("World %s is idle (final save: %s)", world.id, saved_final)

    # ---- startup reconciliation ----------------------------------------------------

    async def reconcile(self) -> None:
        for world in get_placed_worlds():
            try:
                await self._reconcile_world(world)
            except Exception:
                log.exception("Reconciling world %s", world.id)

    async def _reconcile_world(self, world) -> None:
        if not world.provider:
            # Started by the pre-provider orchestrator; stop it with that version.
            log.warning("World %s (%s) has no provider; ignoring it", world.id, world.status)
            return
        if not world.instanceId:
            # The orchestrator died during launch(); nothing to re-attach to.
            log.warning("World %s: launch on %s was interrupted", world.id, world.provider)
            await self._finish(world, saved_final=False)
            return
        provider = self.providers.get(world.provider)
        if provider is None:
            log.error("World %s runs on %s, which is not enabled", world.id, world.provider)
            return
        state = await provider.inspect(world.instanceId)
        if not state.alive:
            async with self._lock(world.id):
                await self._recover(world, f"instance {state.status.value} while the orchestrator was down")
            return
        self._attach(world)
        log.info("World %s re-attached on %s (%s)", world.id, provider.name, world.status)

    def _attach(self, world) -> None:
        # "updating" means a stop was in progress, so the world had been
        # running: wait for its agent to finish the final save.
        live = self.live[world.id] = LiveWorld(world.sessionId, ready=world.status in ("running", "updating"))
        if world.status == "updating":
            self._spawn(self.stop(world.id, "resuming an interrupted stop"))
        else:
            live.supervisor = self._spawn(self._supervise(world.id, world.sessionId))

    # ---- agent messages -----------------------------------------------------------

    async def on_agent_message(self, conn, msg: dict):
        live = self.live.get(conn.world_id)
        if live is None or live.session_id != conn.session_id:
            raise AgentError("session is not active")
        live.last_contact = time.monotonic()
        kind = msg.get("type")
        if kind in ("hello", "heartbeat"):
            await self._on_state(conn.world_id, live, msg)
        elif kind == "event":
            await self._on_event(conn, live, msg)
        elif kind == "bootstrap":
            return self._bootstrap(conn.world_id, conn.session_id)
        elif kind == "upload_url":
            key = storage.temp_key(conn.world_id, conn.session_id)
            return {"url": storage.presign_put(key, TEMP_URL_TTL), "expires_in": TEMP_URL_TTL}
        elif kind == "auth_http":
            return await self._relay_auth(conn.world_id, msg)
        else:
            raise AgentError(f"unknown message type {kind!r}")

    def _bootstrap(self, world_id: int, session_id: str) -> dict:
        world = get_world(world_id)
        key = storage.object_key(world.s3URL) if world.s3URL else None
        return {
            "level": get_level_name(world),
            "download_url": storage.presign_get(key) if key else None,
            "temp_upload_url": storage.presign_put(storage.temp_key(world_id, session_id), TEMP_URL_TTL),
            "temp_url_expires_in": TEMP_URL_TTL,
            "temp_interval": settings.TEMP_SAVE_INTERVAL_MINUTES * 60,
        }

    async def _on_state(self, world_id: int, live: LiveWorld, msg: dict) -> None:
        if msg.get("ready") and not live.ready:
            await self._on_ready(world_id, live, msg.get("ips"))
        players = msg.get("players")
        if not isinstance(players, list):
            return
        live.players = players
        if players:
            live.last_players_at = time.monotonic()
        if live.ready and not live.stopping:
            await self._sync_admins(world_id, live, players)

    async def _on_event(self, conn, live: LiveWorld, msg: dict) -> None:
        event = msg.get("event")
        if event == "ready":
            await self._on_ready(conn.world_id, live, msg.get("ips"))
        elif event == "temp_saved":
            log.info("World %s: temp snapshot uploaded (%s bytes)", conn.world_id, msg.get("size"))
        elif event == "mc_exited" and not live.stopping:
            await self._fail(conn.world_id, conn.session_id, f"minecraft exited with {msg.get('code')}")

    async def _on_ready(self, world_id: int, live: LiveWorld, agent_ips: Optional[dict]) -> None:
        if live.ready or live.stopping:
            return
        live.ready = True
        live.last_players_at = time.monotonic()
        world = get_world(world_id)
        try:
            await self._setup_whitelist(world)
        except Exception:
            log.exception("World %s: setting up admins and whitelist", world_id)
        state = await self._inspect(world_id)
        ips = merge_placement(state.ips if state else None, merge_placement(world.ips, agent_ips))
        ports = merge_placement(state.ports if state else None, world.ports)
        update_world(world_id, status="running", ips=ips, ports=ports)
        log.info("World %s is running (%s)", world_id, ips)

    async def _setup_whitelist(self, world) -> None:
        if uses_proxy_whitelist(world):
            # The proxy checks these names. Vanilla's online profile lookup
            # would put Mojang UUIDs in the whitelist instead of offline UUIDs.
            await self.rcon(world.id, "whitelist off")
            return
        await self.rcon(world.id, "whitelist on")
        for admin in player_list(world.admins):
            await self.rcon(world.id, f"whitelist add {admin}")
            await self.rcon(world.id, f"op {admin}")
        for player in player_list(world.players):
            await self.rcon(world.id, f"whitelist add {player}")

    async def _sync_admins(self, world_id: int, live: LiveWorld, players: list) -> None:
        world = get_world(world_id)
        if world is None or not (is_newauth(world) or uses_proxy_whitelist(world)):
            return
        async with live.sync_lock:
            admins = {name.casefold() for name in player_list(world.admins)}
            connected = {p.casefold() for p in players if p.casefold() in admins}
            for player in players:
                name = player.casefold()
                if name in connected and name not in live.current_admins:
                    # Login has populated vanilla's profile cache with the
                    # actual UUID selected by the proxy.
                    await self.rcon(world_id, f"op {player}")
                elif name in live.current_admins and name not in admins:
                    await self.rcon(world_id, f"deop {player}")
            live.current_admins = connected

    async def _relay_auth(self, world_id: int, msg: dict) -> dict:
        method = str(msg.get("method", "GET")).upper()
        path = str(msg.get("path", ""))
        route = path.split("?", 1)[0]
        if method not in ("GET", "POST") or ".." in route or not route.startswith(AUTH_RELAY_PATHS):
            raise AgentError("auth path not allowed")
        headers = {"X-Realms-World-Id": str(world_id)}
        if settings.INTERNAL_API_TOKEN:
            headers["X-Realms-Internal-Token"] = settings.INTERNAL_API_TOKEN
        if msg.get("content_type"):
            headers["Content-Type"] = msg["content_type"]
        body = base64.b64decode(msg["body"]) if msg.get("body") else None
        url = settings.MCPROXY_AUTH_URL.rstrip("/") + path
        async with self.http.request(method, url, data=body, headers=headers) as resp:
            data = await resp.content.read((1 << 20) + 1)
            if len(data) > 1 << 20:
                raise AgentError("auth response too large")
            if route == "/discovery":
                # Discovery returns absolute URLs on mcproxy's AUTH_PUBLIC_URL;
                # point them back at the agent's local relay.
                upstream = (settings.MCPROXY_AUTH_PUBLIC_URL or settings.MCPROXY_AUTH_URL).rstrip("/")
                data = data.replace(upstream.encode(), f"http://127.0.0.1:{AUTH_RELAY_PORT}".encode())
            return {
                "status": resp.status,
                "content_type": resp.headers.get("Content-Type", ""),
                "body": base64.b64encode(data).decode(),
            }


hub = AgentHub()
manager = WorldManager(hub)
