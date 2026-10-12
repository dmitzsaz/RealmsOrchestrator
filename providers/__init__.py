import importlib

from .base import (  # noqa: F401
    ConnectMode,
    Instance,
    InstanceState,
    InstanceStatus,
    LaunchSpec,
    Provider,
    ProviderError,
)

# Imported lazily: a provider's SDK and credentials are needed only when it
# is listed in ENABLED_PROVIDERS.
REGISTRY = {
    "baremetal": "providers.baremetal:BaremetalProvider",
    "proxmoxlxc": "providers.proxmoxlxc:ProxmoxLXCProvider",
    "northflank": "providers.northflank:NorthflankProvider",
    "awsfargate": "providers.awsfargate:AWSFargateProvider",
}


def load_provider(name: str) -> Provider:
    if name not in REGISTRY:
        raise ProviderError(f"Unknown provider {name!r}; known: {', '.join(REGISTRY)}")
    module_name, class_name = REGISTRY[name].split(":")
    return getattr(importlib.import_module(module_name), class_name)()


def load_providers(names: list[str]) -> dict[str, Provider]:
    return {name: load_provider(name) for name in names}
