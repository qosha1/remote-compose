"""rc.yml v2 schema — dataclasses with manual validation.

Avoids pydantic so the core package stays dep-light (NFR-3). Validation is
strict on required fields, lenient on extras (forward-compatible with
provider_config sub-keys a future provider may add).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(ValueError):
    """Raised when an rc.yml v2 document fails validation."""


VALID_SERVICE_TYPES = {"application", "worker", "infrastructure", "proxy"}
VALID_LAUNCH_TYPES = {"FARGATE", "EC2"}
VALID_CAPACITY_TYPES = {"ON_DEMAND", "SPOT", "MIXED"}
VALID_SECRET_SOURCES = {"file", "aws_sm", "k8s_secret", "gcp_sm"}
VALID_TLS_MODES = {"acm", "cert-manager", "manual"}


@dataclass
class LifecycleHookV2:
    """A named one-shot command that runs inside a service container.

    Examples: 'migrate' (django manage.py migrate), 'createsuperuser',
    'seed' (rails db:seed), 'shell' (interactive REPL).
    """
    name: str
    command: list[str]
    auto_on_deploy: bool = False
    run_once: bool = False
    interactive: bool = False
    probe: Optional[list[str]] = None  # used by run_once: nonzero = "not yet run"

    def validate(self) -> None:
        if not isinstance(self.command, list) or not self.command:
            raise ConfigError(
                f"lifecycle hook {self.name!r}: command must be a non-empty list"
            )
        if not all(isinstance(c, str) for c in self.command):
            raise ConfigError(
                f"lifecycle hook {self.name!r}: command must be list[str]"
            )
        if self.run_once and not self.probe:
            raise ConfigError(
                f"lifecycle hook {self.name!r}: run_once requires a probe command "
                f"(returns nonzero = 'not yet run')"
            )
        if self.probe is not None and (
            not isinstance(self.probe, list) or not self.probe
            or not all(isinstance(c, str) for c in self.probe)
        ):
            raise ConfigError(
                f"lifecycle hook {self.name!r}: probe must be a non-empty list[str]"
            )
        if self.auto_on_deploy and self.interactive:
            raise ConfigError(
                f"lifecycle hook {self.name!r}: auto_on_deploy hooks cannot be "
                f"interactive (no human at deploy time)"
            )


@dataclass
class ServiceV2:
    name: str
    cpu: int
    memory: int
    replicas: int = 1
    type: str = "application"
    launch_type: Optional[str] = None
    health_check_path: Optional[str] = None
    public: bool = False
    port: Optional[int] = None
    ephemeral_storage: Optional[int] = None
    default_target: bool = False
    volumes: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: dict[str, LifecycleHookV2] = field(default_factory=dict)

    def validate(self) -> None:
        if self.type not in VALID_SERVICE_TYPES:
            raise ConfigError(
                f"service {self.name!r}: type must be one of "
                f"{sorted(VALID_SERVICE_TYPES)}, got {self.type!r}"
            )
        if self.launch_type is not None and self.launch_type not in VALID_LAUNCH_TYPES:
            raise ConfigError(
                f"service {self.name!r}: launch_type must be one of "
                f"{sorted(VALID_LAUNCH_TYPES)}, got {self.launch_type!r}"
            )
        if self.public and self.port is None:
            raise ConfigError(
                f"service {self.name!r}: public=true requires a port"
            )


@dataclass
class SecretRefV2:
    name: str
    source: str
    path: Optional[str] = None
    arn: Optional[str] = None
    ref: Optional[str] = None

    def validate(self) -> None:
        if self.source not in VALID_SECRET_SOURCES:
            raise ConfigError(
                f"secret {self.name!r}: source must be one of "
                f"{sorted(VALID_SECRET_SOURCES)}, got {self.source!r}"
            )
        if self.source == "file" and not self.path:
            raise ConfigError(f"secret {self.name!r}: source=file requires path")
        if self.source == "aws_sm" and not self.arn:
            raise ConfigError(f"secret {self.name!r}: source=aws_sm requires arn")
        if self.source in {"k8s_secret", "gcp_sm"} and not self.ref:
            raise ConfigError(f"secret {self.name!r}: source={self.source} requires ref")


@dataclass
class TerraformBackend:
    type: str = "local"
    bucket: Optional[str] = None
    key: Optional[str] = None
    region: Optional[str] = None
    dynamodb_table: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TerraformConfig:
    output_dir: str = "./terraform/${provider}"
    backend: TerraformBackend = field(default_factory=TerraformBackend)


@dataclass
class BackupConfig:
    bucket: Optional[str] = None
    service: Optional[str] = None


@dataclass
class TlsConfig:
    mode: str = "acm"
    certificate_arn: Optional[str] = None

    def validate(self) -> None:
        if self.mode not in VALID_TLS_MODES:
            raise ConfigError(
                f"tls.mode must be one of {sorted(VALID_TLS_MODES)}, got {self.mode!r}"
            )
        if self.mode == "manual" and not self.certificate_arn:
            raise ConfigError("tls.mode=manual requires certificate_arn")


@dataclass
class RcConfigV2:
    version: int
    project: str
    compose_file: str
    provider: str
    provider_config: dict[str, Any] = field(default_factory=dict)
    terraform: TerraformConfig = field(default_factory=TerraformConfig)
    services: dict[str, ServiceV2] = field(default_factory=dict)
    secrets: list[SecretRefV2] = field(default_factory=list)
    backup: Optional[BackupConfig] = None
    domain: Optional[str] = None
    tls: Optional[TlsConfig] = None

    def validate(self) -> None:
        if self.version != 2:
            raise ConfigError(f"version must be 2, got {self.version!r}")
        if not self.project:
            raise ConfigError("project is required")
        if not self.compose_file:
            raise ConfigError("compose_file is required")
        if not self.provider:
            raise ConfigError("provider is required")
        for svc in self.services.values():
            svc.validate()
        for sec in self.secrets:
            sec.validate()
        if self.tls is not None:
            self.tls.validate()


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
            cpu=int(raw["cpu"]),
            memory=int(raw["memory"]),
            replicas=int(raw.get("replicas", 1)),
            type=raw.get("type", "application"),
            launch_type=raw.get("launch_type"),
            health_check_path=raw.get("health_check_path"),
            public=bool(raw.get("public", False)),
            port=raw.get("port"),
            ephemeral_storage=raw.get("ephemeral_storage"),
            default_target=bool(raw.get("default_target", False)),
            volumes=list(raw.get("volumes", [])),
            lifecycle=_parse_lifecycle(name, raw["lifecycle"])
                       if raw.get("lifecycle") else {},
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

    secrets_raw = raw.get("secrets", []) or []
    secrets = [_parse_secret(s) for s in secrets_raw]

    backup = None
    if raw.get("backup"):
        backup = BackupConfig(
            bucket=raw["backup"].get("bucket"),
            service=raw["backup"].get("service"),
        )

    tls = None
    if raw.get("tls"):
        tls = TlsConfig(
            mode=raw["tls"].get("mode", "acm"),
            certificate_arn=raw["tls"].get("certificate_arn"),
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
    )
    cfg.validate()
    return cfg


def load(path: str | Path) -> RcConfigV2:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return parse(raw)
