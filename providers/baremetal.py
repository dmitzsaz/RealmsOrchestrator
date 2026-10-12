"""Docker on a host we control (local socket or DOCKER_HOST / BAREMETAL_DOCKER_HOST).

Container names stay minecraft_{id}_{domainPrefix}, so the current mcproxy,
which resolves backends through Docker, keeps working unchanged.
"""
import asyncio

import docker
from docker.errors import APIError, NotFound
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


class BaremetalSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="BAREMETAL_", extra="ignore")

    DOCKER_HOST: str = ""      # empty: docker.from_env()
    NETWORK: str = ""          # put worlds on mcproxy's network so it can dial them
    PUBLIC_IP: str = ""        # host address reported as ips.public
    CPU: float = 0             # 0 = no limit (previous behaviour)
    MEMORY_MB: int = 0
    STOP_TIMEOUT: int = 30


class BaremetalProvider(Provider):
    name = "baremetal"
    settings_class = BaremetalSettings
    default_connect_mode = ConnectMode.PRIVATE

    def __init__(self, settings=None):
        super().__init__(settings)
        if self.settings.DOCKER_HOST:
            self.client = docker.DockerClient(base_url=self.settings.DOCKER_HOST)
        else:
            self.client = docker.from_env()

    def _placement(self, container):
        net = container.attrs.get("NetworkSettings", {})
        private_ip = None
        networks = net.get("Networks") or {}
        if self.settings.NETWORK and networks.get(self.settings.NETWORK, {}).get("IPAddress"):
            private_ip = networks[self.settings.NETWORK]["IPAddress"]
        else:
            private_ip = next((n["IPAddress"] for n in networks.values() if n.get("IPAddress")), None)
        bindings = (net.get("Ports") or {}).get(f"{MINECRAFT_PORT}/tcp") or []
        public_port = int(bindings[0]["HostPort"]) if bindings and bindings[0].get("HostPort") else None
        ips = {"private": private_ip, "public": self.settings.PUBLIC_IP or None}
        ports = {"private": MINECRAFT_PORT, "public": public_port}
        return ips, ports

    def _launch(self, spec: LaunchSpec) -> Instance:
        try:
            old = self.client.containers.get(spec.name)
        except NotFound:
            old = None
        if old is not None:
            if old.status in ("running", "restarting"):
                raise ProviderError(f"Container {spec.name} is already running outside this session")
            old.remove(force=True)

        kwargs = {}
        if self.settings.NETWORK:
            kwargs["network"] = self.settings.NETWORK
        if spec.memory_mb:
            kwargs["mem_limit"] = f"{spec.memory_mb}m"
        if spec.cpu:
            kwargs["nano_cpus"] = int(spec.cpu * 1e9)
        container = self.client.containers.run(
            spec.image,
            name=spec.name,
            detach=True,
            environment=spec.env,
            # None publishes on a random free host port.
            ports={f"{MINECRAFT_PORT}/tcp": None} if spec.expose_minecraft else {},
            labels=spec.labels,
            extra_hosts={"host.docker.internal": "host-gateway"},
            **kwargs,
        )
        container.reload()
        ips, ports = self._placement(container)
        return Instance(instance_id=container.id, ips=ips, ports=ports)

    async def launch(self, spec: LaunchSpec) -> Instance:
        try:
            return await asyncio.to_thread(self._launch, spec)
        except APIError as e:
            raise ProviderError(f"Docker: {e.explanation or e}") from e

    def _inspect(self, instance_id: str) -> InstanceState:
        try:
            container = self.client.containers.get(instance_id)
        except NotFound:
            return InstanceState(InstanceStatus.MISSING)
        status = {
            "running": InstanceStatus.RUNNING,
            "created": InstanceStatus.PENDING,
            "restarting": InstanceStatus.PENDING,
        }.get(container.status, InstanceStatus.STOPPED)
        ips, ports = self._placement(container)
        return InstanceState(status, ips, ports, detail=container.status)

    async def inspect(self, instance_id: str) -> InstanceState:
        return await asyncio.to_thread(self._inspect, instance_id)

    def _destroy(self, instance_id: str) -> None:
        try:
            container = self.client.containers.get(instance_id)
        except NotFound:
            return
        try:
            container.stop(timeout=self.settings.STOP_TIMEOUT)
        except APIError:
            pass
        try:
            container.remove(force=True)
        except NotFound:
            pass

    async def destroy(self, instance_id: str) -> None:
        await asyncio.to_thread(self._destroy, instance_id)

    async def close(self) -> None:
        self.client.close()
