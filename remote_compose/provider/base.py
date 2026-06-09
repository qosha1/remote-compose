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
    # Container-level ECS healthCheck (readiness), as a dict:
    # {command: list[str], interval, timeout, retries, start_period}. Drives
    # zero-downtime worker rolls — ECS keeps old tasks until new ones pass
    # this check. Distinct from health_check_path (ALB target-group check).
    health_check: Optional[dict[str, Any]] = None
    # rc-05q: ECS service.health_check_grace_period_seconds. Only emitted
    # for services with public=true (load_balancer block). When None the
    # provider computes a default (60s base, 180s when any auto_on_deploy
    # lifecycle hook is declared, since those run during rollout and
    # extend the boot window).
    health_check_grace_period: Optional[int] = None
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
    # ALB listener default action (catch-all) selection. When a public+port
    # service sets this True it becomes the target for traffic that matches no
    # host-header rule (unmatched hosts + service aliases). Without it the
    # provider falls back to the first public+port service in alphabetical
    # order, which silently routes unmatched hosts to whatever sorts first
    # (e.g. celery-flower before nginx). Only one service should set it.
    default_target: bool = False
    # Extra hostnames the SAME service should answer for. Each adds a cert
    # SAN + R53 record but no ALB listener rule — the default action
    # catches them. Used when a fronting service (nginx) handles internal
    # routing for multiple hostnames.
    aliases: list[str] = field(default_factory=list)
    # Hot-reload source mounts (rc-e5u.45.7+). Each entry has name/source/
    # mount. Provider only materializes these when ctx.dev_mode is True
    # (see rc-e5u.45.8) — production deploys ignore the field entirely.
    dev_volumes: list[dict[str, Any]] = field(default_factory=list)
    # rc-12d: auto-discovered SM secret names sourced from this service's
    # compose env_file directives. ECSProvider.emit_terraform filters
    # task-def secrets[] per service against this list so a service that
    # only references env_file X doesn't inherit keys from env_file Y.
    # Empty list = service has no compose env_file directives. Names
    # match entries in DeployContext.secrets (file-sourced).
    env_file_secret_names: list[str] = field(default_factory=list)


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
    # When True, the provider materializes services[*].dev_volumes as
    # EFS-backed bind mounts so `rc dev push` can stream local source
    # into the live task at the declared mount path. Set by `rc up --dev`
    # / `rc deploy --dev`. Production deploys leave this False — see
    # rc-e5u.45.8 for the full gating semantics.
    dev_mode: bool = False
    # rc-5h8.11: when True, ECSProvider.deploy bypasses terraform entirely
    # (no emit_terraform / init / apply / outputs) and goes straight to
    # image build + push + force-roll. Used for hybrid stacks that have
    # v2-shaped task defs but where the underlying VPC/ALB/EFS/SM are NOT
    # under terraform management (e.g. v1-imperative-deployed legacy stacks).
    # ECR repo URLs come from boto3 describe-repositories instead of tf
    # outputs. Set by `rc deploy --no-state`.
    skip_terraform: bool = False

    # rc-1bk: when True, deploy() builds + pushes images but does NOT call
    # _force_new_deployments. Used by `rc up` so the rollout happens AFTER
    # `rc secrets push` populates SM, avoiding the cold-start failure where
    # rolled tasks can't pull placeholder secrets and the wait/exec hooks
    # then hang for 30+ minutes.
    skip_force_roll: bool = False

    # rc-44z: when True, deploy() runs terraform apply + force-roll but
    # SKIPS _build_and_push_images. Used by `rc deploy --no-build` for
    # terraform-only changes (e.g. bumping a task-def field) so users
    # don't pay the multi-minute image rebuild cost on a no-op image
    # change. Force-roll still rolls the existing :latest image.
    skip_build: bool = False


@dataclass
class ServiceStatus:
    name: str
    desired: int
    running: int
    health: str
    last_event: Optional[str] = None
    # Currently-deployed task definition revision (e.g., 3 for "...:3").
    # When None, provider doesn't track per-revision state. When the
    # running revision != latest, the service is "stale" — a previous
    # deploy stuck on it. See rc-e5u.44.24.
    running_revision: Optional[int] = None
    latest_revision: Optional[int] = None

    @property
    def is_stale(self) -> bool:
        """True when the service is running an OLDER task def revision than
        the family's latest. Indicates a partial-deploy failure or a manual
        ECS rollback. ``rc deploy --reconcile`` brings stale services
        forward.
        """
        if self.running_revision is None or self.latest_revision is None:
            return False
        return self.running_revision < self.latest_revision


@dataclass
class StatusReport:
    """Result of :meth:`Provider.status`.

    services: one ServiceStatus per service in ctx (in deterministic order).
    cluster_health: provider-defined coarse string ('healthy' / 'inactive' /
        'degraded'). Read by `rc status` for the summary line.
    ingress_url: public-facing URL when one exists (ECS: ALB DNS name).
    """

    services: list[ServiceStatus]
    cluster_health: str
    ingress_url: Optional[str] = None


@dataclass
class DeployResult:
    """Result of :meth:`Provider.deploy` / redeploy / rollback.

    revision_id: provider-specific stable id for this revision. Caller
        treats it as opaque. Identical (config_hash, ctx) → identical id.
    services: service names actually rolled. Subset of ctx.services when
        services_filter was set.
    duration_s: wall-clock seconds.
    warnings: non-fatal observations (empty secrets, dev_volumes without
        --dev mode, image-build skipped due to no compose build:, etc).
        Rendered to stderr by `rc deploy`.
    terraform_outputs: provider-specific blob exposed by `terraform output
        -json`. ECS surfaces alb_dns_name + ecr_repositories + efs_*.
    """

    revision_id: str
    services: list[str]
    duration_s: float
    warnings: list[str] = field(default_factory=list)
    terraform_outputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResult:
    """Result of :meth:`Provider.plan`.

    Counts come from terraform's "Plan: N to add, N to change, N to
    destroy" line. raw_plan carries full stdout. warnings accumulates
    compose-level lint findings (rc.yml schema warnings, unsupported
    compose features) — see compose_warnings.collect_compose_warnings.
    """

    create: int
    update: int
    destroy: int
    raw_plan: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    """Result of :meth:`Provider.exec` (non-interactive only).

    Interactive exec replaces the current process and never returns a
    value. For non-interactive: exit_code is the inner command's real
    exit code (parsed from a sentinel for ECS — SSM exec channel doesn't
    surface it natively). stdout/stderr are captured strings; trailing
    newline preserved as-is.
    """

    exit_code: int
    stdout: str
    stderr: str


class Provider(ABC):
    """Abstract base class every provider implementation extends.

    Contract semantics are enforced by tests/contract/test_provider_contract.py:
    every concrete provider runs against the same suite, and any new provider
    that passes the suite is interchangeable with ECSProvider / FakeProvider /
    KubernetesProvider from the rest of the system's POV.

    All methods take a :class:`DeployContext` describing the target stack
    (project name, compose, rc.yml v2, services, secrets). Methods may read
    from ctx but should not mutate it — the only ctx mutations rc itself
    performs are setting ``ctx.dev_mode`` (rc up --dev) and ``ctx.expires_at``
    (rc deploy --ttl) BEFORE calling the provider.

    Error contract: provider methods raise :class:`ProviderError` (or a
    subclass) for provider-attributable failures. They MAY also let
    :class:`subprocess.SubprocessError` / boto3 ``ClientError`` propagate
    when the failure is upstream and providing a wrapped message would only
    obscure the cause. The CLI catches ProviderError and renders cleanly;
    bare exceptions render with a stack trace (intentional — they're bugs).

    Idempotency: deploy / destroy / plan / redeploy / emit_terraform must
    all be safe to call repeatedly with the same context. status / logs /
    exec are read-only or per-call and naturally idempotent.

    Subclass-only attribute: ``name`` is the lowercase identifier used by
    ``rc.yml`` ``provider:`` and the registry. Must match the key passed
    to :func:`provider.register`.
    """

    name: str

    @abstractmethod
    def emit_terraform(self, ctx: DeployContext, out_dir: Path) -> Path:
        """Write a self-contained terraform module to ``out_dir``.

        Args:
            ctx: Source context. The provider reads project, services,
                secrets, provider_config, tf_backend_config, dev_mode,
                expires_at. Compose-derived fields (build_context, image)
                are honored when present but optional.
            out_dir: Destination directory for the .tf files. Created if
                missing. Existing files in this directory may be
                overwritten — the caller owns lifecycle.

        Returns:
            The same ``out_dir`` (resolved absolute), as a convenience for
            chaining into ``TerraformRunner(out_dir)``.

        Raises:
            ProviderConfigError: ctx is missing a required field
                (e.g. provider_config.ecs.region for ECSProvider) or
                contains an invalid value (cpu not in Fargate's allowed
                ladder, EFS volume without uid/gid, etc.).

        Side effects: filesystem writes only — never runs terraform,
        boto3, or docker. Pure emission.
        """

    @abstractmethod
    def plan(self, ctx: DeployContext) -> PlanResult:
        """Emit terraform and run ``terraform plan``.

        Args:
            ctx: Same shape as emit_terraform.

        Returns:
            PlanResult with create/update/destroy resource counts parsed
            from the plan output. ``raw_plan`` carries the full stdout
            so callers can render or persist it. ``warnings`` collects
            compose-level lint findings (rc.yml schema warnings,
            unsupported compose features, etc.).

        Raises:
            ProviderConfigError: ctx invalid (see emit_terraform).
            ProviderError: terraform binary missing or plan exited
                non-zero. The wrapped TerraformError stderr is preserved
                in the message.

        Idempotent. Runs `terraform init` (no -upgrade) before plan.
        """

    @abstractmethod
    def deploy(
        self,
        ctx: DeployContext,
        services_filter: Optional[list[str]] = None,
        tag: Optional[str] = None,
    ) -> DeployResult:
        """Idempotent apply: emit tf, terraform apply, build/push images, roll services.

        Args:
            ctx: Source context.
            services_filter: When set, only these services have images
                rebuilt + pushed + force-rolled. Other services keep
                their existing task-def revision. Terraform apply still
                runs (idempotent, may be a no-op) so rc.yml-driven infra
                changes still take effect. Used by ``rc deploy --services
                <name>`` for fast single-service iteration.
            tag: When set (and not 'latest'), provider-specific shortcut:
                check the registry for ``<repo>:<tag>``; if present, skip
                docker build and re-tag the existing image as ``:latest``
                so the task def picks it up. Used by ``rc deploy --tag
                <known-good>`` for rollback or pinned-image deploys.
                ~2-5s per service. When None, build :latest as usual.

        Returns:
            DeployResult with the new revision_id (provider-specific,
            stable per (config_hash, ctx)), the services that were
            actually rolled, total duration, and any warnings (e.g.
            empty-secrets detection, dev_volumes without dev_mode).
            ``terraform_outputs`` carries provider-specific data (for
            ECS: ALB DNS name, ECR repo URLs, EFS file system IDs).

        Raises:
            ProviderConfigError: ctx invalid.
            ProviderError: terraform apply failed, image build failed,
                ECS service update failed.

        Idempotent: re-running with the same ctx is a no-op apply +
        forced rollout.
        """

    @abstractmethod
    def redeploy(
        self, ctx: DeployContext, services: Optional[list[str]] = None
    ) -> DeployResult:
        """Force a new task revision without changing config or rebuilding images.

        Args:
            ctx: Source context.
            services: When set, only these services are rolled. When
                None, every service in ctx is rolled.

        Returns:
            DeployResult shaped like deploy(). Skips image build, skips
            terraform apply.

        Raises:
            ProviderError: ECS service update failed.

        Use case: env vars rotated out-of-band (e.g. SM secret value
        changed via `rc secrets push --no-rollout`), and the user wants
        running tasks to pick them up without a code change.
        """

    @abstractmethod
    def status(self, ctx: DeployContext) -> StatusReport:
        """Inspect live infrastructure and return per-service state.

        Args:
            ctx: Source context.

        Returns:
            StatusReport with one ServiceStatus per service in ctx. Each
            entry's running_revision / latest_revision drives the
            ``is_stale`` property used by ``rc deploy --reconcile``.
            ``ingress_url`` is the public-facing URL when one exists
            (for ECS: ALB DNS name).

        Raises:
            ProviderError: AWS API failure during describe-services /
                describe-task-definition.

        Read-only. Does not mutate AWS state.
        """

    @abstractmethod
    def logs(
        self,
        ctx: DeployContext,
        service: str,
        follow: bool = False,
        tail: int = 100,
    ) -> Iterator[str]:
        """Stream or tail container logs for one service.

        Args:
            ctx: Source context.
            service: Service name (must be in ctx.services).
            follow: When True, yield indefinitely as new lines arrive.
                When False, yield ``tail`` historical lines and stop.
            tail: When ``follow`` is False, max number of lines to return.

        Yields:
            Log lines as strings, in chronological order, no trailing
            newline. Each line is one container stdout/stderr write.

        Raises:
            ProviderNotFoundError: ``service`` not in ctx, or has no
                task definition deployed yet.
            ProviderError: log group / log stream lookup failed.

        Read-only.
        """

    @abstractmethod
    def exec(
        self,
        ctx: DeployContext,
        service: str,
        command: list[str],
        interactive: bool = False,
    ) -> ExecResult:
        """Run a command in a live container of the named service.

        Args:
            ctx: Source context.
            service: Service name (must be in ctx.services).
            command: Argv to run inside the container. NOT a shell
                string — the provider may wrap with ``sh -c`` for
                multi-arg commands but quoting is the caller's job.
            interactive: When True, the provider exec replaces the
                current process with an interactive session (stdin/stdout
                wired to the user's terminal). The return is unreachable
                — caller never observes the result. When False, captures
                stdout/stderr/exit code and returns them.

        Returns:
            ExecResult with the command's exit_code, stdout, stderr.
            Only meaningful when interactive=False.

        Raises:
            ProviderNotFoundError: ``service`` not in ctx, or no running
                tasks for the service.
            ProviderError: SSM / exec channel setup failed (missing
                session-manager-plugin, IAM denial, agent not ready).

        For ECS, exit_code is parsed from a sentinel injected by the
        wrapper: SSM doesn't surface the inner command's exit code
        directly. See ECSProvider._SENTINEL_BEGIN/END/EXIT.
        """

    def run_one_off(
        self,
        ctx: DeployContext,
        service: str,
        command: list[str],
        *,
        wait: bool = True,
        timeout: int = 900,
        container: Optional[str] = None,
    ) -> ExecResult:
        """Run a command as a fresh one-off task on the service's task def.

        Unlike :meth:`exec` (into a running task), this launches a NEW task
        from the service's task definition, so the command gets the task role
        AND any secrets the platform injects at task start (Secrets Manager
        for ECS). Use for secret-dependent management commands that an exec
        session can't run. Optional capability — not every provider supports
        it; the default raises ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support run_one_off "
            f"(one-off task execution)"
        )

    @abstractmethod
    def rollback(
        self,
        ctx: DeployContext,
        to_revision: Optional[str] = None,
    ) -> DeployResult:
        """Revert to the previous (or specified) deployed state.

        Args:
            ctx: Source context.
            to_revision: Revision id to roll back to. When None, picks
                the most recent revision other than the active one.

        Returns:
            DeployResult for the rolled-back revision. revision_id is
            the new clone (rolling back is itself a new revision —
            history is append-only).

        Raises:
            ProviderError: nothing to roll back to (less than 2
                revisions exist), or terraform/cloud failure during
                the revert.
            ProviderNotFoundError: ``to_revision`` is set but no such
                revision exists.

        Local-backend providers may refuse: terraform rollback against
        a local state file isn't safe (state could have drifted between
        the original apply and now). ECS today rejects rollback when
        tf_backend_config.type == "local"; users with remote backends
        (s3) can use it.
        """

    @abstractmethod
    def destroy(self, ctx: DeployContext) -> None:
        """Fully remove everything this provider created for ctx.

        Args:
            ctx: Source context.

        Returns:
            None.

        Raises:
            ProviderError: terraform destroy failed.

        Idempotent: re-running on already-destroyed state is a no-op
        (terraform destroy is itself idempotent). Resources NOT under
        this provider's terraform module (out-of-band created secrets,
        manually-imported buckets) are left alone — see ``rc audit``
        for post-destroy leftover detection.
        """
