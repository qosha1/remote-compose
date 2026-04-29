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

from ...defaults import VPC_CIDR_DEFAULT
from ...envfile import EnvFileError, keys as env_file_keys
from ...terraform.backend import render_backend_block
from ...terraform.emitter import TerraformEmitter
from ...terraform.runner import TerraformError, TerraformRunner
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


_SINGLETON_NAME_SUFFIXES = ("-beat", "-scheduler", "-cron")
_SINGLETON_COMMAND_RE = re.compile(
    r"\b(celery\s+(?:-A\s+\S+\s+)?beat\b|celerybeat\b)",
    re.IGNORECASE,
)


def _looks_like_singleton_scheduler(name: str, command: list[str]) -> bool:
    """True when this service looks like a singleton scheduler that breaks
    under default rolling deploy semantics.

    Two signals:
      1. Service name suffix: '-beat', '-scheduler', '-cron'.
      2. Command-line markers: 'celery ... beat' / 'celerybeat'.

    Both heuristics are conservative — better to apply stop-then-start
    semantics to a service that didn't strictly need it (small deploy
    delay) than to leave a flap loop. See rc-e5u.46.10.
    """
    lname = name.lower()
    if any(lname.endswith(suf) for suf in _SINGLETON_NAME_SUFFIXES):
        return True
    if not command:
        return False
    cmd_str = " ".join(str(c) for c in command)
    return bool(_SINGLETON_COMMAND_RE.search(cmd_str))


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
        vpc_cidr = ecs_cfg.get("vpc_cidr", VPC_CIDR_DEFAULT)
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
        # Dev-mode source mounts (rc-e5u.45.8). Only populated when
        # ctx.dev_mode is True AND at least one service declares
        # dev_volumes. ALL dev mounts share ONE EFS file system per
        # project (cheaper, simpler) named '<project>-dev'; each entry
        # gets its own access point rooted at /<service>__<name>.
        dev_mode_active = bool(getattr(ctx, "dev_mode", False))
        dev_efs_volume: Optional[dict[str, Any]] = None
        dev_volume_mounts: list[dict[str, Any]] = []

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
                # Per-service posix user for the EFS access point. Default
                # 1000:1000 works for most app images. Stateful images that
                # run as a non-standard uid must override — e.g. postgres
                # :16-alpine runs as uid=70, redis:7-alpine as uid=999.
                try:
                    vol_uid = int(vol_entry.get("uid", 1000))
                    vol_gid = int(vol_entry.get("gid", 1000))
                except (TypeError, ValueError) as exc:
                    raise ProviderConfigError(
                        f"service {name!r} volume {vol_name!r}: uid/gid must "
                        f"be integers, got {vol_entry!r}"
                    ) from exc
                vol_mode = vol_entry.get("mode", "0755")
                if not isinstance(vol_mode, str) or not re.fullmatch(
                    r"0[0-7]{3}", vol_mode
                ):
                    raise ProviderConfigError(
                        f"service {name!r} volume {vol_name!r}: mode must be "
                        f"a POSIX octal string like '0755', got {vol_mode!r}"
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
                    "uid": vol_uid,
                    "gid": vol_gid,
                    "mode": vol_mode,
                    "service": name,
                    "access_point_tf_name": ap_tf,
                }
                svc_mounts.append(mount_view)
                service_volume_mounts.append(mount_view)

            # ---- dev_volumes (rc-e5u.45.8) ----
            # Only materialized when the deploy is in dev mode. Skipped
            # entirely otherwise so production stacks are unaffected by
            # the field being present in rc.yml.
            if dev_mode_active and spec.dev_volumes:
                for dv_entry in spec.dev_volumes:
                    dv_name = dv_entry.get("name")
                    dv_mount = dv_entry.get("mount")
                    # Schema validator already enforces these; defensive
                    # so a hand-crafted DeployContext can't crash the
                    # template render with a KeyError.
                    if not dv_name or not dv_mount:
                        raise ProviderConfigError(
                            f"service {name!r}: dev_volumes entry missing "
                            f"name/mount: {dv_entry!r}"
                        )
                    # One shared EFS file system per project — cheaper
                    # and matches the "dev iteration" framing (no point
                    # in per-service file systems for code that's owned
                    # by one developer's laptop).
                    if dev_efs_volume is None:
                        dev_efs_volume = {
                            "name": f"{ctx.project}-dev",
                            "tf_name": "dev",
                        }
                    dv_tf = _tf_name(dv_name)
                    dv_ap_tf = f"{_tf_name(name)}__dev_{dv_tf}"
                    # Each AP needs its own ECS volume entry on the
                    # task def — duplicates of the same `volume name=`
                    # are rejected at register-task-definition time.
                    # Dev mounts share one EFS *file system* but each
                    # gets a per-(service, dev_volume) sourceVolume
                    # name so the task def stays valid.
                    dv_volume_name = f"dev-{_tf_name(name)}-{dv_tf}"
                    # Generic dev defaults: 1000:1000 / 0755 covers the
                    # python:slim, node:alpine, etc. images most users
                    # iterate on. Containers running as a non-standard
                    # uid in dev mode are rare; if it comes up, we'll
                    # add uid/gid to the dev_volumes schema then.
                    dv_mount_view = {
                        "volume": dv_volume_name,
                        # All dev mounts reference the same shared EFS
                        # file system (cheaper than per-mount FS).
                        "volume_tf_name": dev_efs_volume["tf_name"],
                        "mount_path": dv_mount,
                        "uid": 1000,
                        "gid": 1000,
                        "mode": "0755",
                        "service": name,
                        "access_point_tf_name": dv_ap_tf,
                        # Used by the efs.tf template to build the AP's
                        # root_directory.path. Each entry is rooted in
                        # its own dir on the shared FS so two services
                        # mounting different sources don't see each
                        # other's files.
                        "ap_root_path": f"/{_tf_name(name)}__{dv_tf}",
                        "dev": True,
                        "dev_volume_name": dv_name,
                    }
                    svc_mounts.append(dv_mount_view)
                    dev_volume_mounts.append(dv_mount_view)

            # Stateful services that mount EFS cannot safely run two task
            # copies against the same mount (the replacement's entrypoint
            # can race the live primary — postgres initdb will wipe the
            # data dir before the old task realizes it's being replaced).
            # Force stop-then-start for any service with EFS volumes.
            # Dev-mode source mounts are stateless from the engine's POV
            # (just bytes) but still need stop-then-start because two
            # tasks editing the same code dir on EFS = recipe for half-
            # written .pyc files and weird import errors.
            #
            # rc-e5u.46.10: ALSO treat singleton schedulers as stateful
            # even without EFS. Verified .46.6 run #7 against start-simpli
            # — celery-beat in a 5-task flap loop because default
            # min=100/max=200 rolling deploy briefly runs two beat
            # instances → contend for celerybeat-schedule lock → both die.
            # Heuristics: command matches 'celery .* beat' / 'celerybeat',
            # or service name ends in -beat / -scheduler. False-positive
            # cost: a stateless service goes through stop-then-start
            # rolling deploy (slower) instead of overlap. Acceptable.
            singleton = _looks_like_singleton_scheduler(name, spec.command)
            stateful = len(svc_mounts) > 0 or singleton
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
                "stateful": stateful,
                "env": dict(spec.env or {}),
                "command": list(spec.command or []),
                # Pre-built compose image; if set (and no build context), task
                # def uses it verbatim instead of an ECR placeholder.
                "compose_image": spec.image if not spec.build_context else None,
                "has_build_context": bool(spec.build_context),
                "ephemeral_storage": spec.ephemeral_storage,
                # Extra container ports (compose ports[] beyond the primary).
                # Reachable intra-VPC via the tasks SG; not wired to ALB.
                "extra_ports": list(spec.extra_ports or []),
                # Multi-domain routing: when the service declares its own
                # ALB hostname, it gets a dedicated target group + listener
                # rule keyed on Host header.
                "domain": spec.domain if spec.public else None,
                # Extra hostnames the same service answers for. No listener
                # rules; only cert SANs + R53 records. Catch-all default
                # action carries the traffic.
                "aliases": list(spec.aliases or []) if spec.public else [],
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
        # Any service with a compose `build:` context drives BuildKit cache
        # repo creation (rc-e5u.45.2). Pure-image stacks (e.g., a postgres-
        # only side stack) skip the buildcache repo entirely.
        has_build_context_service = any(
            spec.build_context for spec in ctx.services.values()
        )
        # Per-service domain routing. Each service with public=true and
        # domain set gets a dedicated target group + ALB listener rule
        # (host_header) + R53 alias record + ACM cert SAN. The default
        # listener action still forwards to default_public (catch-all).
        domained_services = sorted(
            [s for s in services_view if s.get("domain")],
            key=lambda s: s["domain"],
        )
        # Aliases attach to public services as extra hostnames. They feed
        # into the cert SAN list + R53 records but do NOT generate listener
        # rules — the default action catches traffic for them.
        alias_hostnames: list[str] = []
        for sv in services_view:
            for a in sv.get("aliases", []) or []:
                alias_hostnames.append(a)
        # Listener rules need distinct priorities. Start at 100 and step
        # by 10 so users can hand-write rules in between later.
        for i, dsvc in enumerate(domained_services):
            dsvc["listener_rule_priority"] = 100 + i * 10
        has_domained_services = len(domained_services) > 0
        has_ec2_service = len(ec2_demands) > 0
        # has_efs drives the EFS template (security group, file system,
        # mount targets, access points). True for either persistent OR
        # dev-mode source mounts since both need the same EFS plumbing.
        has_efs = len(efs_volumes) > 0 or dev_efs_volume is not None
        has_dev_efs = dev_efs_volume is not None
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
        # Filter per-service: when a service has a plaintext env override for
        # a key that's also sourced from SM, drop the SM entry from THAT
        # service's secrets[]. ECS rejects task defs where the same key
        # appears in both environment[] and secrets[] ("The secret name
        # must be unique and not shared with any new or existing environment
        # variables"). The plaintext env wins on collision because the user
        # set it explicitly in rc.yml services.<svc>.env. (rc-z30)
        for svc_view in services_view:
            override_keys = set(svc_view.get("env") or {})
            if override_keys:
                svc_view["secrets"] = [
                    s for s in secrets_view
                    if s["env_name"] not in override_keys
                ]
            else:
                svc_view["secrets"] = secrets_view
        default_target_port = default_public["port"] if default_public else 80
        default_health_check_path = (
            (default_public or {}).get("health_check_path") or "/"
        )

        ec2_capacity_cfg = self._resolve_ec2_capacity(ecs_cfg, ec2_demands) if has_ec2_service else None

        # Backup bucket: when rc.yml v2 declares backup.bucket and it is
        # not opted out via bucket_managed=false, terraform creates and
        # owns it. Removes the manual `aws s3api create-bucket` step
        # before any rc db push / rc db backup.
        backup_cfg = (ctx.rc_yml_v2 or {}).get("backup") or {}
        backup_bucket = backup_cfg.get("bucket")
        backup_managed = bool(backup_cfg.get("bucket_managed", True))
        backup_retention = backup_cfg.get("retention_days", 14)
        if backup_retention in (None, "never", 0):
            backup_retention_value: Optional[int] = None
        else:
            backup_retention_value = int(backup_retention)
        has_managed_backup_bucket = bool(backup_bucket) and backup_managed

        domain_info = self._resolve_domain(
            ctx, ecs_cfg, has_public_service,
            domained_services, alias_hostnames,
        )

        environment = "rc-test" if ctx.project.startswith("rc-test-") else None

        context: dict[str, Any] = {
            "project": ctx.project,
            "region": region,
            "vpc_cidr": vpc_cidr,
            "cluster_name": cluster_name,
            "aws_profile": aws_profile,
            "environment": environment,
            # When set, providers.tf default_tags adds Ephemeral=true +
            # ExpiresAt=<iso>. Drives `rc reap` discovery and any
            # out-of-band tag-scan reaper.
            "expires_at": ctx.expires_at,
            "services": services_view,
            "has_public_service": has_public_service,
            "has_build_context_service": has_build_context_service,
            "has_ec2_service": has_ec2_service,
            "has_service_discovery": has_service_discovery,
            "ec2_capacity": ec2_capacity_cfg,
            "has_efs": has_efs,
            "efs_volumes": sorted(efs_volumes.values(), key=lambda v: v["name"]),
            "service_volume_mounts": service_volume_mounts,
            # Dev-mode source mounts (rc-e5u.45.8). When dev_mode is on
            # and any service declares dev_volumes, we provision ONE
            # extra EFS file system tagged DevMode=true (so out-of-band
            # tag scans + reapers can identify it) plus an access point
            # per dev_volumes entry. Production deploys: both are empty
            # and the template emits nothing extra.
            "has_dev_efs": has_dev_efs,
            "dev_efs_volume": dev_efs_volume,
            "dev_volume_mounts": dev_volume_mounts,
            "dev_mode": dev_mode_active,
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
            "san_domains": (domain_info or {}).get("san_domains") or [],
            "all_domains": (domain_info or {}).get("all_domains") or [],
            "default_target_port": default_target_port,
            "default_health_check_path": default_health_check_path,
            "domained_services": domained_services,
            "has_domained_services": has_domained_services,
            "default_target_tf_name": (default_public or {}).get("tf_name"),
            # When the default_target service ALSO declares its own domain,
            # its per-service TG IS the default — emitting a separate empty
            # aws_lb_target_group.default would 503 on unmatched hosts.
            "default_target_has_own_tg": bool(
                default_public and default_public.get("domain")
            ),
            "backend_block": render_backend_block(ctx.tf_backend_config or {"type": "local"}),
            "has_managed_backup_bucket": has_managed_backup_bucket,
            "backup_bucket": backup_bucket,
            "backup_retention_days": backup_retention_value,
            "is_rc_test": ctx.project.startswith("rc-test-"),
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
        # Compose-file detectors (rc-e5u.44.6/.7/.8/.9) flag silently-
        # dropped bind mounts, ephemeral data, dev-only DNS, and
        # unreachable secondary ports. Run here so any caller of
        # provider.plan(ctx) — not only the CLI dispatcher — gets them.
        from ...compose_warnings import collect_compose_warnings
        warnings = collect_compose_warnings(ctx.compose_path, ctx.rc_yml_v2)
        return PlanResult(
            create=summary.create,
            update=summary.update,
            destroy=summary.destroy,
            raw_plan=summary.raw,
            warnings=warnings,
        )

    def deploy(
        self,
        ctx: DeployContext,
        services_filter: Optional[list[str]] = None,
        tag: Optional[str] = None,
    ) -> DeployResult:
        # Validate the filter early so a typo doesn't quietly skip all builds.
        if services_filter is not None:
            unknown = set(services_filter) - set(ctx.services.keys())
            if unknown:
                raise ValueError(
                    f"--services lists service(s) not in this stack: {sorted(unknown)}. "
                    f"Known: {sorted(ctx.services.keys())}"
                )

        # No-state deploy mode (rc-5h8.11): when ctx.skip_terraform is True,
        # we bypass emit_terraform / init / apply / outputs entirely and just
        # rebuild + push images + force-roll services. Used by hybrid
        # v2-task-defs-on-v1-imperative-infra stacks (start-simpli-api today)
        # where there is no terraform state to manage. ECR repo URLs come
        # from boto3 describe-repositories instead of terraform outputs.
        if getattr(ctx, "skip_terraform", False):
            return self._deploy_no_state(ctx, services_filter, tag)

        start = time.monotonic()
        out_dir = self._tf_dir(ctx)
        self.emit_terraform(ctx, out_dir)
        # rc-ysh: detect held local state lock BEFORE invoking terraform so
        # we surface the holder PID in <1s instead of inheriting terraform's
        # subprocess-output buffering and retry loops.
        self._check_local_state_lock(out_dir, ctx)
        runner = self.runner_factory(out_dir)
        runner.init()
        self._reconcile_orphan_log_groups(ctx, runner)
        runner.apply()
        outputs = runner.output()

        warnings: list[str] = []
        pushed = self._build_and_push_images(
            ctx, outputs, warnings,
            services_filter=services_filter, requested_tag=tag,
        )
        if pushed and not getattr(ctx, "skip_force_roll", False):
            # ECS won't pull a new :latest automatically — force it.
            # rc-1bk: rc up sets skip_force_roll=True so the rollout happens
            # AFTER `rc secrets push` populates SM, avoiding the cold-start
            # CannotPullSecrets cascade.
            self._force_new_deployments(ctx, pushed)

        return DeployResult(
            revision_id=_revision_id_from_dir(out_dir),
            services=sorted(services_filter) if services_filter else sorted(ctx.services.keys()),
            duration_s=time.monotonic() - start,
            terraform_outputs=outputs,
            warnings=warnings,
        )

    def _deploy_no_state(
        self,
        ctx: DeployContext,
        services_filter: Optional[list[str]],
        tag: Optional[str],
    ) -> DeployResult:
        """Boto3-only deploy: rebuild + push images + force-roll services.

        Skips every terraform step. Used for stacks where the infrastructure
        is NOT under terraform management — typically v1-imperative stacks
        that have been cut over to v2 task-def shape but where the underlying
        VPC/ALB/EFS/SM resources are still managed externally. The user can
        roll new code from any box because this path requires only AWS
        credentials, not local terraform state.

        ECR repo URLs are discovered via boto3 describe-repositories
        filtered to repos whose names start with ``<project>/``. Falls back
        to ``<project>-<service>`` for legacy single-flat-namespace setups.
        """
        start = time.monotonic()
        warnings: list[str] = []

        # Synthesize a terraform-outputs-shaped dict so _build_and_push_images
        # can be reused unchanged. Keys: repo URL keyed by service name.
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        region = ecs_cfg.get("region")
        session = self.session_factory(ctx)
        ecr = session.client("ecr", region_name=region)

        repo_urls: dict[str, str] = {}
        # Query ECR for every repo we might use. Try several naming
        # conventions in order:
        #   1. <project>/<svc>        — v2 convention
        #   2. <project>-<svc>        — v1 imperative flat
        #   3. <cluster_prefix>/<svc> — legacy stacks where the rc.yml
        #      project field was renamed (label change) but the AWS
        #      resources (cluster + ECR) still carry the original prefix.
        #      Cluster 'ss-debuggai-prod' → prefix 'ss-debuggai'.
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        # Strip common env suffixes from the cluster to get the legacy
        # project prefix.
        cluster_prefix = cluster
        for suffix in ("-prod", "-staging", "-dev", "-cluster"):
            if cluster_prefix.endswith(suffix):
                cluster_prefix = cluster_prefix[: -len(suffix)]
                break
        wanted_repos: dict[str, list[str]] = {}
        for svc_name, spec in ctx.services.items():
            if not spec.build_context:
                continue
            candidates = [
                f"{ctx.project}/{svc_name}",
                f"{ctx.project}-{svc_name}",
            ]
            if cluster_prefix and cluster_prefix != ctx.project:
                candidates.append(f"{cluster_prefix}/{svc_name}")
                candidates.append(f"{cluster_prefix}-{svc_name}")
            wanted_repos[svc_name] = candidates

        # Single describe call, paginate.
        repo_index: dict[str, str] = {}
        paginator = ecr.get_paginator("describe_repositories")
        for page in paginator.paginate():
            for repo in page.get("repositories") or []:
                repo_index[repo["repositoryName"]] = repo["repositoryUri"]

        for svc_name, candidates in wanted_repos.items():
            for cand in candidates:
                if cand in repo_index:
                    repo_urls[svc_name] = repo_index[cand]
                    break
            else:
                warnings.append(
                    f"service {svc_name!r}: no ECR repo found "
                    f"(tried {candidates}); skipping image build+push"
                )

        synthetic_outputs = {
            "ecr_repositories": {"value": repo_urls},
        }

        pushed = self._build_and_push_images(
            ctx, synthetic_outputs, warnings,
            services_filter=services_filter, requested_tag=tag,
        )
        if pushed:
            self._force_new_deployments(ctx, pushed)

        return DeployResult(
            revision_id=f"{ctx.project}-no-state-{int(start)}",
            services=sorted(services_filter) if services_filter else sorted(ctx.services.keys()),
            duration_s=time.monotonic() - start,
            terraform_outputs={},
            warnings=warnings,
        )

    def _build_and_push_images(
        self,
        ctx: DeployContext,
        outputs: dict,
        warnings: list,
        services_filter: Optional[list[str]] = None,
        requested_tag: Optional[str] = None,
    ) -> list[str]:
        """Build each service that has a compose build: context, push to its ECR repo.

        Returns the list of service names that were pushed/rolled (caller forces
        new deployments for exactly these). When ``services_filter`` is set,
        only those services are built; others retain their existing image.

        When ``requested_tag`` is set (and not 'latest'), check ECR first:
          - If <repo>:<tag> already exists, skip docker build entirely and
            re-tag the existing image to :latest in ECR (so the task def's
            :latest reference picks it up). Pure ECR API call, ~2-5s.
          - Otherwise build with [<repo>:<tag>, <repo>:latest] tags + push both.
        Used by ``rc deploy --services X --tag v1.2`` for instant rollback /
        deploy of known-good images. See rc-e5u.45.3.
        """
        to_build = [s for s in ctx.services.values() if s.build_context]
        if services_filter is not None:
            allowed = set(services_filter)
            to_build = [s for s in to_build if s.name in allowed]
        if not to_build:
            # rc-8q4: don't silently return — surface why the build phase
            # produced nothing so the user knows whether tasks will pull
            # pre-existing images or fail with CannotPullContainerError.
            buildable = sum(1 for s in ctx.services.values() if s.build_context)
            if buildable == 0:
                self._emit(
                    "  No images to build (no services declare build context)."
                )
            else:
                self._emit(
                    f"  No matched services to build "
                    f"(filter excludes all {buildable} build_context service(s))."
                )
            return []

        repos = (outputs.get("ecr_repositories") or {}).get("value") or {}
        if not repos:
            msg = (
                "terraform outputs missing ecr_repositories — skipping image build+push"
            )
            warnings.append(msg)
            self._emit(f"  WARN: {msg}")
            return []

        # Shared BuildKit cache repo (rc-e5u.45.2). Optional — older stacks
        # whose terraform predates the buildcache resource won't have this
        # output and we just degrade to no-cache builds.
        buildcache_repo = (
            (outputs.get("buildcache_repository") or {}).get("value")
        )

        from ...image import ImageBuildSpec, ImageBuilder, ImagePusher
        from .ecr_auth import ECRAuthenticator

        builder = ImageBuilder(progress=self.progress)
        session = self.session_factory(ctx)
        auth = ECRAuthenticator(session=session)
        pusher = ImagePusher(authenticator=auth, progress=self.progress)

        # Pre-authenticate the cache registry so buildx can pull/push cache
        # layers. The pusher will re-auth the per-service registry (same
        # ECR account/region in practice; ECRAuthenticator caches by host).
        if buildcache_repo:
            cache_host = buildcache_repo.split("/", 1)[0]
            try:
                auth(cache_host)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"buildcache auth failed ({cache_host}): {exc!s} — "
                    f"falling back to no-cache builds"
                )
                buildcache_repo = None

        # When user passed --tag X (and X != latest), see if X already
        # exists in ECR and short-circuit to "re-tag existing → latest".
        ecr_client = None
        skip_when_tag_exists = (
            requested_tag is not None and requested_tag != "latest"
        )

        pushed: list[str] = []
        for spec in to_build:
            repo_url = repos.get(spec.name)
            if not repo_url:
                msg = (
                    f"service {spec.name!r}: no ECR repo in terraform outputs"
                )
                warnings.append(msg)
                self._emit(f"  WARN: {msg}; skipping image build+push")
                continue
            # ECR repo URL: <account>.dkr.ecr.<region>.amazonaws.com/<repo_path>
            # repo_path can include slashes (e.g., 'test-proj/django') so strip
            # only the registry host, not the last segment.
            repo_name = repo_url.split("/", 1)[1] if "/" in repo_url else repo_url
            latest_tag = f"{repo_url}:latest"

            if skip_when_tag_exists:
                if ecr_client is None:
                    ecr_client = session.client("ecr")
                manifest = self._ecr_image_manifest(
                    ecr_client, repo_name, requested_tag,
                )
                if manifest is not None:
                    # Image exists in ECR — skip docker build entirely.
                    self._ecr_retag(ecr_client, repo_name, manifest, "latest")
                    pushed.append(spec.name)
                    if self.progress:
                        self.progress(
                            f"  {spec.name}: re-tagged ECR "
                            f"{repo_name}:{requested_tag} → :latest "
                            f"(skipped docker build)"
                        )
                    continue
                # Tag wasn't in ECR — fall through to a normal build, but
                # tag the resulting image with BOTH the requested tag AND
                # :latest so a re-run of the same command takes the fast path.

            cache_from: list[str] = []
            cache_to: list[str] = []
            if buildcache_repo:
                cache_ref = f"{buildcache_repo}:{spec.name}-cache"
                cache_from = [cache_ref]
                cache_to = [cache_ref]
            build_tags = [latest_tag]
            if requested_tag and requested_tag != "latest":
                build_tags.insert(0, f"{repo_url}:{requested_tag}")
            build = ImageBuildSpec(
                service=spec.name,
                context=spec.build_context,
                dockerfile=Path(spec.dockerfile) if spec.dockerfile else None,
                target=spec.target,
                build_args=dict(spec.build_args or {}),
                tags=build_tags,
                platform="linux/amd64",
                cache_from=cache_from,
                cache_to=cache_to,
            )
            tags = builder.build(build)
            pusher.push(tags)
            pushed.append(spec.name)
        return pushed

    @staticmethod
    def _ecr_image_manifest(
        ecr_client: Any, repo_name: str, tag: str,
    ) -> Optional[str]:
        """Return the image manifest JSON for ``<repo>:<tag>`` or None if the
        tag doesn't exist. Other ECR errors propagate so the caller sees them
        rather than silently rebuilding (which would mask perms problems)."""
        try:
            resp = ecr_client.batch_get_image(
                repositoryName=repo_name,
                imageIds=[{"imageTag": tag}],
            )
        except Exception as exc:  # noqa: BLE001
            # Permission / throttling / network. Surface clearly + skip
            # the fast path; caller will fall through to a normal build.
            if "ImageNotFoundException" in repr(exc):
                return None
            raise
        images = resp.get("images") or []
        if not images:
            return None
        return images[0].get("imageManifest")

    @staticmethod
    def _ecr_retag(
        ecr_client: Any, repo_name: str, manifest: str, new_tag: str,
    ) -> None:
        """Apply ``new_tag`` to the image identified by ``manifest`` in
        ``repo_name``. Idempotent — ECR's put_image with an existing manifest
        + an existing tag is a no-op."""
        try:
            ecr_client.put_image(
                repositoryName=repo_name,
                imageManifest=manifest,
                imageTag=new_tag,
            )
        except Exception as exc:  # noqa: BLE001
            # ImageAlreadyExistsException happens when the same manifest is
            # already tagged this way (i.e., the previous deploy used the
            # same image). That's the desired end-state — succeed silently.
            if "ImageAlreadyExistsException" in repr(exc):
                return
            raise

    def _emit(self, message: str) -> None:
        """Always-on output sink for reconcile + retry warnings (rc-e5u.46.9).

        Falls back to stderr when self.progress is None so silent-fail paths
        don't disappear into the void during 'rc up'. Tests inject a
        progress callback to capture the messages without coupling to
        sys.stderr; production paths (cli.py + cli_v2.py) inject the
        click.echo bridge.
        """
        if self.progress:
            self.progress(message)
            return
        import sys
        print(message, file=sys.stderr)

    def _check_local_state_lock(
        self, out_dir: Path, ctx: DeployContext,
    ) -> None:
        """Pre-flight: surface a held local terraform state lock fast.

        rc-ysh: when a previous rc invocation crashed or is in flight in
        the same dir, terraform's lock-acquire retry loop can hang for
        10+ minutes with stderr buffered. Stat the lock file ourselves
        and raise a TerraformError with the holder PID inside 1ms so the
        user knows immediately whether to wait or force-unlock.

        Only applies to the local backend; s3+dynamodb already produces
        a clear error via terraform's stock 'state lock' message that
        cli_commands/deploy.py converts to a friendly ClickException.
        """
        backend_type = (ctx.tf_backend_config or {}).get("type", "local")
        if backend_type != "local":
            return
        lock_file = out_dir / ".terraform.tfstate.lock.info"
        if not lock_file.exists():
            return
        import json as _json
        try:
            info = _json.loads(lock_file.read_text())
        except (OSError, _json.JSONDecodeError):
            info = {}
        pid = info.get("PID", "?")
        who = info.get("Who", "unknown")
        created = info.get("Created", "unknown")
        lock_id = info.get("ID", "")
        raise TerraformError(
            cmd=["terraform", "apply"],
            returncode=1,
            stdout="",
            stderr=(
                f"terraform state lock held by PID {pid} ({who}, since "
                f"{created}). Another rc deploy is in flight in this dir; "
                f"wait for it to finish, or `terraform -chdir={out_dir} "
                f"force-unlock {lock_id}` if you're SURE no concurrent "
                f"apply is running."
            ),
        )

    def _reconcile_orphan_log_groups(
        self, ctx: DeployContext, runner: TerraformRunner,
    ) -> None:
        """Import an AWS-side orphan container-insights log group, if any.

        Container Insights auto-creates ``/aws/ecs/containerinsights/<cluster>/performance``
        on first task launch when the log group isn't already present. Stacks
        deployed before terraform managed that log group end up with an
        orphan: AWS owns it, state doesn't, and the next ``terraform apply``
        fails with ResourceAlreadyExistsException.

        Detect the orphan via boto3, then ``terraform import`` it into state
        so the apply that follows is uneventful. Idempotent: if the resource
        is already in state, terraform import errors with "already managed"
        — swallowed.

        Errors are now surfaced via self._emit (rc-e5u.46.9). Earlier
        revisions silently returned on the AWS-side describe failure path,
        which masked a real bug during .46.6 — boto3 raised a
        NoCredentialsError on the second consecutive run, the orphan-import
        never fired, and the ensuing terraform apply blew up with
        ResourceAlreadyExistsException with no breadcrumb pointing at the
        cause.
        """
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster_name = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        log_group_name = (
            f"/aws/ecs/containerinsights/{cluster_name}/performance"
        )

        try:
            session = self.session_factory(ctx)
            logs = session.client("logs")
            resp = logs.describe_log_groups(logGroupNamePrefix=log_group_name)
            groups = resp.get("logGroups", [])
            existing = [
                g for g in groups
                if isinstance(g, dict)
                and g.get("logGroupName") == log_group_name
            ]
            if not existing:
                return
        except Exception as exc:
            # Surface so the user can fix credentials / region / etc.
            # We still proceed (don't raise) — terraform apply may succeed
            # if no orphan actually exists; the user gets an actionable
            # message either way.
            self._emit(
                f"warning: orphan log-group reconcile skipped — "
                f"could not query AWS for {log_group_name}: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        try:
            runner.import_resource(
                "aws_cloudwatch_log_group.container_insights",
                log_group_name,
            )
            self._emit(
                f"imported orphan log group {log_group_name} into "
                f"terraform state"
            )
            return
        except TerraformError as exc:
            msg = ((exc.stderr or "") + (exc.stdout or "")).lower()
            if "already managed" in msg or "already exists in state" in msg:
                return  # already imported on a prior deploy
            # Import failed. Fall through to the boto3-delete fallback
            # below — apply otherwise blows up with
            # ResourceAlreadyExistsException on the same log group.
            self._emit(
                f"orphan log-group import failed ({exc.__class__.__name__}); "
                f"falling back to boto3 delete + recreate-on-apply."
            )

        # Fallback: delete the orphan via boto3 so the upcoming apply
        # creates it fresh under terraform management. Container Insights
        # log groups carry only ephemeral task-lifecycle metadata; AWS
        # auto-recreates the group on first task launch under the new
        # terraform-managed resource. The known triggers for this path:
        # (a) terraform import validates the WHOLE module before
        # importing, and a for_each over a managed-resource attribute
        # (e.g. aws_acm_certificate.main.domain_validation_options for
        # the --domain wiring) is "unknown until apply" — fatal to
        # import even though it's fine for apply itself; (b) the import
        # subprocess racing with another caller. Both cases boil down
        # to "AWS has the orphan, terraform can't import it, apply will
        # die" — delete is the cleanest unblock.
        try:
            logs = self.session_factory(ctx).client("logs")
            logs.delete_log_group(logGroupName=log_group_name)
            self._emit(
                f"deleted orphan log group {log_group_name} via boto3; "
                f"terraform will recreate it under management on apply"
            )
        except Exception as del_exc:  # noqa: BLE001
            self._emit(
                f"warning: orphan log-group fallback delete failed: "
                f"{type(del_exc).__name__}: {del_exc}. terraform apply "
                f"will likely fail with ResourceAlreadyExistsException; "
                f"manually delete via: aws logs delete-log-group "
                f"--log-group-name {log_group_name}"
            )

    # Service-type rollout priority (rc-e5u.46.5). Force-rolls in this order
    # on first deploys so workers + proxies don't race their dependencies on
    # cold start. infrastructure (postgres/redis) → application (django)
    # → worker (celery-*) → proxy (nginx). On steady-state redeploys the
    # ordering is irrelevant (old tasks keep serving while new come up) but
    # is harmless.
    _DEPLOY_ORDER = {
        "infrastructure": 0,
        "application": 1,
        "worker": 2,
        "proxy": 3,
    }

    def _force_new_deployments(self, ctx: DeployContext, services: list[str]) -> None:
        """Force-roll the named ECS services in dependency order (.46.5).

        Cold-start failure mode: when ALL services force-roll simultaneously,
        celery workers race against postgres/redis being healthy + django
        having migrations applied. Workers crash on broker connection,
        ECS exponential backoff stalls them. Ordering by type primes the
        infrastructure first; default ECS deploymentConfiguration (min=100,
        max=200) handles the rest of the convergence naturally.
        """
        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        session = self.session_factory(ctx)
        client = session.client("ecs")

        def priority(svc_name: str) -> tuple[int, str]:
            spec = ctx.services.get(svc_name)
            type_ = spec.type if spec else "application"
            return (self._DEPLOY_ORDER.get(type_, 1), svc_name)

        ordered = sorted(services, key=priority)
        # rc-5h8.11: legacy stacks may have ECS service names prefixed with
        # the original project name (e.g. cluster 'ss-debuggai-prod' →
        # services 'ss-debuggai-django') even after the rc.yml project
        # label was renamed. Probe both: bare name first, then cluster-
        # prefix-+ name.
        cluster_prefix = cluster
        for suffix in ("-prod", "-staging", "-dev", "-cluster"):
            if cluster_prefix.endswith(suffix):
                cluster_prefix = cluster_prefix[: -len(suffix)]
                break
        for svc in ordered:
            candidates = [svc]
            if cluster_prefix and cluster_prefix != ctx.project:
                candidates.append(f"{cluster_prefix}-{svc}")
            last_err: Exception | None = None
            for name in candidates:
                try:
                    client.update_service(
                        cluster=cluster,
                        service=name,
                        forceNewDeployment=True,
                    )
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
            if last_err is not None:
                raise last_err

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

        # rc-e5u.44.24: query the latest revision in each task definition
        # family so we can flag services running on an older revision (i.e.,
        # a previous deploy stuck on it). One ECS API call per family — N+1
        # in service count, acceptable at single-digit-services scale.
        family_latest: dict[str, int] = {}
        for name in service_names:
            family = f"{ctx.project}-{name}"
            try:
                resp_td = client.describe_task_definition(taskDefinition=family)
                family_latest[name] = int(
                    resp_td.get("taskDefinition", {}).get("revision") or 0
                )
            except Exception:  # noqa: BLE001
                # Family doesn't exist (first-deploy) or perms missing —
                # silently skip; the service entry will lack revision data.
                family_latest[name] = 0

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
            # Pull the running revision from the service's taskDefinition ARN.
            # Format: arn:aws:ecs:REGION:ACCT:task-definition/FAMILY:REVISION
            running_rev: Optional[int] = None
            td_arn = s.get("taskDefinition") or ""
            if ":" in td_arn:
                try:
                    running_rev = int(td_arn.rsplit(":", 1)[-1])
                except ValueError:
                    pass
            latest_rev = family_latest.get(name) or None
            base_health = (
                "healthy" if running == desired and desired > 0 else "degraded"
            )
            # Flag stale revisions even when the count side looks healthy —
            # the celery-worker case from .45.8 had running=1 desired=1 but
            # was on a revision behind. See .44.24.
            if running_rev and latest_rev and running_rev < latest_rev:
                health = "stale"
            else:
                health = base_health
            events = s.get("events") or []
            last_event = events[0]["message"] if events else None
            statuses.append(ServiceStatus(
                name=name, desired=desired, running=running,
                health=health, last_event=last_event,
                running_revision=running_rev,
                latest_revision=latest_rev,
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
        timeout: int = 600,
    ) -> ExecResult:
        """Run a command in a live container of the named service.

        Non-interactive path: wraps the user's command in `__RC_BEGIN__`/
        `__RC_EXIT__=$?`/`__RC_END__` sentinels with a final sync+sleep so
        SSM has time to flush stdout before the session closes (which
        otherwise eats the last few KB on fast-exiting commands). Returns
        a real exit code parsed from the sentinel.

        Interactive path (TTY): exec replaces the current process with
        `aws ecs execute-command --interactive` so the user gets a real
        terminal. Caller never observes a return value because the
        process is replaced; this method only returns when the shell
        spawn itself fails.
        """
        import os as _os
        import re as _re
        import shlex
        import subprocess

        ecs_cfg = (ctx.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        region = ecs_cfg.get("region")
        profile = ecs_cfg.get("aws_profile")

        boto = self.session_factory(ctx)
        ecs_client = boto.client("ecs")

        # rc-e5u.46.6: wait for a task that's both RUNNING and has its
        # ExecuteCommandAgent in RUNNING state. Lifecycle auto-hooks fire
        # right after a force-roll; old failing tasks may still be present
        # while new ones are launching, and exec-command requires the new
        # task's SSM agent to be active. Poll for up to 5 minutes — past
        # the typical ~60s startup + agent-registration window. Tests
        # override via RC_EXEC_WAIT_TIMEOUT_S env var to keep mocked
        # ecs_client.list_tasks=[] from looping for 5 min.
        import os as _os_env
        wait_budget = int(_os_env.environ.get("RC_EXEC_WAIT_TIMEOUT_S", "300"))
        wait_interval = float(_os_env.environ.get("RC_EXEC_WAIT_INTERVAL_S", "5"))
        deadline = time.monotonic() + wait_budget
        task_arn: Optional[str] = None
        last_diag = "no running tasks"
        while True:
            tasks = ecs_client.list_tasks(
                cluster=cluster, serviceName=service, desiredStatus="RUNNING"
            ).get("taskArns") or []
            if tasks:
                # Prefer a task whose ExecuteCommandAgent is RUNNING. ECS
                # describe-tasks returns managedAgents per container.
                # Fall through to the bare list_tasks ARN if describe_tasks
                # is unhelpful (mocked test, network blip, missing perms,
                # very fresh task that hasn't reported agents yet) — that
                # matches pre-46.6 behavior. Only DEFER on the explicit
                # "agent reported as not RUNNING" case which is the actual
                # race we're guarding against.
                exec_blocked_tasks: set[str] = set()
                preferred: Optional[str] = None
                try:
                    desc = ecs_client.describe_tasks(cluster=cluster, tasks=tasks)
                    desc_tasks = desc.get("tasks") or []
                except Exception:  # noqa: BLE001
                    desc_tasks = []
                for t in desc_tasks:
                    if not isinstance(t, dict):
                        continue
                    if t.get("lastStatus") != "RUNNING":
                        continue
                    agents_ready = True
                    seen_exec_agent = False
                    containers = t.get("containers") or []
                    if not isinstance(containers, list):
                        containers = []
                    for c in containers:
                        if not isinstance(c, dict):
                            continue
                        for ag in (c.get("managedAgents") or []):
                            if not isinstance(ag, dict):
                                continue
                            if ag.get("name") == "ExecuteCommandAgent":
                                seen_exec_agent = True
                                if ag.get("lastStatus") != "RUNNING":
                                    agents_ready = False
                                break
                    if seen_exec_agent and not agents_ready:
                        exec_blocked_tasks.add(t.get("taskArn") or "")
                        continue
                    preferred = t.get("taskArn")
                    break
                if preferred:
                    task_arn = preferred
                    break
                # describe_tasks didn't give us an agent-ready candidate.
                # If NO task we saw was explicitly blocked, fall through
                # to the first task ARN from list_tasks (pre-46.6 behavior).
                fallback = next(
                    (a for a in tasks if a not in exec_blocked_tasks), None,
                )
                if fallback:
                    task_arn = fallback
                    break
                last_diag = (
                    f"{len(tasks)} task(s) running but exec agent reported "
                    f"as not RUNNING"
                )
            if time.monotonic() > deadline:
                # When wait_budget=0 and there's never been any task, the
                # diagnostic about "stuck on startup" misleads — the user
                # never had a task, period. Use a clearer message in that
                # case (also exercised by tests that mock list_tasks=[]).
                if wait_budget == 0 and last_diag == "no running tasks":
                    msg = (
                        f"no running tasks for service {service!r}. "
                        f"Run `rc deploy` first or check `rc status`."
                    )
                else:
                    msg = (
                        f"timed out ({wait_budget}s) waiting for service "
                        f"{service!r}: {last_diag}. Recent tasks may be "
                        f"stuck on startup; check `rc status` and recent "
                        f"log streams."
                    )
                return ExecResult(exit_code=1, stdout="", stderr=msg)
            # Don't sleep past the deadline — eats wait_interval seconds
            # for nothing on tests that set a 0 budget.
            if time.monotonic() + wait_interval > deadline:
                continue
            time.sleep(wait_interval)

        env = _os.environ.copy()
        if profile:
            env["AWS_PROFILE"] = profile

        if interactive:
            return self._exec_interactive_tty(
                cluster, task_arn, service, command, region, env,
            )
        return self._exec_capture(
            cluster, task_arn, service, command, region, env, timeout,
        )

    _SENTINEL_BEGIN = "__RC_EXEC_BEGIN__"
    _SENTINEL_END = "__RC_EXEC_END__"
    _SENTINEL_EXIT = "__RC_EXEC_EXIT__"

    def _exec_capture(
        self, cluster: str, task_arn: str, container: str,
        command: list[str], region: Optional[str], env: dict, timeout: int,
    ) -> ExecResult:
        import re as _re
        import shlex
        import subprocess

        user_cmd = " ".join(shlex.quote(c) for c in command)
        # `sync; sleep 1` after the user command lets SSM drain its stdout
        # buffer before the session exits — without it, the last several KB
        # of output get swallowed on fast commands. Sentinels let us strip
        # session-manager-plugin chrome and recover the user's real output.
        wrapped_inner = (
            f"echo {self._SENTINEL_BEGIN}; "
            f"({user_cmd}); rc_rc=$?; "
            f"echo {self._SENTINEL_EXIT}=$rc_rc; "
            f"sync; sleep 1; "
            f"echo {self._SENTINEL_END}"
        )
        wrapped = f"sh -c {shlex.quote(wrapped_inner)}"

        aws_cmd = [
            "aws", "ecs", "execute-command",
            "--cluster", cluster,
            "--task", task_arn,
            "--container", container,
            "--interactive",
            "--command", wrapped,
        ]
        if region:
            aws_cmd.extend(["--region", region])

        # SSM session-manager-plugin needs stdin to stay open for the
        # whole session — closing stdin (input=b"" or DEVNULL) causes
        # "Cannot perform start session: EOF" before our wrapper runs on
        # any command that takes more than a beat. Pipe in a never-EOFing
        # stream and let the wrapped command's own exit close the session.
        keepalive = subprocess.Popen(
            ["sh", "-c", "while true; do sleep 60; done"],
            stdout=subprocess.PIPE,
        )
        try:
            proc = subprocess.run(
                aws_cmd, stdin=keepalive.stdout,
                capture_output=True, env=env, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                exit_code=124,
                stdout=(exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=f"exec timed out after {timeout}s",
            )
        finally:
            keepalive.terminate()
            try:
                keepalive.wait(timeout=2)
            except subprocess.TimeoutExpired:
                keepalive.kill()

        raw_out = proc.stdout.decode("utf-8", errors="replace")
        raw_err = proc.stderr.decode("utf-8", errors="replace")

        if self._SENTINEL_BEGIN in raw_out and self._SENTINEL_END in raw_out:
            mid = raw_out.split(self._SENTINEL_BEGIN, 1)[1]
            mid = mid.split(self._SENTINEL_END, 1)[0]
            exit_re = _re.compile(rf"{_re.escape(self._SENTINEL_EXIT)}=(\d+)")
            match = exit_re.search(mid)
            exit_code = int(match.group(1)) if match else 0
            stdout = exit_re.sub("", mid)
            # Strip leading newline from the BEGIN echo and any trailing
            # whitespace from the END echo padding.
            stdout = stdout.strip("\n").rstrip() + "\n" if stdout.strip() else ""
            return ExecResult(exit_code=exit_code, stdout=stdout, stderr=raw_err)

        # No sentinels found — session likely failed before our wrapper
        # ran. Surface what AWS gave us so the user can debug.
        return ExecResult(
            exit_code=proc.returncode or 1,
            stdout=raw_out,
            stderr=raw_err or (
                "exec failed: SSM session ended without our sentinels — "
                "check that ECS Exec is enabled (provider does this on v2 "
                "deploys) and that the task role carries ssmmessages:* perms"
            ),
        )

    def _exec_interactive_tty(
        self, cluster: str, task_arn: str, container: str,
        command: list[str], region: Optional[str], env: dict,
    ) -> ExecResult:
        import os as _os
        import shlex
        user_cmd = " ".join(shlex.quote(c) for c in command)
        aws_cmd = [
            "aws", "ecs", "execute-command",
            "--cluster", cluster,
            "--task", task_arn,
            "--container", container,
            "--interactive",
            "--command", user_cmd,
        ]
        if region:
            aws_cmd.extend(["--region", region])
        # Replace this process so the user gets a true tty session. If
        # execvpe returns at all, it failed.
        _os.execvpe(aws_cmd[0], aws_cmd, env)
        return ExecResult(exit_code=126, stdout="", stderr="execvpe returned")

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
        domained_services: list[dict] | None = None,
        alias_hostnames: list[str] | None = None,
    ) -> Optional[dict]:
        """Resolve custom domain + TLS from rc.yml v2.

        Returns None when no domain is configured, or when there is no public
        service to attach it to (can't HTTPS a worker-only deployment).

        When `domained_services` is non-empty (services declare per-host
        ALB routing), the returned dict's `domain` is the primary cert
        subject (lowest sort order across legacy + per-service domains)
        and `san_domains` lists the rest. `all_domains` is the deduped
        union, used to emit one R53 record per hostname.
        """
        domained_services = domained_services or []
        alias_hostnames = alias_hostnames or []
        legacy_domain = ecs_cfg.get("domain") or (ctx.rc_yml_v2 or {}).get("domain")
        # Collect every distinct hostname that needs a cert + R53 record.
        all_domains: list[str] = sorted({
            *(s["domain"] for s in domained_services),
            *alias_hostnames,
            *([legacy_domain] if legacy_domain else []),
        })
        if not all_domains:
            return None
        if not has_public_service:
            raise ProviderConfigError(
                f"domain {all_domains[0]!r} is set but no service is marked "
                f"public; there is nothing to route traffic to"
            )
        primary = all_domains[0]
        sans = [d for d in all_domains if d != primary]
        # Explicit zone override wins over the 2-label heuristic. Accounts
        # often delegate a subdomain (e.g. api.example.com) without holding
        # the apex, which breaks _zone_from_domain's naive last-two-labels
        # guess on any 3+ label FQDN.
        zone = ecs_cfg.get("route53_zone") or _zone_from_domain(primary)

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
            "domain": primary,
            "zone": zone,
            "tls_mode": tls_mode,
            "certificate_arn": certificate_arn,
            "san_domains": sans,
            "all_domains": all_domains,
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
