"""Proxmox VE application containers (OCI images as LXC, PVE 9.1+).

The runtime image is pulled into PROXMOX_TEMPLATE_STORAGE with
oci-registry-pull and used as ostemplate. PVE overwrites the env passed to
create with the image's ENV, so the container is created stopped, our
variables are merged into its env config, and only then is it started.
The API cannot exec inside an LXC; the agent dials out instead.

The API token needs VM.Allocate, VM.Config.*, VM.PowerMgmt, VM.Audit on the
containers, Datastore.AllocateSpace/AllocateTemplate on both storages and
Sys.AccessNetwork on the node (for the registry pull).
"""
import asyncio
import math
import re
import ssl
from urllib.parse import quote

import aiohttp
from pydantic_settings import SettingsConfigDict

from .base import (
    MINECRAFT_PORT,
    ConnectMode,
    Instance,
    InstanceState,
    InstanceStatus,
    LaunchSpec,
    Provider,
    ProviderError,
    ProviderSettings,
)


class ProxmoxSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="PROXMOX_", extra="ignore")

    API_URL: str = ""             # https://pve.example:8006
    TOKEN_ID: str = ""            # user@realm!tokenid
    TOKEN_SECRET: str = ""
    VERIFY_TLS: bool = True
    CA_FILE: str = ""             # e.g. a copy of /etc/pve/pve-root-ca.pem
    NODE: str = ""
    TEMPLATE_STORAGE: str = "local"      # file based, with vztmpl content
    ROOTFS_STORAGE: str = "local-lvm"
    DISK_GB: int = 16
    NET0: str = "name=eth0,bridge=vmbr0,ip=dhcp"
    UNPRIVILEGED: bool = True
    # Re-pull the image once per orchestrator start (mutable tags such as :dev).
    REPULL_IMAGE: bool = False
    TASK_TIMEOUT: int = 600


def _missing(error: Exception) -> bool:
    return "does not exist" in str(error)


class ProxmoxLXCProvider(Provider):
    name = "proxmoxlxc"
    settings_class = ProxmoxSettings
    default_connect_mode = ConnectMode.PRIVATE

    def __init__(self, settings=None):
        super().__init__(settings)
        s = self.settings
        if not (s.API_URL and s.TOKEN_ID and s.TOKEN_SECRET and s.NODE):
            raise ProviderError("PROXMOX_API_URL, PROXMOX_TOKEN_ID, PROXMOX_TOKEN_SECRET and PROXMOX_NODE are required")
        self.base = s.API_URL.rstrip("/") + "/api2/json"
        self.session = None
        self.template_lock = asyncio.Lock()
        self.template = None

    def _http(self) -> aiohttp.ClientSession:
        if self.session is None:
            if self.settings.CA_FILE:
                tls = ssl.create_default_context(cafile=self.settings.CA_FILE)
            else:
                tls = None if self.settings.VERIFY_TLS else False
            self.session = aiohttp.ClientSession(
                headers={"Authorization": f"PVEAPIToken={self.settings.TOKEN_ID}={self.settings.TOKEN_SECRET}"},
                connector=aiohttp.TCPConnector(ssl=tls),
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self.session

    async def _api(self, method: str, path: str, data=None, params=None):
        async with self._http().request(method, self.base + path, data=data, params=params) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = None
            if resp.status >= 400:
                message = (body or {}).get("message") or (body or {}).get("errors") or resp.reason
                raise ProviderError(f"Proxmox {method} {path}: HTTP {resp.status} {message}")
            return (body or {}).get("data")

    async def _wait_task(self, upid: str) -> None:
        node = upid.split(":")[1]  # UPID:{node}:{pid}:...
        deadline = asyncio.get_running_loop().time() + self.settings.TASK_TIMEOUT
        while True:
            task = await self._api("GET", f"/nodes/{node}/tasks/{quote(upid, safe='')}/status")
            if task.get("status") == "stopped":
                exit_status = task.get("exitstatus", "")
                if exit_status == "OK" or exit_status.startswith("WARNINGS"):
                    return
                raise ProviderError(f"Proxmox task {upid} failed: {exit_status}")
            if asyncio.get_running_loop().time() > deadline:
                raise ProviderError(f"Proxmox task {upid} timed out")
            await asyncio.sleep(1)

    async def _ensure_template(self) -> str:
        async with self.template_lock:
            if self.template:
                return self.template
            node, storage = self.settings.NODE, self.settings.TEMPLATE_STORAGE
            # PVE stores ghcr.io/org/realms-runtime:dev as realms-runtime_dev.tar.
            filename = re.sub(r"[^A-Za-z0-9_.-]", "_", self.image.rsplit("/", 1)[-1]) + ".tar"
            volid = f"{storage}:vztmpl/{filename}"
            content = await self._api("GET", f"/nodes/{node}/storage/{storage}/content", params={"content": "vztmpl"})
            exists = any(item.get("volid") == volid for item in content or [])
            if exists and self.settings.REPULL_IMAGE:
                # A pull refuses to overwrite an existing file.
                await self._api("DELETE", f"/nodes/{node}/storage/{storage}/content/{quote(volid, safe='')}")
                exists = False
            if not exists:
                upid = await self._api("POST", f"/nodes/{node}/storage/{storage}/oci-registry-pull",
                                       data={"reference": self.image})
                await self._wait_task(upid)
            self.template = volid
            return volid

    @staticmethod
    def _split(instance_id: str):
        node, vmid = instance_id.split("/", 1)
        return node, vmid

    async def launch(self, spec: LaunchSpec) -> Instance:
        s = self.settings
        node = s.NODE
        ostemplate = await self._ensure_template()
        hostname = re.sub(r"[^a-z0-9-]", "-", spec.name.lower()).strip("-")[:63] or f"world-{spec.world_id}"
        create = {
            "ostemplate": ostemplate,
            "hostname": hostname,
            "memory": spec.memory_mb or s.MEMORY_MB,
            "swap": 0,
            "cores": max(1, math.ceil(spec.cpu or s.CPU)),
            "rootfs": f"{s.ROOTFS_STORAGE}:{s.DISK_GB}",
            "net0": s.NET0,
            "unprivileged": int(s.UNPRIVILEGED),
            "tags": f"realms;world-{spec.world_id}",
            "description": f"RealmsOrchestrator world {spec.world_id}, session {spec.session_id}",
            "start": 0,
        }
        for _ in range(5):
            # nextid is only free at the moment of the check; retry on races.
            vmid = await self._api("GET", "/cluster/nextid")
            try:
                upid = await self._api("POST", f"/nodes/{node}/lxc", data={**create, "vmid": vmid})
                break
            except ProviderError as e:
                if "already exists" not in str(e):
                    raise
        else:
            raise ProviderError("Could not allocate a Proxmox VMID")
        await self._wait_task(upid)
        instance_id = f"{node}/{vmid}"

        try:
            config = await self._api("GET", f"/nodes/{node}/lxc/{vmid}/config")
            raw = config.get("env") or ""
            entries = raw if isinstance(raw, list) else [e for e in raw.split("\0") if e]
            env = dict(entry.split("=", 1) for entry in entries if "=" in entry)
            env.update({k: str(v) for k, v in spec.env.items()})
            await self._api("PUT", f"/nodes/{node}/lxc/{vmid}/config",
                            data={"env": "\0".join(f"{k}={v}" for k, v in env.items())})
            await self._wait_task(await self._api("POST", f"/nodes/{node}/lxc/{vmid}/status/start"))
        except Exception:
            await self.destroy(instance_id)
            raise
        # The address is known only once the container runs (see inspect).
        return Instance(instance_id=instance_id, ports={"private": MINECRAFT_PORT, "public": None})

    async def _address(self, node: str, vmid: str):
        try:
            interfaces = await self._api("GET", f"/nodes/{node}/lxc/{vmid}/interfaces")
        except ProviderError:
            return None
        for iface in interfaces or []:
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses") or []:
                if addr.get("ip-address-type") == "inet":
                    return addr.get("ip-address")
            if iface.get("inet"):
                return iface["inet"].split("/", 1)[0]
        return None

    async def inspect(self, instance_id: str) -> InstanceState:
        node, vmid = self._split(instance_id)
        try:
            current = await self._api("GET", f"/nodes/{node}/lxc/{vmid}/status/current")
        except ProviderError as e:
            if _missing(e):
                return InstanceState(InstanceStatus.MISSING)
            raise
        if current.get("status") != "running":
            return InstanceState(InstanceStatus.STOPPED, detail=current.get("status", ""))
        ip = await self._address(node, vmid)
        return InstanceState(
            InstanceStatus.RUNNING if ip else InstanceStatus.PENDING,
            ips={"private": ip, "public": None},
            ports={"private": MINECRAFT_PORT, "public": None},
        )

    async def destroy(self, instance_id: str) -> None:
        node, vmid = self._split(instance_id)
        try:
            upid = await self._api("DELETE", f"/nodes/{node}/lxc/{vmid}",
                                   params={"purge": 1, "destroy-unreferenced-disks": 1, "force": 1})
        except ProviderError as e:
            if _missing(e):
                return
            raise
        await self._wait_task(upid)

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
