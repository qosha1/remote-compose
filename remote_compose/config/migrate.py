"""Migrate rc.yml v1 → v2.

Lossless for every field currently in use by known consumers (ECS-only v1).
Unrecognized top-level or per-service fields produce warnings; migration
proceeds unless ``strict=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import v1_schema


@dataclass
class MigrationResult:
    v2: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    unmigratable: list[str] = field(default_factory=list)


def migrate(v1: dict[str, Any], strict: bool = False) -> MigrationResult:
    """Convert a v1 rc.yml dict to v2 and return the result + warnings.

    When strict=True, any field we cannot losslessly migrate raises ValueError
    instead of being appended to result.unmigratable.
    """
    warnings: list[str] = []
    unmigratable: list[str] = []

    for key in v1:
        if key not in v1_schema.V1_TOP_LEVEL_KEYS and key != "version":
            unmigratable.append(f"unknown top-level key: {key}")

    provider_config_ecs: dict[str, Any] = {}
    if "cluster" in v1:
        provider_config_ecs["cluster"] = v1["cluster"]
    if "region" in v1:
        provider_config_ecs["region"] = v1["region"]
    if "aws_profile" in v1:
        provider_config_ecs["aws_profile"] = v1["aws_profile"]
    if "vpc_cidr" in v1:
        provider_config_ecs["vpc_cidr"] = v1["vpc_cidr"]
    if "domain" in v1:
        provider_config_ecs["domain"] = v1["domain"]
    provider_config_ecs.setdefault("default_launch_type", "FARGATE")

    services_v2: dict[str, dict[str, Any]] = {}
    for name, raw in (v1.get("services") or {}).items():
        if not isinstance(raw, dict):
            warnings.append(f"service {name!r}: expected mapping, got {type(raw).__name__}")
            continue
        svc: dict[str, Any] = {}
        for key in v1_schema.V1_SERVICE_KEYS:
            if key in raw:
                svc[key] = raw[key]
        for extra_key in set(raw.keys()) - v1_schema.V1_SERVICE_KEYS:
            unmigratable.append(f"service {name!r}: unknown field {extra_key!r}")
        services_v2[name] = svc

    secrets_v2: list[dict[str, Any]] = []
    for item in v1.get("secrets") or []:
        if isinstance(item, str):
            secrets_v2.append({
                "name": _derive_secret_name(item),
                "source": "file",
                "path": item,
            })
        elif isinstance(item, dict) and "name" in item and "source" in item:
            secrets_v2.append(item)
        else:
            warnings.append(f"secret entry ignored (unrecognized shape): {item!r}")

    v2: dict[str, Any] = {
        "version": 2,
        "project": v1.get("project_name") or v1.get("project") or "",
        "compose_file": v1.get("compose_file", "docker-compose.yml"),
        "provider": "ecs",
        "provider_config": {"ecs": provider_config_ecs},
        "terraform": {
            "output_dir": "./terraform/${provider}",
            "backend": {"type": "local"},
        },
        "services": services_v2,
    }
    if secrets_v2:
        v2["secrets"] = secrets_v2
    if v1.get("backup"):
        v2["backup"] = v1["backup"]
    if v1.get("domain"):
        v2["domain"] = v1["domain"]

    if strict and unmigratable:
        raise ValueError(
            "strict migration failed — unmigratable fields: " + "; ".join(unmigratable)
        )

    return MigrationResult(v2=v2, warnings=warnings, unmigratable=unmigratable)


def _derive_secret_name(path: str) -> str:
    """Derive a stable secret name from its file path (last path component)."""
    stem = path.rstrip("/").rsplit("/", 1)[-1]
    return stem.lstrip(".") or "secret"
