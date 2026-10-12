"""Provider contract.

A provider only places and removes one container running RUNTIME_IMAGE
(itzg/minecraft-server + the node agent). World files, backups, RCON and
reverse tunnels are handled by the agent over its outbound control channel,
so the orchestrator never needs to reach into a provider's network.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from config import settings as global_settings

MINECRAFT_PORT = 25565


class ProviderError(Exception):
    pass


class ConnectMode(str, Enum):
    PRIVATE = "private"  # mcproxy dials ips.private:ports.private
    PUBLIC = "public"    # mcproxy dials ips.public:ports.public
    TUNNEL = "tunnel"    # the agent dials mcproxy's tunnel listener


@dataclass
class LaunchSpec:
    world_id: int
    session_id: str
    name: str                 # minecraft_{id}_{domainPrefix}; mcproxy resolves Docker by it
    image: str
    env: dict[str, str]
    cpu: float                # vCPU, 0 = provider default / unlimited
    memory_mb: int            # 0 = provider default / unlimited
    expose_minecraft: bool    # players may connect without mcproxy
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Instance:
    instance_id: str
    ips: Optional[dict] = None    # {"private": "10.0.0.5", "public": None}
    ports: Optional[dict] = None  # {"private": 25565, "public": None}


class InstanceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"


@dataclass
class InstanceState:
    status: InstanceStatus
    ips: Optional[dict] = None
    ports: Optional[dict] = None
    detail: str = ""

    @property
    def alive(self) -> bool:
        return self.status in (InstanceStatus.PENDING, InstanceStatus.RUNNING)


class ProviderSettings(BaseSettings):
    """Fields shared by every provider; each subclass sets its env_prefix."""
    model_config = SettingsConfigDict(extra="ignore")

    ORCHESTRATOR_URL: str = ""   # agent -> orchestrator, default ORCHESTRATOR_PUBLIC_URL
    CONNECT_MODE: str = ""       # private | public | tunnel, default per provider
    RUNTIME_IMAGE: str = ""      # default RUNTIME_IMAGE
    # newAuth session API as reachable from the node. Empty: the agent relays
    # these requests through its control channel to MCPROXY_AUTH_URL.
    MCPROXY_AUTH_URL: str = ""
    CPU: float = 2
    MEMORY_MB: int = 4096


def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Provider(ABC):
    name: ClassVar[str]
    settings_class: ClassVar[type[ProviderSettings]] = ProviderSettings
    default_connect_mode: ClassVar[ConnectMode] = ConnectMode.PRIVATE

    def __init__(self, settings: Optional[ProviderSettings] = None):
        self.settings = settings or self.settings_class()

    @property
    def connect_mode(self) -> ConnectMode:
        return ConnectMode(self.settings.CONNECT_MODE or self.default_connect_mode)

    @property
    def orchestrator_url(self) -> str:
        return (self.settings.ORCHESTRATOR_URL or global_settings.ORCHESTRATOR_PUBLIC_URL).rstrip("/")

    @property
    def image(self) -> str:
        return self.settings.RUNTIME_IMAGE or global_settings.RUNTIME_IMAGE

    @property
    def auth_url(self) -> Optional[str]:
        return self.settings.MCPROXY_AUTH_URL.rstrip("/") or None

    def direct_address(self, ips: Optional[dict], ports: Optional[dict]) -> Optional[str]:
        """host:port mcproxy should dial, or None in tunnel mode."""
        if self.connect_mode == ConnectMode.TUNNEL:
            return None
        key = self.connect_mode.value
        ip = (ips or {}).get(key)
        port = (ports or {}).get(key)
        if not ip or not port:
            return None
        return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"

    @abstractmethod
    async def launch(self, spec: LaunchSpec) -> Instance:
        """Starts the container. Must not wait for Minecraft to be ready."""

    @abstractmethod
    async def inspect(self, instance_id: str) -> InstanceState:
        """Current state. Unknown/deleted instances are MISSING, not errors."""

    @abstractmethod
    async def destroy(self, instance_id: str) -> None:
        """Removes the instance and its disk. Idempotent."""

    async def close(self) -> None:
        pass
