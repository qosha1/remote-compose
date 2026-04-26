"""Provider interface contract for portable multi-cloud compose deployment.

All cloud-specific deployers implement :class:`Provider`. Core code and
FakeProvider depend on nothing in this module beyond the standard library;
cloud SDKs live in per-provider subpackages as optional extras.

Contract semantics are enforced by tests/contract/test_provider_contract.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


class ProviderError(Exception):
    """Base class for provider-raised errors."""


class ProviderTimeoutError(ProviderError):
    """A provider operation exceeded its time budget."""


class ProviderNotFoundError(ProviderError):
    """A referenced resource does not exist."""


class ProviderConfigError(ProviderError):
    """The supplied DeployContext is invalid for this provider."""


@dataclass
class SecretRef:
    name: str
    source: str
    path: Optional[str] = None
    arn: Optional[str] = None
    ref: Optional[str] = None


@dataclass
class ServiceSpec:
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
    volumes: list[dict[str, Any]] = field(default_factory=list)
    # Populated from docker-compose.yml when the service has a `build:` stanza.
    # build_context is an absolute path. When None, the service uses a pre-built
    # image (skipped by the ImageBuilder/ImagePusher path in Provider.deploy).
    build_context: Optional[Any] = None
    build_args: dict[str, str] = field(default_factory=dict)
    dockerfile: Optional[str] = None
    image: Optional[str] = None
    # Compose 'build: { target: <stage> }' for multi-stage builds. Passed
    # through to docker build --target.
    target: Optional[str] = None
    # Additional containerPorts beyond the primary `port`. Sourced from
    # compose ports[]. Intra-VPC reachable via the tasks SG without per-
    # port ALB wiring (use this for VNC, devtools, internal-only ports).
    extra_ports: list[int] = field(default_factory=list)
    # Plain environment variables (docker-compose environment:). Flows into
    # the ECS task definition containerDefinitions.environment[].
    env: dict[str, str] = field(default_factory=dict)
    # Command override — when set, renders as containerDefinitions.command[].
    command: list[str] = field(default_factory=list)
    # Named lifecycle hooks (rc lifecycle migrate, rc lifecycle createsuperuser).
    # Keyed by hook name; values are dicts with: command (list[str]), and
    # optional flags auto_on_deploy / run_once / interactive / probe.
    lifecycle: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ALB host-based routing. When set + public=true, the provider creates a
    # dedicated target group + ALB listener rule (host_header) + R53 record
    # for this service, and adds the name to the ACM cert SANs.
    domain: Optional[str] = None
    # Extra hostnames the SAME service should answer for. Each adds a cert
    # SAN + R53 record but no ALB listener rule — the default action
    # catches them. Used when a fronting service (nginx) handles internal
    # routing for multiple hostnames.
    aliases: list[str] = field(default_factory=list)


@dataclass
class DeployContext:
    project: str
    compose_path: Path
    rc_yml_v2: dict[str, Any]
    provider_config: dict[str, Any]
    tf_backend_config: dict[str, Any]
    working_dir: Path
    services: dict[str, ServiceSpec] = field(default_factory=dict)
    secrets: list[SecretRef] = field(default_factory=list)
    # When set, this stack was deployed with a TTL. The provider should
    # add Ephemeral=true + ExpiresAt=<this> to its default tags so that
    # ``rc reap`` can locate past-due stacks (and so any out-of-band tag
    # scan can identify ephemeral resources). ISO 8601 UTC timestamp,
    # e.g. "2026-04-25T18:30:00Z".
    expires_at: Optional[str] = None


@dataclass
class ServiceStatus:
    name: str
    desired: int
    running: int
    health: str
    last_event: Optional[str] = None


@dataclass
class StatusReport:
    services: list[ServiceStatus]
    cluster_health: str
    ingress_url: Optional[str] = None


@dataclass
class DeployResult:
    revision_id: str
    services: list[str]
    duration_s: float
    warnings: list[str] = field(default_factory=list)
    terraform_outputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResult:
    create: int
    update: int
    destroy: int
    raw_plan: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class Provider(ABC):
    """Abstract base class every provider implementation extends."""

    name: str

    @abstractmethod
    def emit_terraform(self, ctx: DeployContext, out_dir: Path) -> Path:
        """Write a self-contained terraform module. Does not run terraform."""

    @abstractmethod
    def plan(self, ctx: DeployContext) -> PlanResult:
        """Emit terraform and run ``terraform plan``."""

    @abstractmethod
    def deploy(self, ctx: DeployContext) -> DeployResult:
        """Idempotent apply: emit tf, terraform apply, build/push images, update services."""

    @abstractmethod
    def redeploy(self, ctx: DeployContext, services: Optional[list[str]] = None) -> DeployResult:
        """Force a new revision without a config change."""

    @abstractmethod
    def status(self, ctx: DeployContext) -> StatusReport:
        """Inspect live infrastructure and return per-service state."""

    @abstractmethod
    def logs(
        self,
        ctx: DeployContext,
        service: str,
        follow: bool = False,
        tail: int = 100,
    ) -> Iterator[str]:
        """Stream or tail container logs for one service."""

    @abstractmethod
    def exec(
        self,
        ctx: DeployContext,
        service: str,
        command: list[str],
        interactive: bool = False,
    ) -> ExecResult:
        """Run a command in a live container of the named service."""

    @abstractmethod
    def rollback(
        self,
        ctx: DeployContext,
        to_revision: Optional[str] = None,
    ) -> DeployResult:
        """Revert to the previous (or specified) deployed state."""

    @abstractmethod
    def destroy(self, ctx: DeployContext) -> None:
        """Fully remove everything this provider created for ctx."""
