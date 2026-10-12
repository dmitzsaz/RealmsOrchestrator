import hashlib

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()


class Settings(BaseSettings):
    LISTEN_ADDR: list[str] = ["0.0.0.0", "::"]
    LISTEN_PORT: int = 8080

    BASE_DOMAIN: str = "undefined"
    OFFLINEMODE_ALTWHITELIST: bool = False

    DB_USER_NAME: str = "root"
    DB_ROOT_PASSWORD: str
    DB_NAME: str = "realms"
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306

    R2_ENDPOINT: str
    R2_ACCESS_KEY: str
    R2_SECRET_KEY: str
    R2_BUCKET: str

    # Providers. ENABLED_PROVIDERS is comma separated; only these are loaded,
    # so credentials are required only for providers that are actually used.
    DEFAULT_PROVIDER: str = "baremetal"
    ENABLED_PROVIDERS: str = "baremetal"
    # itzg/minecraft-server + the node agent (see agent/Dockerfile).
    RUNTIME_IMAGE: str = "ghcr.io/dmitzsaz/realms-runtime:dev"

    # Agents connect here (WebSocket). This is the only listener that has to be
    # reachable from remote providers; the management API stays internal.
    AGENT_LISTEN_PORT: int = 8081
    # How agents reach AGENT_LISTEN_PORT; providers may override it.
    ORCHESTRATOR_PUBLIC_URL: str = "http://host.docker.internal:8081"
    # Signs per-session agent tokens. Derived from R2_SECRET_KEY when empty.
    AGENT_SECRET: str = ""

    MCPROXY_AUTH_URL: str = "http://mcproxy:8080"
    # mcproxy's AUTH_PUBLIC_URL, rewritten in relayed /discovery responses.
    MCPROXY_AUTH_PUBLIC_URL: str = ""

    # Shared with mcproxy for /internal/*. The internal API is disabled when empty.
    INTERNAL_API_TOKEN: str = ""
    # mcproxy's reverse tunnel listener as reachable from the nodes (host:port).
    PROXY_TUNNEL_ADDR: str = ""
    PROXY_TUNNEL_TLS: bool = False

    # Crash-safety snapshots uploaded to R2 under temp/. 0 disables them.
    TEMP_SAVE_INTERVAL_MINUTES: int = 10
    EMPTY_STOP_MINUTES: int = 5
    WORLD_BOOT_TIMEOUT: int = 900
    # Agent silent this long -> ask the provider; this long -> treat as lost.
    AGENT_HEARTBEAT_TIMEOUT: int = 60
    AGENT_LOST_TIMEOUT: int = 300
    # Graceful stop: Minecraft shutdown + final zip/upload.
    WORLD_STOP_TIMEOUT: int = 1200

    @property
    def enabled_providers(self) -> list[str]:
        return [p.strip() for p in self.ENABLED_PROVIDERS.split(",") if p.strip()]

    @property
    def agent_secret(self) -> bytes:
        if self.AGENT_SECRET:
            return self.AGENT_SECRET.encode()
        return hashlib.sha256(b"realms-agent:" + self.R2_SECRET_KEY.encode()).digest()


settings = Settings()
