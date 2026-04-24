"""ECS provider implementation.

Phase 6b: ``emit_terraform`` only.
Phase 6b.1 (this module): ``plan``, ``deploy``, ``destroy``, ``redeploy``,
``status``; ``logs`` and ``exec`` are stubbed and ``rollback`` is supported
only for remote (non-local) terraform backends.

Feature work deferred to dedicated follow-ups:
  - rc-e5u.13  EC2 launch type + ASG capacity provider
  - rc-e5u.14  EFS persistent volumes
  - rc-e5u.15  Secrets integration (file / aws_sm)
  - rc-e5u.16  Custom domain + ACM + Route 53
  - rc-e5u.17  Register 'ecs' in registry + enroll in contract suite
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ...envfile import EnvFileError, keys as env_file_keys
from ...terraform.backend import render_backend_block
from ...terraform.emitter import TerraformEmitter
from ...terraform.runner import TerraformRunner
from ..base import (
    DeployContext,
    DeployResult,
    ExecResult,
    PlanResult,
    Provider,
    ProviderConfigError,
    ProviderError,
    ServiceStatus,
    StatusReport,
)
from .autosize import EC2TaskDemand, auto_size


_TEMPLATES_DIR = Path(__file__).parent / "templates"


RunnerFactory = Callable[[Path], TerraformRunner]
SessionFactory = Callable[[DeployContext], Any]


def _tf_name(svc_name: str) -> str:
    """Sanitize a compose service name into a terraform identifier."""
    tf = re.sub(r"[^a-zA-Z0-9_]", "_", svc_name)
    if tf and tf[0].isdigit():
        tf = f"_{tf}"
    return tf or "svc"


def _env_name_for_secret(secret_name: str) -> str:
    """Derive an uppercase env var name from a secret's rc.yml name."""
    env = re.sub(r"[^A-Za-z0-9_]", "_", secret_name).upper()
    if env and env[0].isdigit():
        env = f"_{env}"
    return env or "SECRET"


def _zone_from_domain(domain: str) -> str:
    """Extract the hosted-zone name from an FQDN.

    api.example.com → example.com
    example.com     → example.com
    """
    parts = domain.strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def _default_runner_factory(out_dir: Path) -> TerraformRunner:
    return TerraformRunner(out_dir)


def _default_session_factory(ctx: DeployContext) -> Any:
    """Return a boto3 Session configured from ctx.provider_config.ecs.

    Imported lazily so core + FakeProvider never drag boto3 in.
    """
    import boto3  # noqa: WPS433 (intentional local import)
    ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
    return boto3.Session(
        region_name=ecs_cfg.get("region"),
        profile_name=ecs_cfg.get("aws_profile"),
    )


class ECSProvider(Provider):
    name = "ecs"

    def __init__(
        self,
        emitter: Optional[TerraformEmitter] = None,
        runner_factory: Optional[RunnerFactory] = None,
        session_factory: Optional[SessionFactory] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.emitter = emitter or TerraformEmitter(_TEMPLATES_DIR)
        self.runner_factory = runner_factory or _default_runner_factory
        self.session_factory = session_factory or _default_session_factory
        self.progress = progress

    # -----------------------------------------------------------------
    # emit_terraform
    # -----------------------------------------------------------------

    def emit_terraform(self, ctx: DeployContext, out_dir: Path) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        region = ecs_cfg.get("region")
        if not region:
            raise ProviderConfigError(
                "ECS provider requires provider_config.ecs.region"
            )
        cluster_name = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        vpc_cidr = ecs_cfg.get("vpc_cidr", "10.0.0.0/16")
        aws_profile = ecs_cfg.get("aws_profile")

        default_launch_type = ecs_cfg.get("default_launch_type", "FARGATE")
        if default_launch_type not in {"FARGATE", "EC2"}:
            raise ProviderConfigError(
                f"provider_config.ecs.default_launch_type must be FARGATE or EC2, "
                f"got {default_launch_type!r}"
            )

        services_view = []
        default_public = None
        ec2_demands: list[EC2TaskDemand] = []
        efs_volumes: dict[str, dict[str, Any]] = {}
        service_volume_mounts: list[dict[str, Any]] = []

        for name, spec in sorted(ctx.services.items()):
            launch_type = spec.launch_type or default_launch_type
            if launch_type not in {"FARGATE", "EC2"}:
                raise ProviderConfigError(
                    f"service {name!r}: launch_type must be FARGATE or EC2, "
                    f"got {launch_type!r}"
                )

            if spec.ephemeral_storage is not None:
                if launch_type != "FARGATE":
                    raise ProviderConfigError(
                        f"service {name!r}: ephemeral_storage is only supported "
                        f"on FARGATE launch_type, got {launch_type!r}"
                    )
                if not (21 <= spec.ephemeral_storage <= 200):
                    raise ProviderConfigError(
                        f"service {name!r}: ephemeral_storage must be between "
                        f"21 and 200 GiB (AWS Fargate limits), got "
                        f"{spec.ephemeral_storage}"
                    )

            svc_mounts = []
            for vol_entry in spec.volumes or []:
                vol_name = vol_entry.get("name")
                mount_path = vol_entry.get("mount")
                if not vol_name or not mount_path:
                    raise ProviderConfigError(
                        f"service {name!r}: each volume entry requires "
                        f"'name' and 'mount', got {vol_entry!r}"
                    )
                vol_tf = _tf_name(vol_name)
                efs_volumes.setdefault(vol_name, {
                    "name": vol_name,
                    "tf_name": vol_tf,
                })
                ap_tf = f"{_tf_name(name)}__{vol_tf}"
                mount_view = {
                    "volume": vol_name,
                    "volume_tf_name": vol_tf,
                    "mount_path": mount_path,
                    "service": name,
                    "access_point_tf_name": ap_tf,
                }
                svc_mounts.append(mount_view)
                service_volume_mounts.append(mount_view)

            svc_view = {
                "name": name,
                "tf_name": _tf_name(name),
                "cpu": spec.cpu,
                "memory": spec.memory,
                "replicas": spec.replicas,
                "type": spec.type,
                "port": spec.port,
                "public": bool(spec.public),
                "health_check_path": spec.health_check_path,
                "launch_type": launch_type,
                "mounts": svc_mounts,
                "env": dict(spec.env or {}),
                "command": list(spec.command or []),
                # Pre-built compose image; if set (and no build context), task
                # def uses it verbatim instead of an ECR placeholder.
                "compose_image": spec.image if not spec.build_context else None,
                "has_build_context": bool(spec.build_context),
                "ephemeral_storage": spec.ephemeral_storage,
            }
            services_view.append(svc_view)
            if launch_type == "EC2":
                ec2_demands.append(EC2TaskDemand(
                    name=name, cpu_units=spec.cpu, memory_mib=spec.memory,
                    replicas=spec.replicas,
                ))
            if spec.public and spec.port and default_public is None:
                default_public = svc_view

        has_public_service = default_public is not None
        has_ec2_service = len(ec2_demands) > 0
        has_efs = len(efs_volumes) > 0
        # Service discovery is cheap (one Cloud Map namespace + one entry per
        # service) and turns multi-service compose into ECS that actually
        # talks to itself. Enable whenever there is more than one service.
        has_service_discovery = len(ctx.services) > 1

        # Secrets: split into file (terraform-created) vs aws_sm (pre-existing ARN ref).
        #
        # For source=file secrets, the provider reads KEY NAMES from the file
        # at emit time (values are never read) so every key gets its own
        # entry in the task def's `secrets` block via ECS's JSON-key syntax
        # `arn:KEY::`. Without this, the whole file's contents would arrive
        # in the container as one env var — useless for any real app.
        #
        # Values are uploaded out-of-band by `rc secrets push` which packs
        # the file into a JSON blob on the SM secret.
        file_secrets: list[dict[str, Any]] = []
        aws_sm_secrets: list[dict[str, Any]] = []
        secrets_view: list[dict[str, Any]] = []
        project_dir = (ctx.compose_path.parent if ctx.compose_path else Path.cwd())
        for sec in ctx.secrets or []:
            if sec.source == "file":
                tf_sec_name = _tf_name(sec.name)
                if not sec.path:
                    raise ProviderConfigError(
                        f"secret {sec.name!r}: source=file requires path"
                    )
                file_path = Path(sec.path)
                if not file_path.is_absolute():
                    file_path = (project_dir / file_path).resolve()
                try:
                    file_keys = env_file_keys(file_path)
                except EnvFileError as exc:
                    raise ProviderConfigError(
                        f"secret {sec.name!r}: {exc}"
                    ) from exc
                if not file_keys:
                    raise ProviderConfigError(
                        f"secret {sec.name!r}: {file_path} has no KEY=value entries"
                    )
                file_secrets.append({
                    "name": sec.name,
                    "tf_name": tf_sec_name,
                    "path": str(file_path),
                    "keys": file_keys,
                })
                # One task-def secrets[] entry per KEY in the file, pointing
                # at the same SM secret ARN with a JSON-key selector.
                for key in file_keys:
                    secrets_view.append({
                        "env_name": key,
                        "value_from_ref": (
                            f'"${{aws_secretsmanager_secret.{tf_sec_name}.arn}}'
                            f':{key}::"'
                        ),
                    })
            elif sec.source == "aws_sm":
                if not sec.arn:
                    raise ProviderConfigError(
                        f"secret {sec.name!r}: source=aws_sm requires arn"
                    )
                aws_sm_secrets.append({
                    "name": sec.name,
                    "arn": sec.arn,
                })
                # Pre-existing SM secret; we don't know its shape, so the
                # whole value lands in one env var. Users wanting key splits
                # on aws_sm should reference sub-keys via sec.ref (future).
                secrets_view.append({
                    "env_name": _env_name_for_secret(sec.name),
                    "value_from_ref": f'"{sec.arn}"',
                })
            elif sec.source in {"k8s_secret", "gcp_sm"}:
                # Not applicable to the ECS provider — silently skip. Another
                # provider would consume these.
                continue
            else:
                raise ProviderConfigError(
                    f"secret {sec.name!r}: unknown source {sec.source!r}"
                )

        has_secrets = len(secrets_view) > 0
        has_file_secrets = len(file_secrets) > 0
        # ARNs needed by the task-exec role policy. For file-sourced, we
        # substitute the terraform reference string; for aws_sm, the literal.
        all_secret_arns: list[str] = []
        for sec in file_secrets:
            all_secret_arns.append(f"${{aws_secretsmanager_secret.{sec['tf_name']}.arn}}")
        for sec in aws_sm_secrets:
            all_secret_arns.append(sec["arn"])

        # Attach secrets to every service view so each task def gets them.
        for svc_view in services_view:
            svc_view["secrets"] = secrets_view
        default_target_port = default_public["port"] if default_public else 80
        default_health_check_path = (
            (default_public or {}).get("health_check_path") or "/"
        )

        ec2_capacity_cfg = self._resolve_ec2_capacity(ecs_cfg, ec2_demands) if has_ec2_service else None

        domain_info = self._resolve_domain(ctx, ecs_cfg, has_public_service)

        environment = "rc-test" if ctx.project.startswith("rc-test-") else None

        context: dict[str, Any] = {
            "project": ctx.project,
            "region": region,
            "vpc_cidr": vpc_cidr,
            "cluster_name": cluster_name,
            "aws_profile": aws_profile,
            "environment": environment,
            "services": services_view,
            "has_public_service": has_public_service,
            "has_ec2_service": has_ec2_service,
            "has_service_discovery": has_service_discovery,
            "ec2_capacity": ec2_capacity_cfg,
            "has_efs": has_efs,
            "efs_volumes": sorted(efs_volumes.values(), key=lambda v: v["name"]),
            "service_volume_mounts": service_volume_mounts,
            "has_secrets": has_secrets,
            "has_file_secrets": has_file_secrets,
            "file_secrets": file_secrets,
            "all_secret_arns": all_secret_arns,
            "secrets": secrets_view,
            "has_domain": domain_info is not None,
            "domain": (domain_info or {}).get("domain"),
            "zone": (domain_info or {}).get("zone"),
            "tls_mode": (domain_info or {}).get("tls_mode"),
            "certificate_arn": (domain_info or {}).get("certificate_arn"),
            "default_target_port": default_target_port,
            "default_health_check_path": default_health_check_path,
            "backend_block": render_backend_block(ctx.tf_backend_config or {"type": "local"}),
        }

        self.emitter.render(context, out_dir)
        (out_dir / "README.md").write_text(_README_TEMPLATE.format(project=ctx.project))
        return out_dir

    # -----------------------------------------------------------------
    # Plan / Deploy / Destroy / Redeploy
    # -----------------------------------------------------------------

    def plan(self, ctx: DeployContext) -> PlanResult:
        out_dir = self._tf_dir(ctx)
        self.emit_terraform(ctx, out_dir)
        runner = self.runner_factory(out_dir)
        runner.init()
        summary = runner.plan()
        return PlanResult(
            create=summary.create,
            update=summary.update,
            destroy=summary.destroy,
            raw_plan=summary.raw,
        )

    def deploy(self, ctx: DeployContext) -> DeployResult:
        start = time.monotonic()
        out_dir = self._tf_dir(ctx)
        self.emit_terraform(ctx, out_dir)
        runner = self.runner_factory(out_dir)
        runner.init()
        runner.apply()
        outputs = runner.output()

        warnings: list[str] = []
        pushed = self._build_and_push_images(ctx, outputs, warnings)
        if pushed:
            # ECS won't pull a new :latest automatically — force it.
            self._force_new_deployments(ctx, pushed)

        return DeployResult(
            revision_id=_revision_id_from_dir(out_dir),
            services=sorted(ctx.services.keys()),
            duration_s=time.monotonic() - start,
            terraform_outputs=outputs,
            warnings=warnings,
        )

    def _build_and_push_images(
        self,
        ctx: DeployContext,
        outputs: dict,
        warnings: list,
    ) -> list[str]:
        """Build each service that has a compose build: context, push to its ECR repo.

        Returns the list of service names that were pushed (caller forces
        new deployments for exactly these).
        """
        to_build = [s for s in ctx.services.values() if s.build_context]
        if not to_build:
            return []

        repos = (outputs.get("ecr_repositories") or {}).get("value") or {}
        if not repos:
            warnings.append(
                "terraform outputs missing ecr_repositories — skipping image build+push"
            )
            return []

        from ...image import ImageBuildSpec, ImageBuilder, ImagePusher
        from .ecr_auth import ECRAuthenticator

        builder = ImageBuilder(progress=self.progress)
        session = self.session_factory(ctx)
        auth = ECRAuthenticator(session=session)
        pusher = ImagePusher(authenticator=auth, progress=self.progress)

        pushed: list[str] = []
        for spec in to_build:
            repo_url = repos.get(spec.name)
            if not repo_url:
                warnings.append(
                    f"service {spec.name!r}: no ECR repo in terraform outputs"
                )
                continue
            tag = f"{repo_url}:latest"
            build = ImageBuildSpec(
                service=spec.name,
                context=spec.build_context,
                dockerfile=Path(spec.dockerfile) if spec.dockerfile else None,
                build_args=dict(spec.build_args or {}),
                tags=[tag],
                platform="linux/amd64",
            )
            tags = builder.build(build)
            pusher.push(tags)
            pushed.append(spec.name)
        return pushed

    def _force_new_deployments(self, ctx: DeployContext, services: list[str]) -> None:
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        session = self.session_factory(ctx)
        client = session.client("ecs")
        for svc in services:
            client.update_service(
                cluster=cluster,
                service=svc,
                forceNewDeployment=True,
            )

    def destroy(self, ctx: DeployContext) -> None:
        out_dir = self._tf_dir(ctx)
        if not out_dir.exists():
            self.emit_terraform(ctx, out_dir)
        runner = self.runner_factory(out_dir)
        runner.init()
        runner.destroy()

    def redeploy(
        self,
        ctx: DeployContext,
        services: Optional[list[str]] = None,
    ) -> DeployResult:
        """Force a new task-def revision per service without re-running terraform apply.

        Uses the AWS SDK: ``ecs.update_service(forceNewDeployment=True)``.
        Terraform state is untouched.
        """
        start = time.monotonic()
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        targets = services or sorted(ctx.services.keys())

        session = self.session_factory(ctx)
        client = session.client("ecs")
        for svc in targets:
            client.update_service(
                cluster=cluster,
                service=svc,
                forceNewDeployment=True,
            )
        return DeployResult(
            revision_id=f"force-{int(time.time())}",
            services=list(targets),
            duration_s=time.monotonic() - start,
            warnings=[],
        )

    # -----------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------

    def status(self, ctx: DeployContext) -> StatusReport:
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        session = self.session_factory(ctx)
        client = session.client("ecs")

        service_names = sorted(ctx.services.keys())
        if not service_names:
            return StatusReport(services=[], cluster_health="inactive")

        resp = client.describe_services(cluster=cluster, services=service_names)
        reported = {s["serviceName"]: s for s in resp.get("services", [])}

        statuses: list[ServiceStatus] = []
        for name in service_names:
            s = reported.get(name)
            if s is None:
                statuses.append(ServiceStatus(
                    name=name, desired=0, running=0, health="unknown",
                    last_event="service not found",
                ))
                continue
            running = int(s.get("runningCount", 0))
            desired = int(s.get("desiredCount", 0))
            health = "healthy" if running == desired and desired > 0 else "degraded"
            events = s.get("events") or []
            last_event = events[0]["message"] if events else None
            statuses.append(ServiceStatus(
                name=name, desired=desired, running=running,
                health=health, last_event=last_event,
            ))

        cluster_health = (
            "healthy"
            if all(s.running == s.desired and s.desired > 0 for s in statuses)
            else "degraded"
        )
        ingress_url: Optional[str] = None
        # if terraform module emitted, pull ALB DNS from outputs
        out_dir = self._tf_dir(ctx)
        if out_dir.exists():
            try:
                runner = self.runner_factory(out_dir)
                outputs = runner.output()
                alb_dns = (outputs.get("alb_dns_name") or {}).get("value")
                if alb_dns:
                    ingress_url = f"http://{alb_dns}"
            except Exception:
                pass
        return StatusReport(
            services=statuses,
            cluster_health=cluster_health,
            ingress_url=ingress_url,
        )

    # -----------------------------------------------------------------
    # Rollback — remote-backend only for now
    # -----------------------------------------------------------------

    def rollback(
        self,
        ctx: DeployContext,
        to_revision: Optional[str] = None,
    ) -> DeployResult:
        backend = (ctx.tf_backend_config or {}).get("type", "local")
        if backend == "local":
            raise ProviderError(
                "rollback is not supported on the local terraform backend; "
                "configure an s3/gcs/remote backend with state history, "
                "or redeploy a prior rc.yml."
            )
        raise NotImplementedError(
            "remote-backend rollback will land in a follow-up — for now, "
            "manage via `terraform state` directly against your backend"
        )

    # -----------------------------------------------------------------
    # Logs / exec — thin boto3 wrappers, full UX lands with 6b.1 polish
    # -----------------------------------------------------------------

    def logs(
        self,
        ctx: DeployContext,
        service: str,
        follow: bool = False,
        tail: int = 100,
    ) -> Iterator[str]:
        log_group = f"/ecs/{ctx.project}"
        stream_prefix = f"{service}/"
        session = self.session_factory(ctx)
        client = session.client("logs")
        # CloudWatch Logs forbids combining logStreamNamePrefix with orderBy,
        # so pull prefix-matching streams then sort client-side for "most recent".
        streams = client.describe_log_streams(
            logGroupName=log_group,
            logStreamNamePrefix=stream_prefix,
            limit=50,
        ).get("logStreams", [])
        if not streams:
            return iter([])
        streams.sort(
            key=lambda s: s.get("lastEventTimestamp") or s.get("creationTime") or 0,
            reverse=True,
        )
        stream_name = streams[0]["logStreamName"]
        events = client.get_log_events(
            logGroupName=log_group,
            logStreamName=stream_name,
            limit=tail,
            startFromHead=False,
        ).get("events", [])
        return iter(e["message"] for e in events)

    def exec(
        self,
        ctx: DeployContext,
        service: str,
        command: list[str],
        interactive: bool = False,
    ) -> ExecResult:
        """Delegates to ``ecs execute-command`` via SSM. Full interactive path lives in cli.py."""
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        session = self.session_factory(ctx)
        client = session.client("ecs")

        tasks = client.list_tasks(
            cluster=cluster, serviceName=service, desiredStatus="RUNNING"
        ).get("taskArns") or []
        if not tasks:
            return ExecResult(exit_code=1, stdout="", stderr=f"no running tasks for {service}")

        resp = client.execute_command(
            cluster=cluster,
            task=tasks[0],
            interactive=interactive,
            command=" ".join(command),
        )
        # Non-interactive path doesn't stream; for a real shell the CLI uses
        # `aws ecs execute-command` interactively (see remote_compose/cli.py).
        session_meta = resp.get("session") or {}
        return ExecResult(
            exit_code=0,
            stdout=str(session_meta.get("sessionId", "")),
            stderr="",
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _tf_dir(self, ctx: DeployContext) -> Path:
        return Path(ctx.working_dir) / "terraform"

    def _resolve_domain(
        self,
        ctx: DeployContext,
        ecs_cfg: dict,
        has_public_service: bool,
    ) -> Optional[dict]:
        """Resolve custom domain + TLS from rc.yml v2.

        Returns None when no domain is configured, or when there is no public
        service to attach it to (can't HTTPS a worker-only deployment).
        """
        domain = ecs_cfg.get("domain") or (ctx.rc_yml_v2 or {}).get("domain")
        if not domain:
            return None
        if not has_public_service:
            raise ProviderConfigError(
                f"domain {domain!r} is set but no service is marked public; "
                "there is nothing to route traffic to"
            )
        zone = _zone_from_domain(domain)

        tls = (ctx.rc_yml_v2 or {}).get("tls") or {}
        tls_mode = tls.get("mode", "acm")
        if tls_mode not in {"acm", "manual"}:
            raise ProviderConfigError(
                f"tls.mode {tls_mode!r} is not supported by the ECS provider "
                "(supported: acm, manual)"
            )
        certificate_arn = tls.get("certificate_arn")
        if tls_mode == "manual" and not certificate_arn:
            raise ProviderConfigError(
                "tls.mode=manual requires tls.certificate_arn"
            )
        return {
            "domain": domain,
            "zone": zone,
            "tls_mode": tls_mode,
            "certificate_arn": certificate_arn,
        }

    def _resolve_ec2_capacity(
        self, ecs_cfg: dict, ec2_demands: list[EC2TaskDemand]
    ) -> dict:
        """Merge user-supplied ec2_capacity with auto-sized defaults.

        User-supplied instance_type, min/max/desired all take precedence.
        Auto-sizing fills in gaps.
        """
        user_cfg = ecs_cfg.get("ec2_capacity") or {}
        capacity_type = user_cfg.get("capacity_type", "ON_DEMAND")
        if capacity_type not in {"ON_DEMAND", "SPOT", "MIXED"}:
            raise ProviderConfigError(
                f"ec2_capacity.capacity_type must be ON_DEMAND, SPOT, or MIXED, "
                f"got {capacity_type!r}"
            )

        instance_type = user_cfg.get("instance_type")
        if instance_type is None:
            sizing = auto_size(ec2_demands)
            instance_type = sizing.instance_type
            min_size = user_cfg.get("min", sizing.min_size)
            desired_size = user_cfg.get("desired", sizing.desired_size)
            max_size = user_cfg.get("max", sizing.max_size)
        else:
            # User picked the shape; ASG numbers come from config (with sane defaults).
            min_size = user_cfg.get("min", 1)
            desired_size = user_cfg.get("desired", 1)
            max_size = user_cfg.get("max", 3)

        return {
            "instance_type": instance_type,
            "min_size": min_size,
            "desired_size": desired_size,
            "max_size": max_size,
            "capacity_type": capacity_type,
            "spot_weight": user_cfg.get("spot_weight", 3),
        }


_EMITTED_SUFFIXES = (".tf",)
_EMITTED_EXTRAS = {"README.md"}
_IGNORED_TOP_LEVEL = {".terraform", ".terraform.lock.hcl",
                      "terraform.tfstate", "terraform.tfstate.backup"}


def _revision_id_from_dir(out_dir: Path) -> str:
    """Deterministic revision id from the byte content of emitter output.

    Hashes only files the emitter writes (``*.tf``, ``README.md``); excludes
    terraform's own artifacts (``.terraform/``, ``terraform.tfstate*``,
    ``.terraform.lock.hcl``) which change after ``terraform apply`` even
    when inputs are identical.

    Two ``deploy()`` calls with identical inputs must yield identical ids.
    """
    h = hashlib.sha256()
    for p in sorted(out_dir.iterdir()):
        if p.name in _IGNORED_TOP_LEVEL:
            continue
        if not p.is_file():
            continue
        if not (p.suffix in _EMITTED_SUFFIXES or p.name in _EMITTED_EXTRAS):
            continue
        h.update(p.name.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


_README_TEMPLATE = """# Terraform module for {project}

Generated by remote-compose (ECS provider).

## Standalone usage

    terraform init
    terraform plan
    terraform apply

This module is self-contained. You can commit it to your own IaC repo and stop
using remote-compose for infrastructure changes at any point. remote-compose
remains useful for image build/push, `rc exec`, and `rc logs`, all of which
read from this module's terraform state.
"""
