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
import os
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
    from botocore.exceptions import ProfileNotFound  # noqa: WPS433

    ecs_cfg = _ecs_cfg(ctx)
    region = ecs_cfg.get("region")
    profile = ecs_cfg.get("aws_profile")
    # A named profile (e.g. `aws_profile: default` in rc.yml) only resolves when
    # a shared AWS config/credentials file is present. In CI or any env-credential
    # context — GitHub OIDC, assumed role, AWS_ACCESS_KEY_ID in the environment —
    # that file is absent and boto3 raises ProfileNotFound. Fall back to the
    # default credential chain (env vars, container/instance role, ...) so
    # `rc deploy` works from a CI runner, not just a developer laptop.
    if profile:
        try:
            return boto3.Session(region_name=region, profile_name=profile)
        except ProfileNotFound:
            pass
    return boto3.Session(region_name=region)


def _ecs_cfg(ctx: DeployContext, *, require: tuple[str, ...] = ()) -> dict[str, Any]:
    """rc-tuc: centralized accessor for ctx.provider_config.ecs.

    Replaces the bespoke '(ctx.provider_config or {}).get("ecs") or {}'
    chain that's repeated ~8 times in this module. When ``require`` is
    given, raises ProviderConfigError with the missing key name — turns
    a downstream TypeError ('NoneType has no attribute ...') into a
    callable, named failure at the point of use.
    """
    cfg = ctx.provider_config
    if cfg is not None and not isinstance(cfg, dict):
        raise ProviderConfigError(
            f"provider_config must be a dict, got {type(cfg).__name__}"
        )
    cfg = cfg or {}
    ecs = cfg.get("ecs")
    if ecs is not None and not isinstance(ecs, dict):
        raise ProviderConfigError(
            f"provider_config.ecs must be a dict, got {type(ecs).__name__}"
        )
    ecs = ecs or {}
    for key in require:
        if not ecs.get(key):
            raise ProviderConfigError(f"provider_config.ecs.{key} is required")
    return ecs


def image_group_owners(services: dict[str, Any]) -> dict[str, str]:
    """Map each build-having service to the OWNER of its image group (rc-44i).

    Services sharing a build identity — resolved context + dockerfile + target +
    build_args — produce an identical image (the Django pattern: django +
    celery-* run the same image, differing only by command). rc builds + pushes
    that image ONCE to the owner's ECR repo and points sibling task defs at it,
    instead of N full pushes to N repos (ECR stores layer blobs per-repo, so N
    repos = N uploads — hours on a slow uplink).

    Owner = the FIRST service of the group in declaration order (the order
    services appear in compose/rc.yml, preserved by the dict). For the Django
    layout that's `django` (declared before its `celery-*` workers), so the
    shared repo is named after the service the image belongs to. Deterministic
    for a given config; general — no per-stack logic. Services without a
    build_context use a pre-built image and are not grouped.
    """
    groups: dict[tuple, list[str]] = {}
    for name, spec in services.items():
        if not getattr(spec, "build_context", None):
            continue
        identity = (
            str(spec.build_context),
            spec.dockerfile or "",
            spec.target or "",
            frozenset((spec.build_args or {}).items()),
        )
        groups.setdefault(identity, []).append(name)
    owners: dict[str, str] = {}
    for members in groups.values():
        owner = members[0]  # first-declared service in the group
        for member in members:
            owners[member] = owner
    return owners


def _services_to_build(services: dict[str, Any], services_filter=None) -> list:
    """The service specs to actually build+push (rc-44i): one OWNER per image
    group, never the siblings (their task def references the owner image). A
    services_filter naming a sibling maps to its owner so the shared image is
    still rebuilt. Order follows sorted service name for determinism."""
    owners = image_group_owners(services)
    members: dict[str, set] = {}
    for name, owner in owners.items():
        members.setdefault(owner, set()).add(name)
    build_owners = [
        spec
        for name, spec in sorted(services.items())
        if spec.build_context and owners.get(name, name) == name
    ]
    if services_filter is None:
        return build_owners
    allowed = set(services_filter)
    return [
        spec for spec in build_owners if members.get(spec.name, {spec.name}) & allowed
    ]


def preflight_existing_vpc(ecs_cfg: dict[str, Any], ec2_client: Any) -> None:
    """Validate an adopted VPC + subnets against live AWS before emitting (rc-a57).

    No-op unless ``vpc_id`` is set. Verifies the VPC exists, every declared
    subnet exists AND belongs to that VPC, and the public subnets span >= 2
    AZs (the ALB requires it). Raises ProviderConfigError with a clear message
    so the failure surfaces as a named rc error rather than a terraform stack
    trace. Uses a vpc-id Filter (not SubnetIds) so a stale id is reported as
    "not in vpc" rather than raising a botocore InvalidSubnetID error.
    """
    vpc_id = ecs_cfg.get("vpc_id")
    if not vpc_id:
        return
    public = list(ecs_cfg.get("public_subnet_ids") or [])
    private = list(ecs_cfg.get("private_subnet_ids") or [])

    vpcs = ec2_client.describe_vpcs(VpcIds=[vpc_id]).get("Vpcs", [])
    if not vpcs:
        raise ProviderConfigError(
            f"provider_config.ecs.vpc_id {vpc_id!r} not found in this account/region"
        )

    in_vpc = {
        s["SubnetId"]: s
        for s in ec2_client.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets", [])
    }
    declared = list(dict.fromkeys(public + private))
    missing = [s for s in declared if s not in in_vpc]
    if missing:
        raise ProviderConfigError(
            f"subnet(s) {missing} not found in vpc {vpc_id} "
            "(check the ids belong to provider_config.ecs.vpc_id)"
        )
    azs = {in_vpc[s]["AvailabilityZone"] for s in public}
    if len(azs) < 2:
        raise ProviderConfigError(
            "provider_config.ecs.public_subnet_ids must span >= 2 availability "
            f"zones for the ALB (got {sorted(azs)})"
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

        ecs_cfg = _ecs_cfg(ctx)
        region = ecs_cfg.get("region")
        if not region:
            raise ProviderConfigError(
                "ECS provider requires provider_config.ecs.region"
            )
        cluster_name = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        vpc_cidr = ecs_cfg.get("vpc_cidr", VPC_CIDR_DEFAULT)
        aws_profile = ecs_cfg.get("aws_profile")

        # Existing-VPC support (rc-a57): a GENERAL, opt-in capability. With
        # vpc_id set, rc deploys INTO an existing VPC instead of creating one —
        # needed where the stack must share a VPC + security group with peer
        # systems (same-VPC SG-referencing + Cloud Map DNS that cross-VPC
        # peering can't replicate). Strictly additive: with no vpc_id the
        # emitted terraform is byte-identical to before (see network.tf.j2 +
        # the rendering-alias context keys below). AWS pre-flight validation of
        # the ids lives in preflight_existing_vpc(); here we only validate the
        # config SHAPE.
        existing_vpc_id = ecs_cfg.get("vpc_id")
        public_subnet_ids = list(ecs_cfg.get("public_subnet_ids") or [])
        private_subnet_ids = list(ecs_cfg.get("private_subnet_ids") or [])
        extra_security_group_ids = list(ecs_cfg.get("security_group_ids") or [])
        if existing_vpc_id and not public_subnet_ids:
            raise ProviderConfigError(
                "provider_config.ecs.vpc_id requires public_subnet_ids "
                "(>= 2 subnets across AZs for the ALB + Fargate tasks)"
            )
        if public_subnet_ids and not existing_vpc_id:
            raise ProviderConfigError(
                "provider_config.ecs.public_subnet_ids requires vpc_id "
                "(subnet ids are only meaningful when adopting an existing VPC)"
            )
        existing_vpc = bool(existing_vpc_id)
        if not private_subnet_ids:
            private_subnet_ids = public_subnet_ids
        # Rendering aliases keep the create path byte-identical: in create mode
        # these are exactly the original resource references; in adopt mode they
        # point at the data source + network locals. Templates read these so they
        # never branch on existing_vpc themselves.
        if existing_vpc:
            vpc_id_ref = "data.aws_vpc.main.id"
            public_subnet_ids_ref = "local.rc_public_subnet_ids"
            public_subnet_idx_ref = "local.rc_public_subnet_ids[count.index]"
            private_subnet_ids_ref = "local.rc_private_subnet_ids"
        else:
            vpc_id_ref = "aws_vpc.main.id"
            public_subnet_ids_ref = "aws_subnet.public[*].id"
            public_subnet_idx_ref = "aws_subnet.public[count.index].id"
            private_subnet_ids_ref = "aws_subnet.private[*].id"

        default_launch_type = ecs_cfg.get("default_launch_type", "FARGATE")
        if default_launch_type not in {"FARGATE", "EC2"}:
            raise ProviderConfigError(
                f"provider_config.ecs.default_launch_type must be FARGATE or EC2, "
                f"got {default_launch_type!r}"
            )

        # Shared-image dedup (rc-44i): which service owns each build group's
        # ECR repo. Computed once; consulted per-service below.
        image_owners = image_group_owners(ctx.services)

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
                efs_volumes.setdefault(
                    vol_name,
                    {
                        "name": vol_name,
                        "tf_name": vol_tf,
                    },
                )
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
            # rc-05q: ALB grace period. Only meaningful for public services
            # (load_balancer block exists). When unset, default 60s for
            # fast-boot services, 180s when any auto_on_deploy lifecycle
            # hook is declared (those run during rollout — migrate alone
            # can take 30-60s on a cold DB).
            if spec.public:
                if spec.health_check_grace_period is not None:
                    effective_grace = spec.health_check_grace_period
                else:
                    has_auto_on_deploy = any(
                        (h or {}).get("auto_on_deploy")
                        for h in (spec.lifecycle or {}).values()
                    )
                    effective_grace = 180 if has_auto_on_deploy else 60
            else:
                effective_grace = None
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
                "health_check_grace_period": effective_grace,
                "launch_type": launch_type,
                "mounts": svc_mounts,
                "stateful": stateful,
                "env": dict(spec.env or {}),
                "command": list(spec.command or []),
                # Pre-built compose image; if set (and no build context), task
                # def uses it verbatim instead of an ECR placeholder.
                "compose_image": spec.image if not spec.build_context else None,
                "has_build_context": bool(spec.build_context),
                # Shared-image dedup (rc-44i). Services sharing a build identity
                # share ONE ECR repo: the owner emits aws_ecr_repository, siblings
                # reference the owner's image. A service that isn't a build-group
                # sibling owns its own repo (image_owners.get -> itself), so
                # single-build / image-only stacks emit exactly as before.
                "owns_image_repo": image_owners.get(name, name) == name,
                "image_repo_tf_name": _tf_name(image_owners.get(name, name)),
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
                # Explicit ALB catch-all selection (see ServiceSpec.default_target).
                "default_target": bool(spec.default_target) if spec.public else False,
            }
            services_view.append(svc_view)
            if launch_type == "EC2":
                ec2_demands.append(
                    EC2TaskDemand(
                        name=name,
                        cpu_units=spec.cpu,
                        memory_mib=spec.memory,
                        replicas=spec.replicas,
                    )
                )
            if spec.public and spec.port and default_public is None:
                default_public = svc_view

        # The loop above iterates services alphabetically, so default_public is
        # the alphabetically-first public+port service — a silent, surprising
        # choice when several services are public (e.g. celery-flower sorts
        # before nginx and would wrongly become the catch-all). When a service
        # explicitly sets default_target=true, honor it: it wins regardless of
        # name order. First flagged service wins if more than one is set.
        default_target_view = next(
            (s for s in services_view if s.get("default_target") and s.get("port")),
            None,
        )
        if default_target_view is not None:
            default_public = default_target_view

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
        project_dir = ctx.compose_path.parent if ctx.compose_path else Path.cwd()
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
                    raise ProviderConfigError(f"secret {sec.name!r}: {exc}") from exc
                if not file_keys:
                    raise ProviderConfigError(
                        f"secret {sec.name!r}: {file_path} has no KEY=value entries"
                    )
                file_secrets.append(
                    {
                        "name": sec.name,
                        "tf_name": tf_sec_name,
                        "path": str(file_path),
                        "keys": file_keys,
                    }
                )
                # One task-def secrets[] entry per KEY in the file, pointing
                # at the same SM secret ARN with a JSON-key selector.
                for key in file_keys:
                    secrets_view.append(
                        {
                            "env_name": key,
                            "value_from_ref": (
                                f'"${{aws_secretsmanager_secret.{tf_sec_name}.arn}}'
                                f':{key}::"'
                            ),
                            # rc-12d: track which rc.yml secret this entry came
                            # from so per-service env_file scoping can filter.
                            "source_secret_name": sec.name,
                        }
                    )
            elif sec.source == "aws_sm":
                if not sec.arn:
                    raise ProviderConfigError(
                        f"secret {sec.name!r}: source=aws_sm requires arn"
                    )
                aws_sm_secrets.append(
                    {
                        "name": sec.name,
                        "arn": sec.arn,
                    }
                )
                # Pre-existing SM secret; we don't know its shape, so the
                # whole value lands in one env var. Users wanting key splits
                # on aws_sm should reference sub-keys via sec.ref (future).
                secrets_view.append(
                    {
                        "env_name": _env_name_for_secret(sec.name),
                        "value_from_ref": f'"{sec.arn}"',
                        # rc-12d: aws_sm secrets are global (rc.yml-declared,
                        # not per-service env_file scoped) — None means
                        # "attach to every service".
                        "source_secret_name": None,
                    }
                )
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
            all_secret_arns.append(
                f"${{aws_secretsmanager_secret.{sec['tf_name']}.arn}}"
            )
        for sec in aws_sm_secrets:
            all_secret_arns.append(sec["arn"])

        # Attach secrets to every service view so each task def gets them.
        # Three filters apply per service:
        #
        #   (rc-z30) plaintext-env override: when a service has a plaintext
        #   env entry for a key that's also sourced from SM, drop the SM
        #   entry from THAT service's secrets[]. ECS rejects task defs
        #   where the same key appears in both environment[] and secrets[].
        #   The plaintext env wins on collision because the user set it
        #   explicitly in rc.yml services.<svc>.env.
        #
        #   (rc-12d) env_file scoping for AUTO-DISCOVERED secrets: a
        #   secret whose name was auto-derived from a compose env_file
        #   directive should ONLY attach to services that ACTUALLY
        #   reference that env_file. Without this, postgres got django-
        #   only keys (REDIS_URL etc.) because every secret entry was
        #   broadcast to every service. Tracked via the union of every
        #   ServiceSpec.env_file_secret_names — names appearing there
        #   are auto-scoped; names NOT in that union are rc.yml-only
        #   declarations that remain global (backward compat for users
        #   whose compose has no env_file directives).
        #
        #   (rc-12d) aws_sm secrets stay global regardless. Marked via
        #   source_secret_name=None.
        scoped_secret_names: set[str] = set()
        for spec in ctx.services.values():
            for n in getattr(spec, "env_file_secret_names", []) or []:
                scoped_secret_names.add(n)

        for svc_view in services_view:
            svc_name = svc_view["name"]
            spec = ctx.services.get(svc_name)
            allowed_env_file_names = set(
                getattr(spec, "env_file_secret_names", []) or []
            )
            override_keys = set(svc_view.get("env") or {})
            scoped_entries: list[dict[str, Any]] = []
            global_entries: list[dict[str, Any]] = []
            for s in secrets_view:
                src_name = s.get("source_secret_name")
                # Global (aws_sm) — always attach.
                if src_name is None:
                    global_entries.append(s)
                    continue
                # File-sourced AND name participates in env_file scoping —
                # only attach if THIS service references it.
                if src_name in scoped_secret_names:
                    if src_name in allowed_env_file_names:
                        scoped_entries.append(s)
                    continue
                # File-sourced but NAME isn't matched to any compose
                # env_file — treat as global (backward compat for
                # rc.yml-only stacks with no compose env_file).
                global_entries.append(s)
            # rc-12d: de-dupe by env_name. ECS rejects task defs with
            # duplicate secret names. Two layers of preference:
            #   (a) within scoped entries: when the same KEY comes from
            #       multiple sources that all attach to this service
            #       (e.g. rc.yml secret with basename-linked scope AND
            #       auto-discovered compose env_file secret), keep the
            #       first occurrence — secrets_view is built rc.yml-first
            #       (R4: rc.yml wins on collision).
            #   (b) scoped wins over global on cross-tier collision.
            seen_in_scoped: set[str] = set()
            scoped_unique: list[dict[str, Any]] = []
            for s in scoped_entries:
                if s["env_name"] in seen_in_scoped:
                    continue
                seen_in_scoped.add(s["env_name"])
                scoped_unique.append(s)
            filtered: list[dict[str, Any]] = list(scoped_unique)
            for s in global_entries:
                if s["env_name"] in seen_in_scoped:
                    continue
                filtered.append(s)
            if override_keys:
                filtered = [s for s in filtered if s["env_name"] not in override_keys]
            svc_view["secrets"] = filtered
        default_target_port = default_public["port"] if default_public else 80
        default_health_check_path = (default_public or {}).get(
            "health_check_path"
        ) or "/"

        ec2_capacity_cfg = (
            self._resolve_ec2_capacity(ecs_cfg, ec2_demands)
            if has_ec2_service
            else None
        )

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
            ctx,
            ecs_cfg,
            has_public_service,
            domained_services,
            alias_hostnames,
        )

        environment = "rc-test" if ctx.project.startswith("rc-test-") else None

        context: dict[str, Any] = {
            "project": ctx.project,
            "region": region,
            "vpc_cidr": vpc_cidr,
            # Existing-VPC support (rc-a57). existing_vpc gates network.tf.j2
            # between create (default) and adopt; the *_ref aliases let the
            # other templates stay agnostic.
            "existing_vpc": existing_vpc,
            "existing_vpc_id": existing_vpc_id,
            "public_subnet_ids": public_subnet_ids,
            "private_subnet_ids": private_subnet_ids,
            "extra_security_group_ids": extra_security_group_ids,
            "vpc_id_ref": vpc_id_ref,
            "public_subnet_ids_ref": public_subnet_ids_ref,
            "public_subnet_idx_ref": public_subnet_idx_ref,
            "private_subnet_ids_ref": private_subnet_ids_ref,
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
            "backend_block": render_backend_block(
                ctx.tf_backend_config or {"type": "local"}
            ),
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

    def preflight(self, ctx: DeployContext) -> None:
        """AWS pre-flight for adopted-VPC configs (rc-a57). No-op unless
        provider_config.ecs.vpc_id is set; otherwise verifies the VPC + subnets
        against live AWS so a bad id fails as a clear rc error, not a terraform
        stack trace."""
        ecs_cfg = _ecs_cfg(ctx)
        if not ecs_cfg.get("vpc_id"):
            return
        session = self.session_factory(ctx)
        ec2 = session.client("ec2", region_name=ecs_cfg.get("region"))
        preflight_existing_vpc(ecs_cfg, ec2)

    def plan(self, ctx: DeployContext) -> PlanResult:
        self.preflight(ctx)
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

        self.preflight(ctx)

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
        self._reconcile_orphan_backup_bucket(ctx, runner)
        runner.apply()
        outputs = runner.output()

        warnings: list[str] = []
        # rc-44z: --no-build skips _build_and_push_images entirely. Force-
        # roll still rolls services so they pick up any task-def changes
        # terraform just applied (e.g. new env var, new secret reference,
        # bumped grace period, etc.).
        if getattr(ctx, "skip_build", False):
            self._emit(
                "  rc-44z: --no-build set — skipping image build+push. "
                "Rolling existing :latest images on all services."
            )
            roll_targets = (
                sorted(services_filter)
                if services_filter
                else sorted(ctx.services.keys())
            )
            if not getattr(ctx, "skip_force_roll", False):
                self._force_new_deployments(ctx, roll_targets)
            return DeployResult(
                revision_id=_revision_id_from_dir(out_dir),
                services=roll_targets,
                duration_s=time.monotonic() - start,
                terraform_outputs=outputs,
                warnings=warnings,
            )
        pushed = self._build_and_push_images(
            ctx,
            outputs,
            warnings,
            services_filter=services_filter,
            requested_tag=tag,
        )
        if pushed and not getattr(ctx, "skip_force_roll", False):
            # ECS won't pull a new :latest automatically — force it.
            # rc-1bk: rc up sets skip_force_roll=True so the rollout happens
            # AFTER `rc secrets push` populates SM, avoiding the cold-start
            # CannotPullSecrets cascade.
            self._force_new_deployments(ctx, pushed)

        return DeployResult(
            revision_id=_revision_id_from_dir(out_dir),
            services=(
                sorted(services_filter)
                if services_filter
                else sorted(ctx.services.keys())
            ),
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

        # rc-5h8.12: --no-build in no-state mode. Images are built+pushed out
        # of band (e.g. CodeBuild / CI), so rc builds nothing — but it must
        # still force-roll so ECS pulls the freshly-pushed :latest. The normal
        # path below only rolls services it pushed, so with --no-build (or a
        # stack whose services declare no build context) `pushed` is empty and
        # nothing rolls — a silent no-op that reports success. Short-circuit:
        # force-roll the requested services (or all) and return. Skips ECR repo
        # discovery entirely, so --no-build deploys don't even need ECR perms.
        if getattr(ctx, "skip_build", False):
            roll_targets = (
                sorted(services_filter)
                if services_filter
                else sorted(ctx.services.keys())
            )
            self._emit(
                "  no-state + --no-build: force-rolling "
                f"{len(roll_targets)} service(s) onto existing :latest "
                f"({', '.join(roll_targets)})."
            )
            if not getattr(ctx, "skip_force_roll", False):
                self._force_new_deployments(ctx, roll_targets)
            return DeployResult(
                revision_id=f"{ctx.project}-no-state-{int(start)}",
                services=roll_targets,
                duration_s=time.monotonic() - start,
                terraform_outputs={},
                warnings=warnings,
            )

        # Synthesize a terraform-outputs-shaped dict so _build_and_push_images
        # can be reused unchanged. Keys: repo URL keyed by service name.
        ecs_cfg = _ecs_cfg(ctx)
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
            ctx,
            synthetic_outputs,
            warnings,
            services_filter=services_filter,
            requested_tag=tag,
        )
        if pushed:
            self._force_new_deployments(ctx, pushed)

        return DeployResult(
            revision_id=f"{ctx.project}-no-state-{int(start)}",
            services=(
                sorted(services_filter)
                if services_filter
                else sorted(ctx.services.keys())
            ),
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
        to_build = _services_to_build(ctx.services, services_filter)
        # rc-2v8 (extended): check build-context sizes BEFORE running
        # docker build. Without this, a 6GB+ context goes straight to
        # buildkit and the user only learns they should have written a
        # .dockerignore after 15-30 min of "transferring context: 5.6GB".
        # >5GB hard-errors unless RC_FORCE_LARGE_CONTEXT=1; >1GB warns.
        if to_build:
            self._preflight_build_context_sizes(to_build)
        if not to_build:
            # rc-8q4 + rc-3kr: don't silently return — emit per-service
            # diagnostic so the user can see WHY each service was
            # excluded. start-simpli session reported "Deploy complete"
            # in 39s with zero builds, even though compose had build:
            # blocks; without a per-service breakdown the user couldn't
            # see whether build_context was None for every service or
            # whether a filter excluded everything.
            buildable = sum(1 for s in ctx.services.values() if s.build_context)
            if buildable == 0:
                self._emit(
                    "  No images to build (no services declare build "
                    "context). Per-service breakdown:"
                )
                for name, spec in sorted(ctx.services.items()):
                    img = spec.image or "<no image>"
                    self._emit(
                        f"    {name}: build_context=None image={img!r} "
                        f"(no compose build: stanza found, or rc.yml "
                        f"references compose_file with no build:)"
                    )
                self._emit(
                    "  If this is unexpected: check rc.yml.compose_file "
                    "points at a compose YAML that has services.<svc>."
                    "build:{context,dockerfile} for the service(s) you "
                    "want rebuilt. See rc-3kr."
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
        buildcache_repo = (outputs.get("buildcache_repository") or {}).get("value")
        # Adopted / `--no-state` stacks build+push without ever running
        # terraform, so there's no `buildcache_repository` output to read and
        # every deploy rebuilds the (heavy pip/apt) layers cold. Let CI or an
        # operator point at a pre-created cache repo via RC_BUILDCACHE_REPO so
        # those deploys get layer caching too. Terraform output wins when both
        # are set; RC_DISABLE_BUILDCACHE (handled in ImageBuilder) still opts
        # out entirely.
        if not buildcache_repo:
            buildcache_repo = os.environ.get("RC_BUILDCACHE_REPO") or None

        from ...image import ImageBuildSpec, ImageBuilder, ImagePusher
        from ...no_cache_state import consume_no_cache
        from .ecr_auth import ECRAuthenticator

        # rc-2kp: an `rc fix *` subcommand (bake-bind-mount-source,
        # django-tls, nginx-conf) drops a sentinel when it edits files
        # in the project. Consume it so the next build forces --no-cache
        # for every service — buildx's registry layer cache otherwise
        # sometimes returns stale layers that don't reflect the edit.
        no_cache_pending = consume_no_cache(ctx.working_dir)
        if no_cache_pending:
            self._emit(
                "  rc-2kp: no-cache sentinel found (an `rc fix *` "
                "subcommand modified files since the last build) — "
                "this build runs with --no-cache."
            )
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
        skip_when_tag_exists = requested_tag is not None and requested_tag != "latest"

        pushed: list[str] = []
        for spec in to_build:
            repo_url = repos.get(spec.name)
            if not repo_url:
                msg = f"service {spec.name!r}: no ECR repo in terraform outputs"
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
                    ecr_client,
                    repo_name,
                    requested_tag,
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
                no_cache=no_cache_pending,
            )
            tags = builder.build(build)
            pusher.push(tags)
            pushed.append(spec.name)
        return pushed

    @staticmethod
    def _ecr_image_manifest(
        ecr_client: Any,
        repo_name: str,
        tag: str,
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
        ecr_client: Any,
        repo_name: str,
        manifest: str,
        new_tag: str,
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

    def _preflight_build_context_sizes(self, to_build: list) -> None:
        """rc-2v8: refuse to start docker build when the context is huge.

        Sentinal repro: backend/ was 6.8GB (5.8GB Django uploaded media in
        backend/backend/media). The first build hung 25+ min uploading
        context to buildkit. This pre-flight uses the same size walker as
        the plan-time warning to catch the problem BEFORE buildkit gets
        the context. Errors (not warns) when the user is about to ship a
        multi-GB image — escape hatch via RC_FORCE_LARGE_CONTEXT=1.

        Honors a soft threshold (1GB → warn) and a hard threshold
        (5GB → error) tunable via env:
          RC_BUILD_CONTEXT_WARN_GB (default 1)
          RC_BUILD_CONTEXT_BLOCK_GB (default 5)
        """
        import os as _os

        try:
            warn_gb = float(_os.environ.get("RC_BUILD_CONTEXT_WARN_GB", "1"))
            block_gb = float(_os.environ.get("RC_BUILD_CONTEXT_BLOCK_GB", "5"))
        except ValueError:
            warn_gb, block_gb = 1.0, 5.0
        warn_b = int(warn_gb * 1024 * 1024 * 1024)
        block_b = int(block_gb * 1024 * 1024 * 1024)
        force = _os.environ.get("RC_FORCE_LARGE_CONTEXT", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        from ...compose_warnings import _build_context_size, _human_bytes

        seen: set[str] = set()
        block_msgs: list[str] = []
        for spec in to_build:
            if not spec.build_context:
                continue
            ctx_path = Path(spec.build_context)
            key = str(ctx_path.resolve()) if ctx_path.exists() else str(ctx_path)
            if key in seen:
                continue
            seen.add(key)
            total, top_dirs = _build_context_size(ctx_path)
            if total < warn_b:
                continue
            top_summary = ", ".join(
                f"{name} ({_human_bytes(sz)})" for name, sz in top_dirs[:3]
            )
            line = (
                f"build context for {spec.name!r} ({ctx_path}) is "
                f"{_human_bytes(total)}. Heaviest: {top_summary}. "
                f"Add to .dockerignore."
            )
            if total >= block_b and not force:
                block_msgs.append(line)
            else:
                self._emit(f"  WARN: {line}")
        if block_msgs:
            joined = "\n    ".join(block_msgs)
            raise ProviderConfigError(
                f"rc-2v8: refusing to docker build with multi-GB context(s) — "
                f"the upload to buildkit can hang for 25+ min on slow uplinks. "
                f"Slim the context via .dockerignore (or set "
                f"RC_FORCE_LARGE_CONTEXT=1 to bypass).\n    {joined}"
            )

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
        self,
        out_dir: Path,
        ctx: DeployContext,
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
        self,
        ctx: DeployContext,
        runner: TerraformRunner,
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
        ecs_cfg = _ecs_cfg(ctx)
        cluster_name = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        log_group_name = f"/aws/ecs/containerinsights/{cluster_name}/performance"

        try:
            session = self.session_factory(ctx)
            logs = session.client("logs")
            resp = logs.describe_log_groups(logGroupNamePrefix=log_group_name)
            groups = resp.get("logGroups", [])
            existing = [
                g
                for g in groups
                if isinstance(g, dict) and g.get("logGroupName") == log_group_name
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
                f"imported orphan log group {log_group_name} into " f"terraform state"
            )
            return
        except TerraformError as exc:
            # rc-e5u.37.5: terraform's actual message is "is already
            # managing a remote object" (verb form), not "already
            # managed". The old substring missed this so subsequent
            # deploys hit ResourceAlreadyExistsException on apply even
            # though the resource WAS in state. Match the verb form
            # plus the older "already exists in state" tf <0.12 wording.
            msg = ((exc.stderr or "") + (exc.stdout or "")).lower()
            already_managed_signals = (
                "already managed",
                "is already managing",
                "already exists in state",
            )
            if any(s in msg for s in already_managed_signals):
                # rc-b0d: terraform's raw 'Error: Resource already managed'
                # already hit the user's terminal via subprocess streaming.
                # Emit a follow-up so they know rc handled it cleanly.
                self._emit(
                    f"  ✓ orphan log group {log_group_name} already in "
                    f"terraform state — prior 'Error: Resource already "
                    f"managed' is informational; deploy continues."
                )
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

    def _reconcile_orphan_backup_bucket(
        self,
        ctx: DeployContext,
        runner: TerraformRunner,
    ) -> None:
        """rc-nae: import an AWS-side S3 bucket whose name matches
        backup.bucket but isn't in terraform state.

        Pattern mirrors _reconcile_orphan_log_groups. When a user adds
        backup.bucket to rc.yml AFTER the deploy is up (or migrates a
        manually-created bucket under terraform), the next apply
        otherwise dies with BucketAlreadyOwnedByYou. We probe via boto3
        head_bucket to confirm we own it, then 'terraform import'.

        Distinct from log-group case: S3 deletion is destructive (data
        loss potential), so there is NO delete-then-recreate fallback.
        If import fails, we surface a clear error and let the apply
        crash naturally — better than silently nuking dump data.
        """
        backup_cfg = (ctx.rc_yml_v2 or {}).get("backup") or {}
        bucket_name = backup_cfg.get("bucket")
        managed = bool(backup_cfg.get("bucket_managed", True))
        if not bucket_name or not managed:
            return

        try:
            session = self.session_factory(ctx)
            s3 = session.client("s3")
            # head_bucket returns 200 when the bucket exists AND we own
            # it (or have permission); 404 when it doesn't exist; 403
            # when someone else owns it.
            s3.head_bucket(Bucket=bucket_name)
        except Exception as exc:  # noqa: BLE001
            err_repr = repr(exc)
            if (
                "Not Found" in err_repr
                or "404" in err_repr
                or "NoSuchBucket" in err_repr
            ):
                # No orphan — terraform will create it from scratch. Normal.
                return
            self._emit(
                f"warning: orphan backup-bucket reconcile skipped — "
                f"could not query AWS for s3://{bucket_name}: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        try:
            runner.import_resource(
                "aws_s3_bucket.backups",
                bucket_name,
            )
            self._emit(
                f"imported orphan backup bucket s3://{bucket_name} "
                f"into terraform state"
            )
        except TerraformError as exc:
            msg = ((exc.stderr or "") + (exc.stdout or "")).lower()
            already_managed_signals = (
                "already managed",
                "is already managing",
                "already exists in state",
            )
            if any(s in msg for s in already_managed_signals):
                # rc-b0d: same as the log-group case — surface a follow-up
                # so the prior raw 'Error: Resource already managed' isn't
                # alarming.
                self._emit(
                    f"  ✓ backup bucket s3://{bucket_name} already in "
                    f"terraform state — prior 'Error: Resource already "
                    f"managed' is informational; deploy continues."
                )
                return  # already imported on a prior deploy
            # No safe fallback — refusing to delete an S3 bucket that
            # may contain dump data. Surface a clear next-step.
            self._emit(
                f"warning: backup-bucket import failed "
                f"({exc.__class__.__name__}). The upcoming apply will "
                f"likely fail with BucketAlreadyOwnedByYou. Manually "
                f"import: terraform -chdir={self._tf_dir(ctx)} import "
                f"aws_s3_bucket.backups {bucket_name}"
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
        ecs_cfg = _ecs_cfg(ctx)
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
        rolled_names: list[str] = []
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
                    rolled_names.append(name)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
            if last_err is not None:
                raise last_err

        # rc-8zz: post-rollout watcher. Poll service events for 60s looking
        # for IAM/secret/ECR resolution failures. ECS retries failed task
        # placements every ~30s, so the first error usually shows up
        # within 30-60s of force-roll. Without this, the user only learns
        # about ResourceInitializationError when they manually inspect
        # service events — sometimes hours later.
        self._watch_post_rollout_errors(client, cluster, rolled_names)

    def _watch_post_rollout_errors(
        self,
        client: Any,
        cluster: str,
        services: list[str],
    ) -> None:
        """rc-8zz: poll ECS service events for ~60s after force-roll;
        surface IAM/secret/ECR placement errors clearly + early.

        Looks for the most common deploy-time gotchas:
          - ResourceInitializationError (SM perms missing)
          - unable to retrieve secret from asm
          - CannotPullContainerError (ECR perms missing or image absent)

        Honors RC_POST_ROLLOUT_WATCH_S env var (default 60) so tests +
        operators can tune.
        """
        import os as _os

        budget = int(_os.environ.get("RC_POST_ROLLOUT_WATCH_S", "60"))
        if budget <= 0 or not services:
            return
        deadline = time.monotonic() + budget
        seen_event_ids: set[str] = set()
        # Capture pre-roll event IDs so we only flag NEW events.
        try:
            pre = client.describe_services(cluster=cluster, services=services)
            for svc in pre.get("services") or []:
                for ev in svc.get("events") or []:
                    eid = ev.get("id")
                    if eid:
                        seen_event_ids.add(eid)
        except Exception as exc:  # noqa: BLE001
            # rc-x19: silently disabling the watcher on transient AWS
            # errors hides whether the user got post-rollout diagnostics
            # or not. Emit a warning so they know.
            self._emit(
                f"  WARN: post-rollout watcher disabled — could not "
                f"baseline service events ({exc!s})."
            )
            return
        problem_patterns = (
            "ResourceInitializationError",
            "unable to retrieve secret",
            "is not authorized to perform: secretsmanager",
            "CannotPullContainerError",
            "is not authorized to perform: ecr",
            "Repository does not exist",
        )
        # rc-8vb: flap signal — service is killing tasks because ALB health
        # checks fail repeatedly. 3+ unhealthy events in the watch window
        # = high confidence flap (not just a normal rolling drain).
        flap_patterns = (
            "Health checks failed",
            "is unhealthy in (target-group",
        )
        flagged: dict[str, str] = {}
        flap_counts: dict[str, int] = {}
        flap_sample: dict[str, str] = {}
        while time.monotonic() < deadline:
            try:
                desc = client.describe_services(
                    cluster=cluster,
                    services=services,
                )
            except Exception as exc:  # noqa: BLE001
                # rc-x19: same as above — warn rather than silently abort
                # the watch loop mid-budget.
                self._emit(
                    f"  WARN: post-rollout watcher aborted — "
                    f"describe_services failed mid-poll ({exc!s})."
                )
                return
            for svc in desc.get("services") or []:
                svc_name = svc.get("serviceName") or "?"
                for ev in svc.get("events") or []:
                    eid = ev.get("id")
                    if not eid or eid in seen_event_ids:
                        continue
                    seen_event_ids.add(eid)
                    msg = ev.get("message") or ""
                    if any(p in msg for p in problem_patterns):
                        flagged.setdefault(svc_name, msg)
                    if any(p in msg for p in flap_patterns):
                        flap_counts[svc_name] = flap_counts.get(svc_name, 0) + 1
                        flap_sample.setdefault(svc_name, msg)
            if flagged:
                break
            # Check deadline BEFORE sleeping so a tight budget exits
            # quickly. Sleep for the smaller of (5s, remaining budget)
            # so we don't oversleep past the deadline.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(5.0, remaining))
        if flagged:
            self._emit(
                f"\n  WARN: post-rollout placement errors detected on "
                f"{len(flagged)} service(s):"
            )
            for svc_name, msg in flagged.items():
                self._emit(f"    {svc_name}: {msg.strip()[:300]}")
            self._emit(
                "  Common causes: (1) ecsTaskExecutionRole missing "
                "secretsmanager:GetSecretValue on new auto-discovered "
                "SM secrets — attach an inline policy granting it on "
                "<project>/* ARNs; (2) ECR perms missing on the role; "
                "(3) the image pushed isn't tagged the way the task "
                "def expects. Old tasks may still serve traffic during "
                "this window."
            )
        # rc-8vb: separate diagnostic for flap loops. A service with 3+
        # unhealthy events in the watch window is being killed by ALB
        # before it can serve — almost always grace period too short
        # OR the app is crashing on startup.
        flapping = {n: c for n, c in flap_counts.items() if c >= 3}
        if flapping:
            self._emit(
                f"\n  WARN: post-rollout flap loop detected on "
                f"{len(flapping)} service(s) — tasks are being killed "
                f"before they can serve traffic:"
            )
            for svc_name, count in flapping.items():
                sample = flap_sample.get(svc_name, "").strip()[:200]
                self._emit(
                    f"    {svc_name}: {count} unhealthy events. " f"Last: {sample}"
                )
            self._emit(
                "  Likely causes: (1) health_check_grace_period too short "
                "for the boot time — bump it via "
                "services.<svc>.health_check_grace_period in rc.yml "
                "(180s for Django/Rails with migrate; 60s baseline); "
                "(2) the app is crashing on startup — `aws logs tail "
                "/ecs/<project> --since 5m --log-stream-name-prefix "
                "<svc>` should show ImportError/OperationalError/etc.; "
                "(3) wrong containerPort (compose port doesn't match "
                "what the app actually binds)."
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
        ecs_cfg = _ecs_cfg(ctx)
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
        ecs_cfg = _ecs_cfg(ctx)
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
                statuses.append(
                    ServiceStatus(
                        name=name,
                        desired=0,
                        running=0,
                        health="unknown",
                        last_event="service not found",
                    )
                )
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
            statuses.append(
                ServiceStatus(
                    name=name,
                    desired=desired,
                    running=running,
                    health=health,
                    last_event=last_event,
                    running_revision=running_rev,
                    latest_revision=latest_rev,
                )
            )

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
            except Exception as exc:  # noqa: BLE001
                # rc-x19: don't silently swallow. Status report still
                # works without ingress_url; just tell the caller why.
                self._emit(
                    f"  WARN: could not read ALB DNS from terraform "
                    f"outputs ({exc!s}). Status report omits ingress_url."
                )
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

        ecs_cfg = _ecs_cfg(ctx)
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
        # rc-uct: heartbeat the silent 5-min wait so users can tell exec
        # progress vs stuck. The waiter polls every wait_interval (5s);
        # heartbeat fires every RC_HEARTBEAT_INTERVAL_S (default 30s).
        # Skipped when wait_budget=0 (test paths).
        from ...heartbeat import heartbeat as _hb

        _hb_ctx = (
            _hb(
                self.progress,
                f"waiting for service {service!r} to be exec-ready",
            )
            if wait_budget > 0
            else None
        )
        if _hb_ctx is not None:
            _hb_ctx.__enter__()
        # rc-0ev: also fetch the service's CURRENT taskDefinitionArn so
        # we can prefer tasks running under the latest revision. Without
        # this, during a force-roll we can land on a draining old task
        # whose enableExecuteCommand=false even though its agent is
        # technically RUNNING. Best-effort: silent fall-through if the
        # describe_services call fails (test mocks, transient AWS).
        current_td_arn: Optional[str] = None
        try:
            svc_desc = ecs_client.describe_services(
                cluster=cluster,
                services=[service],
            )
            services_list = svc_desc.get("services") or []
            if services_list:
                current_td_arn = services_list[0].get("taskDefinition")
        except Exception:  # noqa: BLE001
            current_td_arn = None
        while True:
            tasks = (
                ecs_client.list_tasks(
                    cluster=cluster, serviceName=service, desiredStatus="RUNNING"
                ).get("taskArns")
                or []
            )
            if tasks:
                # Prefer a task whose ExecuteCommandAgent is RUNNING AND
                # whose enableExecuteCommand is True AND whose
                # taskDefinitionArn matches the service's current
                # revision (rc-0ev). Fall through to the first
                # list_tasks ARN if describe_tasks is unhelpful (mocked
                # test, network blip, missing perms, very fresh task
                # that hasn't reported agents yet) — pre-46.6 behavior.
                exec_blocked_tasks: set[str] = set()
                preferred: Optional[str] = None
                preferred_old_revision: Optional[str] = None
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
                    # rc-0ev: skip tasks whose execute-command was disabled
                    # at run-time. AWS returns this field literally as
                    # `enableExecuteCommand` on the task. The old task in a
                    # force-roll may have been launched before exec was
                    # enabled on the task def → exec attempt would fail
                    # with 'execute command was not enabled when the task
                    # was run'. Default to True when the field is absent so
                    # older mocks / older AWS API responses don't get
                    # spuriously rejected.
                    if t.get("enableExecuteCommand") is False:
                        exec_blocked_tasks.add(t.get("taskArn") or "")
                        continue
                    agents_ready = True
                    seen_exec_agent = False
                    containers = t.get("containers") or []
                    if not isinstance(containers, list):
                        containers = []
                    for c in containers:
                        if not isinstance(c, dict):
                            continue
                        for ag in c.get("managedAgents") or []:
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
                    # rc-0ev: prefer matching-revision tasks; remember
                    # the old-revision exec-ready task as a fallback.
                    arn = t.get("taskArn")
                    if current_td_arn and t.get("taskDefinitionArn") == current_td_arn:
                        preferred = arn
                        break
                    if preferred_old_revision is None:
                        preferred_old_revision = arn
                if preferred:
                    task_arn = preferred
                    break
                if preferred_old_revision:
                    # All exec-ready tasks are on an older task def — use
                    # one anyway, since waiting forever for the new
                    # revision can stall lifecycle hooks if the new task
                    # is taking a while to register its agent.
                    task_arn = preferred_old_revision
                    break
                # describe_tasks didn't give us an agent-ready candidate.
                # If NO task we saw was explicitly blocked, fall through
                # to the first task ARN from list_tasks (pre-46.6 behavior).
                fallback = next(
                    (a for a in tasks if a not in exec_blocked_tasks),
                    None,
                )
                if fallback:
                    task_arn = fallback
                    break
                last_diag = (
                    f"{len(tasks)} task(s) running but none have "
                    f"enableExecuteCommand + ExecuteCommandAgent ready "
                    f"(check `rc status` if the new revision is healthy)"
                )
            if time.monotonic() > deadline:
                if _hb_ctx is not None:
                    _hb_ctx.__exit__(None, None, None)
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

        # Loop exited via `break` (got a task_arn) — close heartbeat.
        if _hb_ctx is not None:
            _hb_ctx.__exit__(None, None, None)

        env = _os.environ.copy()
        if profile:
            env["AWS_PROFILE"] = profile

        # rc-uct: heartbeat the actual aws ecs execute-command call too.
        # SSM session-manager-plugin can hang on a flaky network or
        # broken in-container SSM agent; without this the user sees
        # nothing until the 10-min timeout fires.
        from ...heartbeat import heartbeat as _hb2

        if interactive:
            with _hb2(
                self.progress,
                f"executing in {service!r} (interactive)",
            ):
                return self._exec_interactive_tty(
                    cluster,
                    task_arn,
                    service,
                    command,
                    region,
                    env,
                )
        with _hb2(self.progress, f"executing in {service!r}"):
            return self._exec_capture(
                cluster,
                task_arn,
                service,
                command,
                region,
                env,
                timeout,
            )

    _SENTINEL_BEGIN = "__RC_EXEC_BEGIN__"
    _SENTINEL_END = "__RC_EXEC_END__"
    _SENTINEL_EXIT = "__RC_EXEC_EXIT__"

    def _exec_capture(
        self,
        cluster: str,
        task_arn: str,
        container: str,
        command: list[str],
        region: Optional[str],
        env: dict,
        timeout: int,
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
            "aws",
            "ecs",
            "execute-command",
            "--cluster",
            cluster,
            "--task",
            task_arn,
            "--container",
            container,
            "--interactive",
            "--command",
            wrapped,
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
                aws_cmd,
                stdin=keepalive.stdout,
                capture_output=True,
                env=env,
                timeout=timeout,
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
            stderr=raw_err
            or (
                "exec failed: SSM session ended without our sentinels — "
                "check that ECS Exec is enabled (provider does this on v2 "
                "deploys) and that the task role carries ssmmessages:* perms"
            ),
        )

    def _exec_interactive_tty(
        self,
        cluster: str,
        task_arn: str,
        container: str,
        command: list[str],
        region: Optional[str],
        env: dict,
    ) -> ExecResult:
        import os as _os
        import shlex

        user_cmd = " ".join(shlex.quote(c) for c in command)
        aws_cmd = [
            "aws",
            "ecs",
            "execute-command",
            "--cluster",
            cluster,
            "--task",
            task_arn,
            "--container",
            container,
            "--interactive",
            "--command",
            user_cmd,
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
        all_domains: list[str] = sorted(
            {
                *(s["domain"] for s in domained_services),
                *alias_hostnames,
                *([legacy_domain] if legacy_domain else []),
            }
        )
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
            raise ProviderConfigError("tls.mode=manual requires tls.certificate_arn")
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
_IGNORED_TOP_LEVEL = {
    ".terraform",
    ".terraform.lock.hcl",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}


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
