"""rc.yml v2 — raw dict → validated RcConfigV2 parser.

Cross-instance validation lives here (e.g. duplicate-hostname detection
across services). Per-instance validation lives on the dataclasses
themselves in _schema_types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ._schema_types import (
    BackupConfig,
    ComposeConfig,
    ConfigError,
    LifecycleHookV2,
    RcConfigV2,
    SecretRefV2,
    ServiceV2,
    TerraformBackend,
    TerraformConfig,
    TlsConfig,
)


def _parse_backend(raw: dict[str, Any]) -> TerraformBackend:
    known = {"type", "bucket", "key", "region", "dynamodb_table"}
    extra = {k: v for k, v in raw.items() if k not in known}
    return TerraformBackend(
        type=raw.get("type", "local"),
        bucket=raw.get("bucket"),
        key=raw.get("key"),
        region=raw.get("region"),
        dynamodb_table=raw.get("dynamodb_table"),
        extra=extra,
    )


def _parse_terraform(raw: dict[str, Any]) -> TerraformConfig:
    return TerraformConfig(
        output_dir=raw.get("output_dir", "./terraform/${provider}"),
        backend=_parse_backend(raw.get("backend", {})),
    )


def _parse_lifecycle(svc_name: str, raw: dict[str, Any]) -> dict[str, LifecycleHookV2]:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"service {svc_name!r}: lifecycle must be a mapping of "
            f"hook-name → spec, got {type(raw).__name__}"
        )
    out: dict[str, LifecycleHookV2] = {}
    for hook_name, hook_raw in raw.items():
        if not isinstance(hook_raw, dict):
            raise ConfigError(
                f"service {svc_name!r}: lifecycle.{hook_name} must be a "
                f"mapping, got {type(hook_raw).__name__}"
            )
        cmd_raw = hook_raw.get("command")
        if cmd_raw is not None and not isinstance(cmd_raw, list):
            raise ConfigError(
                f"lifecycle hook {hook_name!r}: command must be a non-empty list, "
                f"got {type(cmd_raw).__name__}"
            )
        probe_raw = hook_raw.get("probe")
        if probe_raw is not None and not isinstance(probe_raw, list):
            raise ConfigError(
                f"lifecycle hook {hook_name!r}: probe must be a non-empty list[str], "
                f"got {type(probe_raw).__name__}"
            )
        hook = LifecycleHookV2(
            name=hook_name,
            command=list(cmd_raw or []),
            auto_on_deploy=bool(hook_raw.get("auto_on_deploy", False)),
            run_once=bool(hook_raw.get("run_once", False)),
            interactive=bool(hook_raw.get("interactive", False)),
            probe=list(probe_raw) if probe_raw else None,
        )
        hook.validate()
        out[hook_name] = hook
    return out


def _parse_service(name: str, raw: dict[str, Any]) -> ServiceV2:
    try:
        return ServiceV2(
            name=name,
            # cpu / memory default to 256 / 512 when omitted so partial
            # overrides (rc.yml entry that only sets type or public)
            # match the auto-import path's defaults — see
            # cli_v2.build_deploy_context.
            cpu=int(raw.get("cpu", 256)),
            memory=int(raw.get("memory", 512)),
            replicas=int(raw.get("replicas", 1)),
            type=raw.get("type", "application"),
            launch_type=raw.get("launch_type"),
            health_check_path=raw.get("health_check_path"),
            health_check_grace_period=raw.get("health_check_grace_period"),
            public=bool(raw.get("public", False)),
            port=raw.get("port"),
            ephemeral_storage=raw.get("ephemeral_storage"),
            default_target=bool(raw.get("default_target", False)),
            volumes=list(raw.get("volumes", [])),
            lifecycle=(
                _parse_lifecycle(name, raw["lifecycle"]) if raw.get("lifecycle") else {}
            ),
            domain=raw.get("domain"),
            # Preserve raw shape so validate() can flag non-list values.
            aliases=raw["aliases"] if "aliases" in raw else [],
            # rc-e5u.46.1: optional Dockerfile override.
            dockerfile=raw.get("dockerfile"),
            # Same — preserve raw shape for validate() to inspect.
            dev_volumes=raw["dev_volumes"] if "dev_volumes" in raw else [],
            # rc-e5u.46.4: extra env vars merged into the task def alongside
            # compose's ``environment:``. Coerce scalars to str so YAML
            # booleans (DJANGO_DEBUG: False) and ints become valid env
            # values. Non-scalar values are caught by ServiceV2.validate.
            env=(
                {
                    str(k): (str(v) if not isinstance(v, (dict, list)) else v)
                    for k, v in raw["env"].items()
                }
                if isinstance(raw.get("env"), dict)
                else (raw["env"] if "env" in raw else {})
            ),
            framework=raw.get("framework"),
        )
    except KeyError as e:
        raise ConfigError(f"service {name!r}: missing required field {e.args[0]!r}")


def _parse_secret(raw: dict[str, Any]) -> SecretRefV2:
    if "name" not in raw or "source" not in raw:
        raise ConfigError(f"secret entry missing name or source: {raw!r}")
    return SecretRefV2(
        name=raw["name"],
        source=raw["source"],
        path=raw.get("path"),
        arn=raw.get("arn"),
        ref=raw.get("ref"),
    )


def parse(raw: dict[str, Any]) -> RcConfigV2:
    """Parse a rc.yml v2 dict into a validated RcConfigV2."""
    if not isinstance(raw, dict):
        raise ConfigError(f"rc.yml v2 must be a mapping, got {type(raw).__name__}")

    services_raw = raw.get("services", {}) or {}
    services = {n: _parse_service(n, s) for n, s in services_raw.items()}

    # Per-service validation before cross-service checks so we surface the
    # most specific error first (e.g. "aliases must be a list" beats
    # "duplicate hostname" when aliases is mistakenly a string).
    for svc in services.values():
        svc.validate()

    # Cross-service uniqueness: two services can't claim the same hostname,
    # whether as a primary domain or as an alias of either.
    seen_hostnames: dict[str, str] = {}
    for svc in services.values():
        candidates = [svc.domain] if svc.domain else []
        candidates.extend(svc.aliases or [])
        for host in candidates:
            existing = seen_hostnames.get(host)
            if existing:
                raise ConfigError(
                    f"duplicate hostname {host!r}: claimed by both "
                    f"service {existing!r} and {svc.name!r}"
                )
            seen_hostnames[host] = svc.name

    secrets_raw = raw.get("secrets", []) or []
    secrets = [_parse_secret(s) for s in secrets_raw]

    backup = None
    if raw.get("backup"):
        backup = BackupConfig(
            bucket=raw["backup"].get("bucket"),
            service=raw["backup"].get("service"),
            bucket_managed=bool(raw["backup"].get("bucket_managed", True)),
            retention_days=(
                None
                if raw["backup"].get("retention_days") in (None, "never", 0)
                else int(raw["backup"]["retention_days"])
            ),
        )

    tls = None
    if raw.get("tls"):
        tls = TlsConfig(
            mode=raw["tls"].get("mode", "acm"),
            certificate_arn=raw["tls"].get("certificate_arn"),
        )

    compose_cfg = None
    if raw.get("compose"):
        cb = raw["compose"]
        if not isinstance(cb, dict):
            raise ConfigError(f"compose must be a mapping, got {type(cb).__name__}")
        unknown = set(cb.keys()) - {"include", "exclude"}
        if unknown:
            raise ConfigError(
                f"unknown compose keys: {sorted(unknown)} "
                f"(supported: include, exclude)"
            )
        compose_cfg = ComposeConfig(
            include=cb.get("include"),
            exclude=cb.get("exclude"),
        )

    cfg = RcConfigV2(
        version=int(raw.get("version", 0)),
        project=raw.get("project", ""),
        compose_file=raw.get("compose_file", ""),
        provider=raw.get("provider", ""),
        provider_config=raw.get("provider_config", {}) or {},
        terraform=_parse_terraform(raw.get("terraform", {}) or {}),
        services=services,
        secrets=secrets,
        backup=backup,
        domain=raw.get("domain"),
        tls=tls,
        compose=compose_cfg,
    )
    cfg.validate()
    return cfg


def load(path: str | Path) -> RcConfigV2:
    """Load an rc.yml v2 file from disk and return a validated RcConfigV2."""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return parse(raw)
