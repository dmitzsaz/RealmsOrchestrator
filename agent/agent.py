#!/usr/bin/env python3
"""Realms node agent: the entrypoint of the runtime image.

Runs next to itzg/minecraft-server's /start and
  * downloads the world from R2 through a presigned URL,
  * supervises Minecraft and talks to it over local RCON,
  * uploads a temp snapshot every few minutes and the final save on stop,
  * keeps one outbound WebSocket to the orchestrator for commands,
  * dials mcproxy's tunnel listener on request (reverse tunnels),
  * relays newAuth session HTTP through the orchestrator when asked to.
Every connection is outbound, so the node needs no public address.
"""
import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import logging
import os
import random
import shutil
import signal
import socket
import ssl
import struct
import sys
import time
import zipfile

import aiohttp
from aiohttp import web
from yarl import URL

ORCHESTRATOR_URL = os.environ["REALMS_ORCHESTRATOR_URL"].rstrip("/")
TOKEN = os.environ["REALMS_AGENT_TOKEN"]
# SHA-256 of the orchestrator's self-signed certificate; accepted instead of a CA.
ORCHESTRATOR_PIN = os.environ.get("REALMS_ORCHESTRATOR_TLS_SHA256", "").strip().lower()
SESSION_ID = TOKEN.split(".")[1]
DATA_DIR = os.environ.get("REALMS_DATA_DIR", "/data")
MC_COMMAND = os.environ.get("REALMS_MC_COMMAND", "/start").split()
SERVER_PORT = int(os.environ.get("SERVER_PORT", "25565"))
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "minecraft")
AUTH_RELAY_PORT = int(os.environ.get("REALMS_AUTH_RELAY_PORT") or 0)
STOP_GRACE = int(os.environ.get("REALMS_STOP_GRACE", "90"))
UID = int(os.environ.get("UID", "1000"))
GID = int(os.environ.get("GID", "1000"))
SESSION_MARKER = os.path.join(DATA_DIR, ".realms-session")

log = logging.getLogger("agent")


class RemoteError(Exception):
    pass


# ---- RCON ---------------------------------------------------------------------

class Rcon:
    def __init__(self):
        self.reader = self.writer = None
        self.lock = asyncio.Lock()
        self.ids = itertools.count(1)

    async def _send(self, kind: int, body: str) -> int:
        request_id = next(self.ids)
        payload = struct.pack("<ii", request_id, kind) + body.encode() + b"\0\0"
        self.writer.write(struct.pack("<i", len(payload)) + payload)
        await self.writer.drain()
        return request_id

    async def _read(self):
        size = struct.unpack("<i", await self.reader.readexactly(4))[0]
        data = await self.reader.readexactly(size)
        request_id, kind = struct.unpack("<ii", data[:8])
        return request_id, kind, data[8:-2].decode("utf-8", "replace")

    async def _command(self, command: str) -> str:
        if self.writer is None:
            self.reader, self.writer = await asyncio.open_connection("127.0.0.1", RCON_PORT)
            await self._send(3, RCON_PASSWORD)
            if (await self._read())[0] == -1:
                raise PermissionError("RCON authentication failed")
        request_id = await self._send(2, command)
        while True:
            response_id, _, body = await self._read()
            if response_id == request_id:
                return body

    async def command(self, command: str, timeout: float = 10) -> str:
        async with self.lock:
            try:
                return await asyncio.wait_for(self._command(command), timeout)
            except BaseException:
                self.close()
                raise

    def close(self):
        if self.writer is not None:
            self.writer.close()
        self.reader = self.writer = None


# ---- files --------------------------------------------------------------------

def looks_like_world(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "level.dat")) or os.path.isdir(os.path.join(path, "region"))


def zip_dir(src: str, dest: str) -> int:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, _, files in os.walk(src):
            for name in files:
                path = os.path.join(root, name)
                try:
                    archive.write(path, os.path.relpath(path, src))
                except FileNotFoundError:
                    pass  # removed while walking
    return os.path.getsize(dest)


def extract(archive: str, dest: str) -> None:
    # extractall drops absolute paths and ".." components.
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)


def chown_tree(path: str) -> None:
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.lchown(name, UID, GID)
            except OSError:
                pass


def read_marker():
    try:
        with open(SESSION_MARKER) as f:
            return f.read().strip()
    except OSError:
        return None


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 9))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return None


def parse_players(response: str) -> list:
    # "There are 2 of a max of 5 players online: Alex, Steve"
    if ":" not in response:
        return []
    return [p.strip() for p in response.split(":", 1)[1].split(",") if p.strip()]


async def splice(reader_a, writer_a, reader_b, writer_b) -> None:
    async def pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    await asyncio.gather(pipe(reader_a, writer_b), pipe(reader_b, writer_a))


# ---- agent --------------------------------------------------------------------

class Agent:
    def __init__(self):
        self.rcon = Rcon()
        self.http = None
        self.ws = None
        self.ids = itertools.count(1)
        self.pending = {}
        self.tasks = set()
        self.done = asyncio.Event()
        self.exit_code = 0

        self.level = None
        self.temp_interval = 0
        self.temp_url = None
        self.temp_url_expires = 0.0

        self.mc = None
        self.ready = False
        self.players = []
        self.dirty = False           # players were online since the last snapshot
        self.save_lock = asyncio.Lock()
        self.stopping = False        # Minecraft is being stopped on purpose
        self.shutdown_task = None
        self.ip = local_ip()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Task failed", exc_info=t.exception())
        task.add_done_callback(done)
        return task

    def finish(self, code: int) -> None:
        self.exit_code = code
        self.done.set()

    @property
    def world_dir(self) -> str:
        return os.path.join(DATA_DIR, self.level)

    @property
    def mc_running(self) -> bool:
        return self.mc is not None and self.mc.returncode is None

    async def run(self) -> int:
        self.http = aiohttp.ClientSession()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self.spawn(self.on_sigterm()))
        self.spawn(self.control_loop())
        self.spawn(self.boot())
        await self.done.wait()
        if self.mc_running:
            self.mc.kill()
            await self.mc.wait()
        await self.http.close()
        return self.exit_code

    # ---- control channel ----

    async def control_loop(self) -> None:
        url = URL(ORCHESTRATOR_URL + "/agent/v1/ws")
        url = url.with_scheme({"http": "ws", "https": "wss"}.get(url.scheme, url.scheme))
        tls = aiohttp.Fingerprint(bytes.fromhex(ORCHESTRATOR_PIN)) if ORCHESTRATOR_PIN else True
        backoff = 1
        while not self.done.is_set():
            try:
                async with self.http.ws_connect(url, headers={"Authorization": f"Bearer {TOKEN}"}, ssl=tls,
                                                heartbeat=20, max_msg_size=16 * 1024 * 1024) as ws:
                    self.ws, backoff = ws, 1
                    log.info("Connected to the orchestrator")
                    await self.send_state("hello")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                self.on_message(json.loads(msg.data))
                            except ValueError:
                                log.warning("Ignoring a malformed control message")
            except aiohttp.WSServerHandshakeError as e:
                if e.status in (401, 410):
                    log.error("Orchestrator rejected this session (HTTP %s)", e.status)
                    self.spawn(self.abandon())
                    return
                log.warning("Orchestrator handshake failed: HTTP %s", e.status)
            except aiohttp.ServerFingerprintMismatch:
                log.error("Orchestrator certificate does not match REALMS_ORCHESTRATOR_TLS_SHA256")
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                log.warning("Orchestrator unreachable: %s", e)
            except Exception:
                log.exception("Control channel failed")
            finally:
                self.ws = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("orchestrator disconnected"))
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30)

    def on_message(self, msg: dict) -> None:
        if "re" in msg:
            future = self.pending.get(str(msg["re"]))
            if future is not None and not future.done():
                future.set_result(msg)
        elif "id" in msg:
            self.spawn(self.handle_request(msg))

    async def send(self, msg: dict) -> None:
        if self.ws is None:
            raise ConnectionError("orchestrator offline")
        await self.ws.send_json(msg)

    async def notify(self, type_: str, **params) -> None:
        try:
            await self.send({"type": type_, **params})
        except (ConnectionError, RuntimeError):
            pass

    async def send_state(self, type_: str) -> None:
        await self.notify(type_, ready=self.ready, players=self.players if self.ready else None,
                          mc="running" if self.mc_running else "stopped",
                          ips={"private": self.ip, "public": None})

    async def request(self, type_: str, timeout: float = 30, **params) -> dict:
        request_id = str(next(self.ids))
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "type": type_, **params})
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)
        if not reply.get("ok"):
            raise RemoteError(reply.get("error") or f"{type_} failed")
        return reply

    async def handle_request(self, msg: dict) -> None:
        handlers = {
            "rcon": self.cmd_rcon,
            "save": self.cmd_save,
            "shutdown": self.cmd_shutdown,
            "tunnel": self.cmd_tunnel,
        }
        handler = handlers.get(msg.get("type"))
        try:
            if handler is None:
                raise ValueError(f"unknown request {msg.get('type')!r}")
            reply = {"re": msg["id"], "ok": True, **(await handler(msg) or {})}
        except Exception as e:
            reply = {"re": msg["id"], "ok": False, "error": str(e) or type(e).__name__}
        try:
            await self.send(reply)
        except (ConnectionError, RuntimeError):
            pass

    # ---- boot ----

    async def boot(self) -> None:
        while True:
            try:
                config = await self.request("bootstrap")
                break
            except RemoteError as e:
                log.error("Bootstrap refused: %s", e)
                self.finish(1)
                return
            except (ConnectionError, asyncio.TimeoutError):
                await asyncio.sleep(2)
        self.level = config["level"]
        self.temp_interval = int(config.get("temp_interval") or 0)
        self.temp_url = config["temp_upload_url"]
        self.temp_url_expires = time.time() + config["temp_url_expires_in"]
        try:
            await self.prepare_world(config.get("download_url"))
        except Exception:
            log.exception("Could not prepare the world")
            await self.notify("event", event="mc_exited", code=None, reason="world download failed")
            self.finish(1)
            return
        if AUTH_RELAY_PORT:
            await self.start_auth_relay()
        if self.stopping:
            return
        self.mc = await asyncio.create_subprocess_exec(*MC_COMMAND)
        log.info("Minecraft started (pid %s)", self.mc.pid)
        self.spawn(self.watch_minecraft())
        self.spawn(self.monitor_loop())
        if self.temp_interval > 0:
            self.spawn(self.temp_loop())

    async def prepare_world(self, download_url) -> None:
        target = self.world_dir
        if read_marker() == SESSION_ID and looks_like_world(target):
            # The container restarted within this session; local files are newer.
            log.info("Keeping the world from an earlier run of this session")
            return
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target, exist_ok=True)
        if download_url:
            archive = os.path.join(DATA_DIR, ".realms-download.zip")
            async with self.http.get(URL(download_url, encoded=True),
                                     timeout=aiohttp.ClientTimeout(total=None, sock_read=300)) as resp:
                resp.raise_for_status()
                with open(archive, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
            await asyncio.to_thread(extract, archive, target)
            os.remove(archive)
            log.info("World downloaded into %s", target)
        await asyncio.to_thread(chown_tree, DATA_DIR)
        with open(SESSION_MARKER, "w") as f:
            f.write(SESSION_ID)

    # ---- Minecraft supervision ----

    async def monitor_loop(self) -> None:
        while self.mc_running:
            try:
                players = parse_players(await self.rcon.command("list"))
                self.players = players
                if players:
                    self.dirty = True
                if not self.ready:
                    self.ready = True
                    log.info("Minecraft is ready")
                    await self.notify("event", event="ready", ips={"private": self.ip, "public": None})
            except Exception:
                pass  # still booting, or RCON hiccup
            await self.send_state("heartbeat")
            await asyncio.sleep(5 if self.ready else 2)

    async def watch_minecraft(self) -> None:
        code = await self.mc.wait()
        self.ready = False
        self.rcon.close()
        if self.stopping:
            return
        log.warning("Minecraft exited on its own with code %s", code)
        temp_saved = False
        if code == 0:
            # A clean exit (e.g. /stop) leaves consistent files: snapshot them
            # so the orchestrator promotes this state instead of an older one.
            try:
                temp_saved = await self.upload_world(await self.temp_upload_url())
            except Exception:
                log.exception("Snapshot after exit failed")
        await self.notify("event", event="mc_exited", code=code, temp_saved=temp_saved)
        self.spawn(self.exit_later(600, 1 if code else 0))

    async def stop_minecraft(self) -> bool:
        """Stops Minecraft; True if it shut down by itself in time."""
        if self.mc is None:
            return False
        if self.mc.returncode is not None:
            return self.mc.returncode == 0
        try:
            await self.rcon.command("save-on")
            await self.rcon.command("stop")
        except Exception:
            # mc-server-runner turns SIGTERM into a graceful "stop".
            if self.mc.returncode is None:
                try:
                    self.mc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(self.mc.wait(), STOP_GRACE)
            return True
        except asyncio.TimeoutError:
            log.error("Minecraft did not stop in %ss, killing it", STOP_GRACE)
            self.mc.kill()
            await self.mc.wait()
            return False

    # ---- snapshots ----

    async def temp_upload_url(self) -> str:
        if time.time() > self.temp_url_expires - 3600:
            try:
                reply = await self.request("upload_url", kind="temp")
                self.temp_url = reply["url"]
                self.temp_url_expires = time.time() + reply["expires_in"]
            except Exception as e:
                log.warning("Could not refresh the temp upload URL: %s", e)
        return self.temp_url

    async def upload(self, url: str, path: str) -> None:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            async with self.http.put(URL(url, encoded=True), data=f, headers={"Content-Length": str(size)},
                                     timeout=aiohttp.ClientTimeout(total=None, sock_read=300)) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"upload failed: HTTP {resp.status} {(await resp.text())[:200]}")

    async def upload_world(self, url: str) -> bool:
        """Zips and uploads the world as it is on disk (Minecraft stopped or saving off)."""
        if not looks_like_world(self.world_dir):
            log.error("%s does not look like a world, not uploading", self.world_dir)
            return False
        path = f"/tmp/realms-world-{int(time.time())}.zip"
        try:
            size = await asyncio.to_thread(zip_dir, self.world_dir, path)
            await self.upload(url, path)
            log.info("Uploaded world snapshot (%s bytes)", size)
            return True
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def save_temp(self) -> bool:
        async with self.save_lock:
            if self.stopping or not self.ready:
                return False
            self.dirty = False
            path = f"/tmp/realms-temp-{int(time.time())}.zip"
            try:
                await self.rcon.command("save-off")
                try:
                    await self.rcon.command("save-all flush", timeout=300)
                    size = await asyncio.to_thread(zip_dir, self.world_dir, path)
                finally:
                    await self.rcon.command("save-on")
                await self.upload(await self.temp_upload_url(), path)
            except BaseException:
                self.dirty = True
                raise
            finally:
                if os.path.exists(path):
                    os.remove(path)
            await self.notify("event", event="temp_saved", size=size)
            return True

    async def temp_loop(self) -> None:
        while self.mc_running:
            await asyncio.sleep(self.temp_interval)
            if self.dirty:
                try:
                    await self.save_temp()
                except Exception:
                    log.exception("Temp snapshot failed")

    # ---- shutdown paths ----

    async def _shutdown(self, upload_url: str) -> bool:
        self.stopping = True
        async with self.save_lock:  # let a running snapshot finish first
            clean = await self.stop_minecraft()
            saved = False
            if clean:
                saved = await self.upload_world(upload_url)
        # The orchestrator destroys the instance next; leave on our own otherwise.
        self.spawn(self.exit_later(600, 0))
        return saved

    async def on_sigterm(self) -> None:
        if self.shutdown_task is not None:
            await asyncio.shield(self.shutdown_task)
            self.finish(0)
            return
        if self.stopping:
            return
        log.info("SIGTERM without an orchestrated stop: saving a temp snapshot")
        self.stopping = True
        temp_saved = False
        clean = await self.stop_minecraft()
        if clean and self.level:
            try:
                temp_saved = await self.upload_world(await self.temp_upload_url())
            except Exception:
                log.exception("Snapshot on SIGTERM failed")
        await self.notify("event", event="mc_exited", code=self.mc.returncode if self.mc else None,
                          reason="SIGTERM", temp_saved=temp_saved)
        self.finish(0)

    async def abandon(self) -> None:
        # The orchestrator no longer owns this session; it has already
        # recovered the world, so nothing is uploaded.
        self.stopping = True
        await self.stop_minecraft()
        self.finish(0)

    async def exit_later(self, seconds: int, code: int) -> None:
        await asyncio.sleep(seconds)
        self.finish(code)

    # ---- orchestrator requests ----

    async def cmd_rcon(self, msg: dict) -> dict:
        if not self.mc_running:
            raise RuntimeError("Minecraft is not running")
        return {"result": await self.rcon.command(str(msg["command"]), timeout=float(msg.get("timeout", 10)))}

    async def cmd_save(self, msg: dict) -> dict:
        if not await self.save_temp():
            raise RuntimeError("world is not ready or stopping")
        return {}

    async def cmd_shutdown(self, msg: dict) -> dict:
        # Idempotent: a reconnecting orchestrator asks again and gets the same result.
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(self._shutdown(msg["upload_url"]))
        return {"saved": await asyncio.shield(self.shutdown_task)}

    async def cmd_tunnel(self, msg: dict) -> dict:
        ticket = str(msg["ticket"])
        host, port = str(msg["addr"]).rsplit(":", 1)
        host = host.strip("[]")
        local_reader, local_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", SERVER_PORT), 5)
        pin = str(msg.get("tls_sha256") or "").lower()
        try:
            context = None
            if pin:
                # mcproxy's certificate is self-signed; the pin below replaces CA checks.
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port), ssl=context), 10)
        except BaseException:
            local_writer.close()
            raise
        if pin:
            cert = remote_writer.get_extra_info("ssl_object").getpeercert(binary_form=True) or b""
            if not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), pin):
                remote_writer.close()
                local_writer.close()
                raise ConnectionError("tunnel certificate does not match mcproxy's fingerprint")
        remote_writer.write(f"RTUN1 {ticket}\n".encode())
        await remote_writer.drain()
        self.spawn(splice(local_reader, local_writer, remote_reader, remote_writer))
        return {}

    # ---- newAuth relay ----

    async def start_auth_relay(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.relay_http)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", AUTH_RELAY_PORT).start()
        log.info("newAuth relay listening on 127.0.0.1:%s", AUTH_RELAY_PORT)

    async def relay_http(self, request: web.Request) -> web.Response:
        body = await request.read()
        try:
            reply = await self.request(
                "auth_http", timeout=15, method=request.method, path=request.path_qs,
                content_type=request.headers.get("Content-Type", ""),
                body=base64.b64encode(body).decode() if body else "")
        except Exception as e:
            return web.Response(status=502, text=f"relay: {e}")
        headers = {"Content-Type": reply["content_type"]} if reply.get("content_type") else None
        return web.Response(status=reply["status"], body=base64.b64decode(reply.get("body") or ""), headers=headers)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [agent] %(levelname)s %(message)s", stream=sys.stderr)
    sys.exit(asyncio.run(Agent().run()))


if __name__ == "__main__":
    main()
