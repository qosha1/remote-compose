"""Provider package: pluggable multi-cloud deployers behind a shared contract.

See :mod:`remote_compose.provider.base` for the Provider ABC.
"""

from .base import (
    DeployContext,
    DeployResult,
    ExecResult,
    PlanResult,
    Provider,
    ProviderConfigError,
    ProviderError,
    ProviderNotFoundError,
    ProviderTimeoutError,
    SecretRef,
    ServiceSpec,
    ServiceStatus,
    StatusReport,
)

_REGISTRY: dict[str, type[Provider]] = {}


def register(name: str, cls: type[Provider]) -> None:
    _REGISTRY[name] = cls


def get(name: str) -> type[Provider]:
    if name not in _REGISTRY:
        raise ProviderNotFoundError(f"no provider registered under '{name}'")
    return _REGISTRY[name]


def available() -> list[str]:
    return sorted(_REGISTRY.keys())


__all__ = [
    "DeployContext",
    "DeployResult",
    "ExecResult",
    "PlanResult",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderTimeoutError",
    "SecretRef",
    "ServiceSpec",
    "ServiceStatus",
    "StatusReport",
    "register",
    "get",
    "available",
]
