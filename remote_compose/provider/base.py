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
