"""Northflank deployment services.

Only HTTP ports can be public on Northflank (TCP needs a paid L4 load
balancer), so worlds are reached through the agent's reverse tunnel by
default. The API allows 1000 requests/hour: the orchestrator calls inspect()
only while booting or when the agent stops sending heartbeats.
"""
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


class NorthflankSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="NORTHFLANK_", extra="ignore")

    API_URL: str = "https://api.northflank.com"
    API_TOKEN: str = ""
    PROJECT_ID: str = ""
    DEPLOYMENT_PLAN: str = "nf-compute-200"   # GET /v1/plans lists them
    REGISTRY_CREDENTIALS: str = ""             # saved credentials ID for private images
    EPHEMERAL_STORAGE_MB: int = 10240
    GRACE_PERIOD_SECONDS: int = 120            # SIGTERM -> SIGKILL, lets the agent upload temp
    # Safety net: Northflank pauses and deletes the service after this many
    # seconds even if the orchestrator is gone (300..604800, 0 = off).
    EXPIRY_SECONDS: int = 0


STATUS = {
    "PENDING": InstanceStatus.PENDING,
    "IN_PROGRESS": InstanceStatus.PENDING,
    "COMPLETED": InstanceStatus.RUNNING,
    "FAILED": InstanceStatus.STOPPED,
}


class NorthflankProvider(Provider):
    name = "northflank"
    settings_class = NorthflankSettings
    default_connect_mode = ConnectMode.TUNNEL

    def __init__(self, settings=None):
        super().__init__(settings)
        if not (self.settings.API_TOKEN and self.settings.PROJECT_ID):
            raise ProviderError("NORTHFLANK_API_TOKEN and NORTHFLANK_PROJECT_ID are required")
        self.base = f"{self.settings.API_URL.rstrip('/')}/v1/projects/{self.settings.PROJECT_ID}/services"
        self.session = None

    def _http(self) -> aiohttp.ClientSession:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.settings.API_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self.session

    async def _api(self, method: str, path: str, json=None):
        async with self._http().request(method, self.base + path, json=json) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = None
            if resp.status == 404:
                return None
            if resp.status >= 400:
                error = (body or {}).get("error") or {}
                message = error.get("message") if isinstance(error, dict) else error
                raise ProviderError(f"Northflank {method} {path or '/'}: HTTP {resp.status} {message or resp.reason}")
            return (body or {}).get("data")

    async def launch(self, spec: LaunchSpec) -> Instance:
        s = self.settings
        external = {"imagePath": spec.image}
        if s.REGISTRY_CREDENTIALS:
            external["credentials"] = s.REGISTRY_CREDENTIALS
        body = {
            # 3-54 chars, letters/digits separated by single hyphens.
            "name": f"mc-{spec.world_id}-{spec.session_id[:8]}",
            "description": f"RealmsOrchestrator world {spec.world_id}",
            "billing": {"deploymentPlan": s.DEPLOYMENT_PLAN},
            "deployment": {
                "instances": 1,
                "external": external,
                "storage": {"ephemeralStorage": {"storageSize": s.EPHEMERAL_STORAGE_MB}},
                "gracePeriodSeconds": s.GRACE_PERIOD_SECONDS,
            },
            # Private port: reachable inside the project only, never publicly.
            "ports": [{"name": "mc", "internalPort": MINECRAFT_PORT, "public": False, "protocol": "TCP"}],
            "runtimeEnvironment": {k: str(v) for k, v in spec.env.items()},
        }
        if s.EXPIRY_SECONDS:
            body["createOptions"] = {"expiryTime": s.EXPIRY_SECONDS}
        data = await self._api("POST", "/deployment", json=body)
        if not data or not data.get("id"):
            raise ProviderError("Northflank did not return a service ID")
        return Instance(instance_id=data["id"], ports={"private": MINECRAFT_PORT, "public": None})

    async def inspect(self, instance_id: str) -> InstanceState:
        data = await self._api("GET", f"/{instance_id}")
        if data is None:
            return InstanceState(InstanceStatus.MISSING)
        deployment = (data.get("status") or {}).get("deployment") or {}
        status = deployment.get("status", "")
        return InstanceState(STATUS.get(status, InstanceStatus.PENDING), detail=status)

    async def destroy(self, instance_id: str) -> None:
        await self._api("DELETE", f"/{instance_id}")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
