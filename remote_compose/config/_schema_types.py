"""rc.yml v2 — dataclass types + per-instance validate() methods.

The dataclasses live here (not in v2_schema.py) so the cross-instance
parsing logic in _schema_parser.py can import them without pulling in
yaml/load. Keeps the type surface dependency-light.

Per-instance ``validate()`` lives on the dataclasses themselves; cross-
service uniqueness checks (e.g. duplicate hostname detection) live in
_schema_parser.parse since they need the full config in hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class ConfigError(ValueError):
    """Raised when an rc.yml v2 document fails validation."""


VALID_SERVICE_TYPES = {"application", "worker", "infrastructure", "proxy"}
VALID_LAUNCH_TYPES = {"FARGATE", "EC2"}
VALID_CAPACITY_TYPES = {"ON_DEMAND", "SPOT", "MIXED"}
VALID_SECRET_SOURCES = {"file", "env_file_auto", "aws_sm", "k8s_secret", "gcp_sm"}
VALID_TLS_MODES = {"acm", "cert-manager", "manual"}


_FQDN_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def _looks_like_fqdn(name: str) -> bool:
    """Crude FQDN check: at least one dot, RFC1123-ish labels, no
    trailing dot, no leading/trailing hyphen on any label.

    Accepts a leading ``*.`` wildcard — ACM, Route 53, and most ALB
    listener rule conditions support wildcard hostnames at the leftmost
    label only.
    """
    if not name or "." not in name or name.endswith("."):
        return False
    if " " in name:
        return False
    labels = name.split(".")
    if labels[0] == "*":
        labels = labels[1:]
        if not labels:
            return False
    return all(_FQDN_LABEL_RE.match(label) for label in labels)


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
    # rc-05q: ECS service.health_check_grace_period_seconds (Fargate-only,
    # only meaningful for ALB-fronted services). When None, provider
    # computes 60s default (or 180s with an auto_on_deploy lifecycle
    # hook). Set explicitly to bypass the auto-default.
    health_check_grace_period: Optional[int] = None
    public: bool = False
    port: Optional[int] = None
    ephemeral_storage: Optional[int] = None
    default_target: bool = False
    volumes: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: dict[str, LifecycleHookV2] = field(default_factory=dict)
    # ALB host-based routing. When set + public=true, the provider creates
    # a per-service target group, ALB listener rule keyed on Host header,
    # R53 alias record, and adds the name to the ACM cert SANs.
    domain: Optional[str] = None
    # Extra hostnames the SAME service should answer for. Used when a
    # single fronting service (nginx, traefik) handles internal routing
    # for multiple hostnames. Each alias adds a cert SAN + R53 record but
    # NOT a listener rule — the ALB default action catches them.
    aliases: list[str] = field(default_factory=list)
    # Override the Dockerfile path used during 'rc deploy' for this service.
    # Path is interpreted RELATIVE TO THE BUILD CONTEXT (matching compose's
    # ``build.dockerfile`` semantics — ImageBuilder joins this to
    # ServiceSpec.build_context before invoking docker buildx). When set,
    # rc uses this instead of compose's ``services.<svc>.build.dockerfile``,
    # letting users keep their docker-compose.yml untouched while pointing
    # rc at an ECS-aware variant (e.g., the one emitted by `rc fix
    # nginx-conf`). See .46.1.
    dockerfile: Optional[str] = None
    # Hot-reload mounts for `rc up --dev` (rc-e5u.45.7+). Each entry maps a
    # local source dir into a mount path inside the container; the provider
    # backs them with EFS in dev mode and mounts the EFS access point at the
    # declared path. PRODUCTION DEPLOYS IGNORE THIS FIELD — see .45.8 for
    # the dev-mode gating. Distinct from `volumes` (which is for persistent
    # state like postgres data, EFS-backed in any mode).
    dev_volumes: list[dict[str, Any]] = field(default_factory=list)
    # rc-e5u.46.4: extra plaintext env vars merged ON TOP of compose's
    # ``environment:`` / ``env_file:`` at deploy time. rc.yml wins on key
    # collision. Values flow into the task-def containerDefinitions
    # environment[] verbatim — these are NOT secrets (use the top-level
    # ``secrets:`` block for those). Primary use: scaffolder-injected
    # DJANGO_ALLOWED_HOSTS=* / CSRF_TRUSTED_ORIGINS=* for ephemeral
    # ``rc-test-*`` projects so plain ``curl http://<ALB>/`` works without
    # nginx Host: rewrites. Hand-editable for any non-secret toggle a user
    # wants applied at the rc.yml layer rather than touching compose.
    env: dict[str, str] = field(default_factory=dict)
    # rc-e5u.35.7: explicit framework hint. When set, cli_v2 merges the
    # named preset's lifecycle_hooks into this service's lifecycle dict
    # for hooks the user hasn't declared. ``django`` / ``rails`` /
    # ``phoenix`` are built-in (see remote_compose.frameworks).
    # Auto-detection from the Dockerfile still runs when this is None.
    framework: Optional[str] = None

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
        if self.domain is not None:
            if not self.public:
                raise ConfigError(
                    f"service {self.name!r}: domain={self.domain!r} requires "
                    f"public=true (private services have no ALB target)"
                )
            if not _looks_like_fqdn(self.domain):
                raise ConfigError(
                    f"service {self.name!r}: domain {self.domain!r} is not a "
                    f"valid FQDN"
                )
        if self.aliases:
            if not isinstance(self.aliases, list):
                raise ConfigError(
                    f"service {self.name!r}: aliases must be a list of FQDNs, "
                    f"got {type(self.aliases).__name__}"
                )
            if not self.public:
                raise ConfigError(
                    f"service {self.name!r}: aliases requires public=true "
                    f"(private services have no ALB target)"
                )
            for alias in self.aliases:
                if not isinstance(alias, str) or not _looks_like_fqdn(alias):
                    raise ConfigError(
                        f"service {self.name!r}: alias {alias!r} is not a "
                        f"valid FQDN"
                    )
                if alias == self.domain:
                    raise ConfigError(
                        f"service {self.name!r}: alias {alias!r} duplicates "
                        f"the service's own domain"
                    )
        if self.env:
            if not isinstance(self.env, dict):
                raise ConfigError(
                    f"service {self.name!r}: env must be a mapping of "
                    f"VAR: value, got {type(self.env).__name__}"
                )
            for k, v in self.env.items():
                if not isinstance(k, str) or not k:
                    raise ConfigError(
                        f"service {self.name!r}: env keys must be non-empty "
                        f"strings, got {k!r}"
                    )
                if isinstance(v, (dict, list)):
                    raise ConfigError(
                        f"service {self.name!r}: env[{k!r}] must be scalar "
                        f"(str/int/bool), got {type(v).__name__}"
                    )
        if self.dev_volumes:
            if not isinstance(self.dev_volumes, list):
                raise ConfigError(
                    f"service {self.name!r}: dev_volumes must be a list, got "
                    f"{type(self.dev_volumes).__name__}"
                )
            seen_names: set[str] = set()
            seen_mounts: set[str] = set()
            for i, entry in enumerate(self.dev_volumes):
                if not isinstance(entry, dict):
                    raise ConfigError(
                        f"service {self.name!r}: dev_volumes[{i}] must be a "
                        f"mapping with name/source/mount, got "
                        f"{type(entry).__name__}"
                    )
                for key in ("name", "source", "mount"):
                    if not entry.get(key):
                        raise ConfigError(
                            f"service {self.name!r}: dev_volumes[{i}] missing "
                            f"required field {key!r} (need name + source + mount)"
                        )
                name = str(entry["name"])
                source = str(entry["source"])
                mount = str(entry["mount"])
                if Path(source).is_absolute():
                    raise ConfigError(
                        f"service {self.name!r}: dev_volumes[{i}].source "
                        f"({source!r}) must be a relative path "
                        f"(resolved against the compose file's directory). "
                        f"Absolute paths defeat portability across machines."
                    )
                if not mount.startswith("/"):
                    raise ConfigError(
                        f"service {self.name!r}: dev_volumes[{i}].mount "
                        f"({mount!r}) must be an absolute path inside the "
                        f"container (e.g. '/app')."
                    )
                if name in seen_names:
                    raise ConfigError(
                        f"service {self.name!r}: dev_volumes name {name!r} "
                        f"declared twice"
                    )
                if mount in seen_mounts:
                    raise ConfigError(
                        f"service {self.name!r}: dev_volumes mount {mount!r} "
                        f"declared on two entries — each container path can "
                        f"only mount one source"
                    )
                seen_names.add(name)
                seen_mounts.add(mount)


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

    def validate(self) -> None:
        """Strict schema validation.

        ``type=s3`` requires both ``bucket`` and ``key``. ``dynamodb_table``
        is optional but recommended — without it, terraform's s3 backend
        runs without state-locking and concurrent ``rc deploy`` calls can
        corrupt state. We emit a stderr warning in that case rather than
        raise so back-compat with existing single-developer setups stays
        intact.
        """
        if self.type == "s3":
            if not self.bucket:
                raise ConfigError(
                    "terraform.backend type=s3 requires bucket"
                )
            if not self.key:
                raise ConfigError(
                    "terraform.backend type=s3 requires key"
                )
            if not self.dynamodb_table:
                import sys
                sys.stderr.write(
                    "warning: terraform.backend type=s3 without "
                    "dynamodb_table — concurrent rc deploys can corrupt "
                    "state. Add a lock table to guard against this.\n"
                )


@dataclass
class TerraformConfig:
    output_dir: str = "./terraform/${provider}"
    backend: TerraformBackend = field(default_factory=TerraformBackend)


@dataclass
class ComposeConfig:
    """How rc.yml v2 relates to the docker-compose file.

    By default the deploy set is the union of compose services + any
    rc.yml services. include narrows to a whitelist; exclude removes a
    blacklist. Mutually exclusive.
    """
    include: Optional[list[str]] = None
    exclude: Optional[list[str]] = None

    def validate(self) -> None:
        if self.include is not None and self.exclude is not None:
            raise ConfigError(
                "compose.include and compose.exclude are mutually exclusive"
            )
        if self.include is not None and not isinstance(self.include, list):
            raise ConfigError(
                f"compose.include must be a list of service names, got "
                f"{type(self.include).__name__}"
            )
        if self.exclude is not None and not isinstance(self.exclude, list):
            raise ConfigError(
                f"compose.exclude must be a list of service names, got "
                f"{type(self.exclude).__name__}"
            )


@dataclass
class BackupConfig:
    bucket: Optional[str] = None
    service: Optional[str] = None
    # When True (default), the provider creates the S3 bucket via terraform
    # alongside the rest of the stack. Set False if the bucket already
    # exists or is owned by another team / pipeline you don't want
    # remote-compose to touch.
    bucket_managed: bool = True
    # Days to keep dump objects before lifecycle expiration. None = never expire.
    retention_days: Optional[int] = 14


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
    compose: Optional[ComposeConfig] = None

    def validate(self) -> None:
        if self.version != 2:
            raise ConfigError(f"version must be 2, got {self.version!r}")
        if not self.project:
            raise ConfigError("project is required")
        if not self.compose_file:
            raise ConfigError("compose_file is required")
        if not self.provider:
            raise ConfigError("provider is required")
        if self.compose is not None:
            self.compose.validate()
        for svc in self.services.values():
            svc.validate()
        for sec in self.secrets:
            sec.validate()
        if self.tls is not None:
            self.tls.validate()
