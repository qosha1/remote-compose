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
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Optional

from ...config.v2_schema import (
    ConfigError,
    resolve_task_groups,
    validate_task_groups,
)
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
from .autosize import (
    DEPLOYMENT_MAX_PERCENT_DEFAULT,
    TRUNKING_DISABLED,
    TRUNKING_ENABLED,
    TRUNKING_UNKNOWN,
    EC2TaskDemand,
    KNOWN_INSTANCE_SHAPES,
    auto_size,
    check_fixed_shape_capacity,
    measure_fleet,
)
from .iam_plan import IamPlan, build_iam_plan
from .deploy_preflight import FAIL as _PREFLIGHT_FAIL
from .deploy_preflight import run_preflight
from .plan_analysis import (
    detect_binpack_az_rebalancing_conflicts,
    detect_task_definition_replacements,
    render_binpack_conflict_warning,
    render_replacement_warning,
)
from .network_plan import (
    NetworkPlan,
    build_network_plan,
    check_endpoint_reachability,
    check_reserved_names,
    tf_ident,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


RunnerFactory = Callable[[Path], TerraformRunner]
SessionFactory = Callable[[DeployContext], Any]


def _parse_declared_network(ctx: DeployContext) -> tuple[Any, dict[str, Any]]:
    """Re-read the ``network:`` / ``repositories:`` blocks off the raw rc.yml.

    ``DeployContext`` carries the raw document rather than the parsed
    ``RcConfigV2``, so the provider re-parses these two blocks rather than
    threading a new field through every context construction site. The parse
    is pure and cheap, and ``parse()`` has already validated the document once
    by the time we get here — this cannot surface an error the user has not
    already seen.
    """
    from ...config._schema_parser import _parse_network, _parse_repositories

    raw = ctx.rc_yml_v2 or {}
    network = _parse_network(raw.get("network"))
    repositories = _parse_repositories(raw.get("repositories"))
    network.validate()
    for repo in repositories.values():
        repo.validate()
    return network, repositories


def _parse_declared_iam_roles(ctx: DeployContext) -> dict[str, Any]:
    """Re-read the ``iam_roles:`` block off the raw rc.yml.

    Same rationale as :func:`_parse_declared_network`: ``DeployContext``
    carries the raw document, and re-parsing one pure block is cheaper than
    threading a new field through every context construction site.
    """
    from ...config._schema_parser import _parse_iam_roles

    roles = _parse_iam_roles((ctx.rc_yml_v2 or {}).get("iam_roles"))
    for role in roles.values():
        role.validate()
    return roles


def _service_role_view(spec: Any, *, iam_plan: IamPlan) -> dict[str, Any]:
    """Resolve which task role this service's task definition points at.

    Defaults to ``aws_iam_role.task`` — the shared role every task definition
    has referenced since rc emitted its first ECS stack. Naming an
    ``iam_role:`` swaps the reference for that service only; nothing about the
    shared role's own emission changes, so a stack where nobody opts in is
    byte-identical to one that predates this feature.
    """
    role_name = getattr(spec, "iam_role", None)
    if not role_name:
        return {
            "task_role_ref": "aws_iam_role.task.arn",
            "declared_iam_role": None,
        }
    return {
        "task_role_ref": iam_plan.role_arn_ref(role_name),
        "declared_iam_role": role_name,
    }


def _resolve_subnet_group_placement(
    subnet_group_name: Optional[str],
    *,
    net_plan: NetworkPlan,
    default_subnets_ref: str,
    default_assign_public_ip: bool,
    where: str,
) -> dict[str, Any]:
    """Resolve ``subnets_ref`` / ``assign_public_ip`` for a named subnet group.

    Shared by ``_service_placement_view`` (a service's own ``subnet_group:``)
    and the EC2 capacity ASG's ``ec2_capacity.subnet_group`` (rc-e5u.25.6) —
    both need IDENTICAL semantics, so this is the one place that resolves a
    declared name into placement, rather than two copies that could drift.

    ``assign_public_ip`` is derived, never declared: it follows the placement
    subnet's routing (``group.public``). A private subnet with a public IP is
    a broken configuration AWS will happily accept and then silently fail to
    route, so there is no switch here to get it wrong with.

    With no ``subnet_group_name``, falls back to the caller-resolved
    environment default — ``provider_config.ecs.default_subnet_placement``
    (rc-0cv), "public" unless overridden.

    An unknown name raises ``ProviderConfigError`` rather than letting
    ``net_plan.subnet_group()``'s bare ``KeyError`` escape — ``where`` names
    the offending knob (``"service 'web': subnet_group"`` vs
    ``"provider_config.ecs.ec2_capacity.subnet_group"``) so the message
    points at whichever caller triggered it. In the normal ``emit_terraform``
    flow this branch is unreachable for both callers: the unknown-group case
    is already caught earlier — services by ``validate_network_refs``,
    ec2_capacity by the parallel check next to it — before ``net_plan`` is
    even built. It stays here as a safety net for any caller that resolves
    placement without going through that front door first.
    """
    if not subnet_group_name:
        return {
            "subnets_ref": default_subnets_ref,
            "assign_public_ip": default_assign_public_ip,
        }
    try:
        group = net_plan.subnet_group(subnet_group_name)
    except KeyError:
        known = sorted(g.name for g in net_plan.subnet_groups)
        raise ProviderConfigError(
            f"{where}: {subnet_group_name!r} does not name a declared "
            f"network.subnets group (known: {known or 'none'})"
        ) from None
    return {
        "subnets_ref": f"aws_subnet.{group.tf_name}[*].id",
        "assign_public_ip": group.public,
    }


def _service_placement_view(
    spec: Any,
    *,
    net_plan: NetworkPlan,
    default_subnets_ref: str,
    extra_security_group_ids: list[str],
    default_assign_public_ip: bool = True,
) -> dict[str, Any]:
    """Resolve a service's subnets / security groups / public-IP for the task ENI.

    Both declared knobs REPLACE rather than append — naming a security group
    and still being joined to the shared ``tasks`` group would defeat the
    point, since that group carries ALB ingress and blanket egress.

    Subnet placement itself is resolved by ``_resolve_subnet_group_placement``
    — see its docstring for why ``assign_public_ip`` is derived rather than
    declared.
    """
    declared_sgs = list(getattr(spec, "security_groups", None) or [])
    subnet_group_name = getattr(spec, "subnet_group", None)

    if declared_sgs:
        refs = [f"aws_security_group.{net_plan.sg_tf_name(n)}.id" for n in declared_sgs]
        # provider_config.ecs.security_group_ids is an environment-wide
        # adopt-an-existing-SG knob; a service that names its own groups has
        # opted out of environment-wide defaults entirely.
        security_groups_ref = "[" + ", ".join(refs) + "]"
    else:
        extras = "".join(f', "{sg}"' for sg in extra_security_group_ids)
        security_groups_ref = f"[aws_security_group.tasks.id{extras}]"

    placement = _resolve_subnet_group_placement(
        subnet_group_name,
        net_plan=net_plan,
        default_subnets_ref=default_subnets_ref,
        default_assign_public_ip=default_assign_public_ip,
        where=f"service {getattr(spec, 'name', '?')!r}: subnet_group",
    )

    return {
        "subnets_ref": placement["subnets_ref"],
        "security_groups_ref": security_groups_ref,
        "assign_public_ip": placement["assign_public_ip"],
        "declared_subnet_group": subnet_group_name,
        "declared_security_groups": declared_sgs,
    }


# IMDS defaults for the ECS container instances in aws_launch_template.ec2.
#
# http_tokens = "required" is IMDSv2-only, and it is the setting that actually
# matters: without it, any SSRF or request-forgery bug reachable from a
# container can read the *instance* role's credentials with a plain
# `GET http://169.254.169.254/...`. IMDSv2 requires a PUT to mint a session
# token first, which neither a forged GET nor a naive proxy can do. Safe as a
# default here because rc pins the ECS-optimized AL2 AMI (see the ssm_parameter
# data source in capacity.tf.j2), whose ECS agent, SSM agent and CloudWatch
# agent have all spoken IMDSv2 since 2019 — and rc's tasks take their
# credentials from the ECS task metadata endpoint (169.254.170.2), not IMDS,
# so nothing rc runs in a container is affected either way.
#
# The hop limit is 2, NOT 1, and that is the interesting half:
#
#   * The value is the IP TTL of the token response, and each container
#     network hop decrements it. 1 reaches only processes in the instance's
#     own network namespace; anything on the docker bridge (every bridge-mode
#     container, including tooling baked into the ECS-optimized AMI) is cut
#     off. That breakage is silent and instance-wide, so 1 is not a safe
#     default for stacks that already exist.
#   * awsvpc — the network mode rc uses for every task — does NOT make hop
#     limit 1 the container cut-off it looks like. An awsvpc task owns its
#     ENI and reaches 169.254.169.254 over it directly, so the TTL budget is
#     not spent the way a bridge-mode container's is. The knob that reliably
#     denies awsvpc tasks the instance role is the ECS agent's
#     ECS_AWSVPC_BLOCK_IMDS, which rc exposes separately as `block_task_imds`.
#
# So: IMDSv2 on by default (real mitigation, no known breakage), hop limit at
# the container-compatible value, and the two settings that CAN break a
# running stack left opt-in.
IMDS_DEFAULT_TOKENS = "required"
IMDS_DEFAULT_HOP_LIMIT = 2
VALID_IMDS_TOKEN_MODES = {"required", "optional"}


# The root volume an ECS container instance gets when the launch template
# declares no block_device_mappings: whatever the AMI ships. Verified live
# against the AMI capacity.tf.j2 resolves (2026-08-18):
#
#   aws ec2 describe-images --image-ids $(aws ssm get-parameter \
#     --name /aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id \
#     --query Parameter.Value --output text) \
#     --query 'Images[0].{Root:RootDeviceName,Bdm:BlockDeviceMappings}'
#   -> RootDeviceName=/dev/xvda, VolumeSize=30, VolumeType=gp2
#
# device_name has to match that root device exactly. Any other name adds a
# SECOND, unmounted volume rather than resizing root — a clean plan, a real
# bill, and the original problem untouched.
ECS_AMI_ROOT_DEVICE_NAME = "/dev/xvda"
ECS_AMI_DEFAULT_ROOT_VOLUME_GIB = 30

# gp3 rather than the AMI's gp2: same durability, cheaper per GiB, and its
# baseline 3000 IOPS is not tied to volume size the way gp2's is. Only
# applies when the user opts into a root volume at all.
ROOT_VOLUME_TYPE_DEFAULT = "gp3"
VALID_ROOT_VOLUME_TYPES = {"gp2", "gp3", "io1", "io2", "standard"}

# EBS root volumes must be at least as large as the AMI snapshot; AWS rejects
# anything smaller outright.
MIN_ROOT_VOLUME_GIB = ECS_AMI_DEFAULT_ROOT_VOLUME_GIB


# ECS managed-scaling defaults. target_capacity is what ECS treats as "full": at
# 80 it holds 20% of the fleet spare and scales OUT to restore that margin.
# Defaults preserved exactly, so every existing EC2 stack renders byte-identically.
MANAGED_SCALING_TARGET_DEFAULT = 80
MANAGED_SCALING_MIN_STEP_DEFAULT = 1
MANAGED_SCALING_MAX_STEP_DEFAULT = 10


def _resolve_managed_scaling(user_cfg: dict) -> dict:
    """Validate ``ec2_capacity`` managed-scaling knobs (rc-bbq).

    These were hardcoded at 80 / 1..10 in capacity.tf.j2, which is a reasonable
    default for a fleet meant to absorb bursts and the wrong one for a fleet
    deliberately packed.

    MEASURED, on the first real EC2 tenant (foundry-tenant-obwbqa, 2026-08-19):
    6 tasks declaring 2304 MiB on an m6i.large that registers 7817 MiB is 29%
    utilised, and ECS scaled the ASG to 2 instances to restore its 20% margin.
    Two boxes for a workload occupying a third of one, and nothing to do with
    whether it fit.

    target_capacity=100 is legitimate and means "hold no spare": correct when the
    fleet is packed on purpose and a rolling deploy's headroom is already modelled
    per-service (rc-anl6 sizes for deployment_maximum_percent), so paying for a
    second idle box to cover the same roll is buying it twice.
    """

    def _int(key, default, lo, hi):
        v = user_cfg.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ProviderConfigError(
                f"ec2_capacity.{key} must be an integer, got {v!r}"
            )
        if not lo <= v <= hi:
            raise ProviderConfigError(
                f"ec2_capacity.{key} must be within AWS's accepted range "
                f"{lo}..{hi}, got {v}"
            )
        return v

    target = _int("target_capacity", MANAGED_SCALING_TARGET_DEFAULT, 1, 100)
    lo = _int("minimum_scaling_step_size", MANAGED_SCALING_MIN_STEP_DEFAULT, 1, 10000)
    hi = _int("maximum_scaling_step_size", MANAGED_SCALING_MAX_STEP_DEFAULT, 1, 10000)
    if lo > hi:
        raise ProviderConfigError(
            f"ec2_capacity.minimum_scaling_step_size ({lo}) cannot exceed "
            f"maximum_scaling_step_size ({hi})"
        )
    return {
        "managed_scaling_target": target,
        "managed_scaling_min_step": lo,
        "managed_scaling_max_step": hi,
    }


def _resolve_root_volume_options(user_cfg: dict) -> dict:
    """Validate the root-volume knobs under ``provider_config.ecs.ec2_capacity``.

    rc-hbjb. ``ephemeral_storage`` is a Fargate-only task field and rc
    correctly rejects it on EC2 — but until now it offered nothing in its
    place, so the user deleted the setting to satisfy the error and silently
    lost the capacity: capacity.tf.j2 declared no block_device_mappings, so
    every instance took the AMI's 30 GiB and every binpacked task on it
    shared that one disk.

    Instance-level, beside ``instance_type``, because the resource that owns
    it is ``aws_launch_template.ec2`` — a root volume belongs to the
    container instance, not to any one task on it. That is also the
    substantive difference from Fargate's ``ephemeral_storage``, which is
    per-task and private, and the reason the two are not interchangeable.
    """
    size = user_cfg.get("root_volume_size")
    if size is None:
        return {
            "root_volume_size": None,
            "root_volume_type": ROOT_VOLUME_TYPE_DEFAULT,
            "root_volume_device": ECS_AMI_ROOT_DEVICE_NAME,
            "root_volume_encrypted": True,
        }
    if isinstance(size, bool) or not isinstance(size, int):
        raise ProviderConfigError(
            f"ec2_capacity.root_volume_size must be an integer number of "
            f"GiB, got {size!r}"
        )
    if size < MIN_ROOT_VOLUME_GIB:
        raise ProviderConfigError(
            f"ec2_capacity.root_volume_size must be at least "
            f"{MIN_ROOT_VOLUME_GIB} GiB (an EBS root volume cannot be smaller "
            f"than the ECS-optimized AMI's own snapshot), got {size}"
        )
    vol_type = str(user_cfg.get("root_volume_type", ROOT_VOLUME_TYPE_DEFAULT))
    if vol_type not in VALID_ROOT_VOLUME_TYPES:
        raise ProviderConfigError(
            f"ec2_capacity.root_volume_type must be one of "
            f"{sorted(VALID_ROOT_VOLUME_TYPES)}, got {vol_type!r}"
        )
    return {
        "root_volume_size": size,
        "root_volume_type": vol_type,
        "root_volume_device": ECS_AMI_ROOT_DEVICE_NAME,
        "root_volume_encrypted": bool(user_cfg.get("root_volume_encrypted", True)),
    }


def _trunking_state(eni_trunking: Optional[bool]) -> str:
    """Map the resolved account setting onto autosize's three-state vocabulary.

    None is UNKNOWN, never DISABLED (rc-hguq ask 3): rc must not report an
    assumption as a verified finding.
    """
    if eni_trunking is True:
        return TRUNKING_ENABLED
    if eni_trunking is False:
        return TRUNKING_DISABLED
    return TRUNKING_UNKNOWN


def _resolve_imds_options(user_cfg: dict) -> dict:
    """Validate the IMDS knobs under ``provider_config.ecs.ec2_capacity``.

    Instance-level, so it lives beside ``instance_type`` / ``capacity_type``
    rather than on a service: ``aws_ecs_task_definition`` has no
    ``metadata_options`` argument, and a Fargate task has no IMDS to harden in
    the first place (its credentials come from 169.254.170.2). The exposure is
    entirely EC2-backed ECS, and the resource that owns it is
    ``aws_launch_template.ec2``.
    """
    tokens = str(user_cfg.get("imdsv2", IMDS_DEFAULT_TOKENS)).strip().lower()
    if tokens not in VALID_IMDS_TOKEN_MODES:
        raise ProviderConfigError(
            f"ec2_capacity.imdsv2 must be 'required' or 'optional', got "
            f"{user_cfg.get('imdsv2')!r}"
        )
    hop_limit = user_cfg.get("metadata_hop_limit", IMDS_DEFAULT_HOP_LIMIT)
    if isinstance(hop_limit, bool) or not isinstance(hop_limit, int):
        raise ProviderConfigError(
            f"ec2_capacity.metadata_hop_limit must be an integer, got {hop_limit!r}"
        )
    if not 1 <= hop_limit <= 64:
        raise ProviderConfigError(
            f"ec2_capacity.metadata_hop_limit must be between 1 and 64, got "
            f"{hop_limit}"
        )
    return {
        "imds_tokens": tokens,
        "imds_hop_limit": hop_limit,
        "block_task_imds": bool(user_cfg.get("block_task_imds", False)),
    }


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


# Default first listener-rule priority; rules step by 10 from here so operators
# can hand-write rules in between. Overridable per project via
# existing_alb.listener_rule_priority_base when several projects share ONE
# adopted listener — priorities are unique per listener, not per project
# (rc-4tkc.2).
LISTENER_RULE_PRIORITY_BASE = 100

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


def _is_stateful_service(
    name: str, spec: Any, *, mount_count: "int | None" = None
) -> bool:
    """Whether this service must roll one-at-a-time (stop-then-start).

    rc-usk0: ONE predicate, shared by the terraform emit path and the
    ``--no-state`` force-roll. They used to compute this separately — the roll
    path checked ``spec.volumes`` alone and never consulted
    ``_looks_like_singleton_scheduler`` — so a celery-beat with no EFS mount was
    stateful in terraform and stateless in the roll. Since the roll runs on every
    deploy, it silently won: it reset the service to min=100/max=200 AND ran two
    schedulers during the overlap, which is the exact failure rc-e5u.46.10 added
    the singleton heuristic to prevent.

    ``mount_count`` lets the terraform path pass its already-computed volume
    count; omit it and the spec's own ``volumes`` are used.
    """
    if mount_count is None:
        mount_count = len(getattr(spec, "volumes", None) or []) if spec else 0
    if mount_count > 0:
        return True
    if spec is None:
        return False
    if getattr(spec, "stateful", False):
        return True
    return _looks_like_singleton_scheduler(name, getattr(spec, "command", None))


def _stateful_reason(name: str, spec: Any, *, mount_count: "int | None" = None) -> str:
    """Which of ``_is_stateful_service``'s three signals fired, in words.

    Only used to make the rejection in ``_deployment_percents`` actionable:
    statefulness is INFERRED, so a user whose service happens to be called
    ``report-scheduler`` must not be handed a lecture about EFS data
    directories it does not have.
    """
    if mount_count is None:
        mount_count = len(getattr(spec, "volumes", None) or []) if spec else 0
    if mount_count > 0:
        return f"it mounts {mount_count} EFS volume(s)"
    if getattr(spec, "stateful", False):
        return "rc.yml sets stateful: true on it"
    return (
        "rc reads it as a singleton scheduler (name ends in "
        "-beat/-scheduler/-cron, or the command runs celery beat)"
    )


# rc's minimum_healthy_percent for a normal rolling deploy, matching what
# services.tf.j2 has always rendered. Its 200% counterpart lives in
# autosize.py (DEPLOYMENT_MAX_PERCENT_DEFAULT) because the ASG sizer needs
# it too. Stateful services pin (0, 100) instead — stop-then-start.
DEPLOYMENT_MIN_HEALTHY_PERCENT_DEFAULT = 100
STATEFUL_DEPLOYMENT_PERCENTS = (0, 100)
_DEPLOYMENT_KEYS = ("minimum_healthy_percent", "maximum_percent")


class DeploymentPercents(NamedTuple):
    minimum_healthy: int
    maximum: int
    #: True when rc.yml supplied either value, i.e. the numbers below are
    #: NOT rc's defaults. Drives the explanatory comment in services.tf.j2
    #: (and nothing else — the emitted percentages are the same shape
    #: either way).
    overridden: bool


def _deployment_percents(
    name: str,
    spec: Any,
    *,
    mount_count: "int | None" = None,
) -> DeploymentPercents:
    """Resolve this service's rollout percentages. THE single decision point.

    rc-6akx. Three places have to agree on these two numbers, and every one of
    them is load-bearing:

      1. ``services.tf.j2``          — what terraform applies to the service.
      2. ``EC2TaskDemand``           — what the ASG is sized for (rc-anl6).
      3. ``_force_new_deployments``  — what ``rc deploy --no-state`` writes
                                       onto the LIVE service on every deploy.

    (3) is why this is a shared helper rather than three inline literals: the
    no-state roll runs on every deploy and would silently reset the service
    back to 100/200, which is precisely the class of bug rc-usk0 fixed.

    Defaults are unchanged from before this existed — (100, 200) for a
    stateless service, (0, 100) for a stateful one — so a stack with no
    ``deployment:`` block emits byte-identical terraform.

    Raises ``ProviderConfigError`` on any input rc cannot honour, rather than
    emitting terraform AWS will accept and then wedge on: a service whose
    percentages cannot roll is not a failed plan, it is a service that can
    never be deployed again.
    """
    stateful = _is_stateful_service(name, spec, mount_count=mount_count)
    default = (
        STATEFUL_DEPLOYMENT_PERCENTS
        if stateful
        else (DEPLOYMENT_MIN_HEALTHY_PERCENT_DEFAULT, DEPLOYMENT_MAX_PERCENT_DEFAULT)
    )
    raw = getattr(spec, "deployment", None) if spec is not None else None
    if raw is None or raw == {}:
        return DeploymentPercents(default[0], default[1], overridden=False)

    where = f"service {name!r}: deployment"
    if not isinstance(raw, dict):
        raise ProviderConfigError(
            f"{where} must be a mapping with keys {list(_DEPLOYMENT_KEYS)}, "
            f"got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - set(_DEPLOYMENT_KEYS))
    if unknown:
        # Services have no unknown-key rejection at the config layer, so a
        # typo here would otherwise parse into a mapping rc silently ignores
        # — the user sets a knob, sees no change, and blames the feature.
        raise ProviderConfigError(
            f"{where}: unknown key(s) {unknown} "
            f"(supported: {list(_DEPLOYMENT_KEYS)})"
        )
    if stateful:
        raise ProviderConfigError(
            f"{where} is not available on this service because rc rolls it "
            f"stop-then-start ({_stateful_reason(name, spec, mount_count=mount_count)}). "
            f"Those services are pinned at minimum_healthy_percent=0 / "
            f"maximum_percent=100 so two tasks NEVER share the data directory "
            f"— postgres initdb will wipe a volume the outgoing task still "
            f"holds — and a rollout percentage that permits overlap would "
            f"undo exactly that guarantee. Remove the deployment: block."
        )

    values: dict[str, int] = {}
    for key in _DEPLOYMENT_KEYS:
        if key not in raw:
            continue
        val = raw[key]
        # bool is a subclass of int, and YAML turns `maximum_percent: yes`
        # into True — which would sail through an isinstance(int) check and
        # silently become 1.
        if isinstance(val, bool) or not isinstance(val, int):
            raise ProviderConfigError(
                f"{where}.{key} must be an integer percentage, got {val!r}"
            )
        values[key] = val

    min_pct = values.get("minimum_healthy_percent", default[0])
    max_pct = values.get("maximum_percent", default[1])
    if not 0 <= min_pct <= 100:
        raise ProviderConfigError(
            f"{where}.minimum_healthy_percent must be between 0 and 100, "
            f"got {min_pct} — it is the floor on tasks kept RUNNING during a "
            f"roll, as a percentage of replicas, so above 100 is meaningless."
        )
    if max_pct < 100:
        raise ProviderConfigError(
            f"{where}.maximum_percent must be at least 100, got {max_pct} — "
            f"below 100 the service could not even hold its own steady-state "
            f"task count while deploying."
        )

    replicas = getattr(spec, "replicas", 1) or 0
    if replicas >= 1:
        # ECS rounds minimumHealthyPercent UP and maximumPercent DOWN against
        # desiredCount. A roll is only possible if it can either start a
        # replacement first (needs room for replicas+1) or stop an old task
        # first (needs the floor to sit below replicas). Neither => the
        # service deadlocks and every future deploy hangs.
        #
        # This is the general form of the two cases people trip over:
        # 100/100 cannot roll at ANY replica count, and anything above 0
        # minimum with maximum=100 cannot roll at ONE replica.
        min_running = math.ceil(replicas * min_pct / 100)
        max_running = (replicas * max_pct) // 100
        if max_running < replicas + 1 and replicas - 1 < min_running:
            raise ProviderConfigError(
                f"{where}: minimum_healthy_percent={min_pct} / "
                f"maximum_percent={max_pct} with replicas={replicas} can never "
                f"roll. ECS would have to keep {min_running} task(s) healthy "
                f"while running at most {max_running}, so it can neither start "
                f"a replacement nor stop an old task, and every deploy of this "
                f"service hangs until it times out. Either lower "
                f"minimum_healthy_percent to at most "
                f"{_largest_rollable_min_percent(replicas)} (so a task can be "
                f"stopped and replaced in place), or raise maximum_percent to "
                f"at least {_smallest_rollable_max_percent(replicas)} (so a "
                f"replacement can start alongside)."
            )

    return DeploymentPercents(min_pct, max_pct, overridden=True)


def _largest_rollable_min_percent(replicas: int) -> int:
    """Biggest minimum_healthy_percent that still permits a stop-then-replace.

    Purely for the error message above — a number the user can paste beats
    "lower it a bit". Independent of maximum_percent: stopping first needs no
    extra capacity at all. Returns 0 at replicas=1, where replacing in place
    necessarily means going to zero tasks for a moment.
    """
    for pct in range(100, -1, -1):
        if math.ceil(replicas * pct / 100) <= replicas - 1:
            return pct
    return 0


def _smallest_rollable_max_percent(replicas: int) -> int:
    """Smallest maximum_percent leaving room for a replacement to start first.

    ECS rounds maximumPercent DOWN, so the ceiling has to reach replicas+1
    running tasks: 200 at one replica, but only 134 at three. "Raise it above
    100" would be wrong advice for anyone already sitting between 100 and 200
    — 150 at one replica still floors to 1 and still deadlocks.
    """
    return math.ceil((replicas + 1) * 100 / replicas)


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


# Environment variables that mean "this process already has credentials
# without any named profile" — the CI/OIDC/container shape. Ordered
# most-specific-first so the reported name is the informative one.
#
# AWS_PROFILE is deliberately NOT here: it names a profile, it does not
# supply credentials, so a set-but-unresolvable AWS_PROFILE is the same
# broken state rc-rigk is about, not a rescue from it.
_AMBIENT_CREDENTIAL_ENV_VARS = (
    "AWS_WEB_IDENTITY_TOKEN_FILE",  # OIDC (GitHub Actions, IRSA)
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",  # ECS task / CodeBuild
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_ACCESS_KEY_ID",  # static keys in the environment
)

# Tri-state results of _aws_profile_status(). "unknown" follows the module's
# existing "not modeled" convention (InstanceShape.max_enis=None,
# KNOWN_INSTANCE_SHAPES misses): when rc cannot determine the answer it must
# not act as though it had, in EITHER direction.
PROFILE_UNSET = "unset"
PROFILE_PRESENT = "present"
PROFILE_ABSENT = "absent"
PROFILE_UNKNOWN = "unknown"


def _ambient_aws_credentials() -> list[str]:
    """Names of ambient AWS credential env vars set in this process."""
    return [v for v in _AMBIENT_CREDENTIAL_ENV_VARS if os.environ.get(v)]


def _aws_config_search_paths() -> list[str]:
    """The shared-config files botocore would read, for error messages.

    Reported as paths rather than "your AWS config" because the single most
    common cause of a missing profile is that the file rc looked in is not
    the file the user edited (AWS_CONFIG_FILE set, different $HOME under
    sudo/CI, ...). Naming both files removes that guess.
    """
    return [
        os.environ.get("AWS_CONFIG_FILE") or os.path.expanduser("~/.aws/config"),
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
        or os.path.expanduser("~/.aws/credentials"),
    ]


def _aws_profile_status(profile: Optional[str]) -> str:
    """Whether ``profile`` resolves in the shared AWS config (tri-state).

    Returns PROFILE_UNSET / PROFILE_PRESENT / PROFILE_ABSENT /
    PROFILE_UNKNOWN. PROFILE_UNKNOWN means the probe itself failed — no
    botocore installed, an unreadable/malformed config file — and callers
    must treat it as "no information", never as absent.
    """
    if not profile:
        return PROFILE_UNSET
    try:
        import botocore.session  # noqa: WPS433

        config = botocore.session.Session().full_config or {}
    except Exception:  # noqa: BLE001 — a failed probe is not evidence
        return PROFILE_UNKNOWN
    return (
        PROFILE_PRESENT if profile in (config.get("profiles") or {}) else PROFILE_ABSENT
    )


def resolve_run_launch(svc: dict, task_def: dict) -> dict[str, Any]:
    """RunTask kwargs that place a one-off task the way its SERVICE runs.

    rc-fg83. ``run_one_off`` used ``svc.get("launchType") or "FARGATE"``,
    and that fallback is not a harmless default -- it is wrong precisely when
    it fires. ``launchType`` and ``capacityProviderStrategy`` are mutually
    exclusive on a service, so an EC2 service (which rc renders with a
    capacity provider strategy) reports NO launchType at all. The fallback
    then sent launchType=FARGATE for a task definition whose
    requiresCompatibilities is ["EC2"], and RunTask refused it:

        InvalidParameterException: Task definition does not support
        launch_type FARGATE

    Resolution order, so ``rc run django`` and the django service always
    agree about where work runs:

      1. The service's own capacityProviderStrategy -- the one-off lands on
         the same ASG the services use, and engages that provider's managed
         scaling, which a bare launchType=EC2 would not.
      2. The service's own launchType.
      3. The task definition's requiresCompatibilities, preferring FARGATE
         when it declares both (matching what the service resolution would
         pick for a task def that can do either).
      4. Nothing -- let ECS apply the cluster's default capacity provider
         strategy rather than guess a launch type that may be rejected.

    Never returns both keys: RunTask rejects that combination.
    """
    strategy = svc.get("capacityProviderStrategy")
    if strategy:
        return {"capacityProviderStrategy": list(strategy)}
    launch_type = svc.get("launchType")
    if launch_type:
        return {"launchType": launch_type}
    compatibilities = [
        str(c).upper() for c in (task_def or {}).get("requiresCompatibilities") or []
    ]
    if "FARGATE" in compatibilities:
        return {"launchType": "FARGATE"}
    if "EC2" in compatibilities:
        return {"launchType": "EC2"}
    return {}


def _profile_is_resolvable(profile: Optional[str]) -> bool:
    """True only if a named AWS profile actually exists in the shared config.

    The boto3 session path (``_default_session_factory``) already falls back
    to the default credential chain when a profile is absent. CLI subprocesses
    don't get that for free: setting ``AWS_PROFILE=default`` (or passing
    ``--profile``) makes the aws CLI hard-fail with "config profile could not
    be found" under OIDC / env-only credentials. Mirror the session fallback
    so exec/run subprocesses only pin a profile that's really there; otherwise
    they inherit the ambient credential chain (env vars, assumed role, ...).

    Collapses _aws_profile_status()'s tri-state to "present or not": a probe
    that couldn't answer means don't pin a profile on the subprocess, which
    is the safe direction for exec/run specifically (the ambient chain still
    works; a bogus AWS_PROFILE does not).
    """
    return _aws_profile_status(profile) == PROFILE_PRESENT


def check_aws_profile_for_terraform(profile: Optional[str]) -> tuple[bool, str]:
    """Decide what the terraform provider block should do with ``aws_profile``.

    Returns ``(omit, message)``. ``omit`` True means "render no profile line
    and let terraform use the ambient credential chain"; ``message`` is a
    warning to surface when non-empty.

    rc-rigk: ``profile = "..."`` renders into providers.tf whenever
    provider_config.ecs.aws_profile is set, but a named profile is a LAPTOP
    concept. On an OIDC runner credentials arrive as environment variables
    and no named profile exists, so terraform dies with "failed to get
    shared config profile, default" — an error that names neither rc.yml nor
    the fact that a profile was never needed here. Three cases:

      * profile resolves          -> render it. It is explicit and it works,
                                     ambient credentials or not.
      * absent + ambient creds    -> omit + warn. This is CI. The stack has
                                     working credentials; the profile line is
                                     the only thing that would break it.
      * absent + no ambient creds -> caller's decision (raise). Nothing here
                                     can save the deploy, so the value is in
                                     saying WHICH thing is missing, early.

    A PROFILE_UNKNOWN probe renders the profile unchanged — rc has no
    evidence and must not silently drop a setting the user wrote down.
    """
    status = _aws_profile_status(profile)
    if status in (PROFILE_UNSET, PROFILE_PRESENT, PROFILE_UNKNOWN):
        return False, ""
    ambient = _ambient_aws_credentials()
    if not ambient:
        return False, ""
    return True, (
        f"provider_config.ecs.aws_profile: {profile!r} is not a profile in "
        f"{' or '.join(_aws_config_search_paths())}, but this environment "
        f"already carries AWS credentials ({', '.join(ambient)}) — the CI / "
        f'OIDC shape. rc is omitting `profile = "{profile}"` from the '
        f"terraform provider so it uses those credentials instead; leaving "
        f"it in would fail the apply with terraform's "
        f'"failed to get shared config profile, {profile}". A named '
        f"profile only exists on a workstation: drop aws_profile from rc.yml "
        f"if this stack deploys from CI."
    )


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


def _service_prefix(ctx: DeployContext) -> str:
    """The prefix rc renders into every ECS service name (rc-py32).

    ``service_name_prefix`` exists because ECS service names are unique per
    CLUSTER: when several projects adopt one shared cluster they would otherwise
    all try to own a service called ``django``. rc applies it when RENDERING
    terraform, so the live service is ``<prefix><name>`` while ``ctx.services``
    is still keyed by the bare compose name.

    Every runtime path that hands a service name to the ECS API therefore has to
    add it back, and every path that reads a serviceName off the API has to take
    it off again (``_compose_service_name``). Without that rc writes
    ``acme-django`` and then talks to ``django``: status reports every service
    missing, redeploy updates nothing, exec cannot find a task.
    """
    return str(_ecs_cfg(ctx).get("service_name_prefix") or "")


# Fields a task group takes from its ECS *service* / *task* rather than from a
# container. Every member is validated to agree on the rc.yml inputs behind
# these (see ``validate_task_groups``), so reading them off the first member is
# well-defined -- and for a group of one it is simply that service's own value,
# which is what keeps the rendered output byte-identical.
_GROUP_TASK_FIELDS = (
    "type",
    "launch_type",
    "replicas",
    "stateful",
    "deployment_min_healthy_percent",
    "deployment_max_percent",
    "deployment_overridden",
    "ephemeral_storage",
    "declared_iam_role",
    "task_role_ref",
    "subnets_ref",
    "security_groups_ref",
    "assign_public_ip",
    "declared_subnet_group",
    "declared_security_groups",
)

# Fields that describe how the ALB reaches the group. They come from the
# INGRESS container, not from the first member: a target group names one
# container_name/container_port pair, and only one container in a task can be
# behind the load balancer.
_GROUP_INGRESS_FIELDS = (
    "public",
    "port",
    "domain",
    "aliases",
    "default_target",
    "health_check_path",
    "health_check_grace_period",
)


def _task_group_views(
    resolved: dict[str, Any],
    services_view: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold per-service views into one view per ECS task.

    One ``aws_ecs_task_definition`` + one ``aws_ecs_service`` per GROUP, with
    the per-container block looping over ``group["containers"]``.

    A group of one produces a view whose every task/service field is that
    service's own, whose single container is that service, and whose name and
    tf_name are unchanged -- so the degenerate case renders exactly what rc
    emitted before task groups existed, without the template branching on it.
    """
    by_name = {sv["name"]: sv for sv in services_view}
    out: list[dict[str, Any]] = []

    for gname in sorted(resolved):
        group = resolved[gname]
        containers = [by_name[m] for m in group.members if m in by_name]
        if not containers:
            continue
        anchor = containers[0]
        ingress = by_name.get(group.ingress) if group.ingress else None

        view: dict[str, Any] = {
            "name": group.name,
            "tf_name": _tf_name(group.name),
            # Task-level memory is the SUM of the members unless rc.yml
            # overrides it. One hard ceiling now covers N containers: they can
            # share slack within the task (better than N separate hard
            # reservations), but a runaway member can starve its siblings.
            "memory": group.memory,
            "containers": containers,
            "is_implicit": group.is_implicit,
            "retired_hostnames": list(group.retired_hostnames),
            # Union of every member's mounts. These render the task-level
            # `volume` blocks; each container keeps its own `mounts` for its
            # mountPoints. Two members cannot claim one volume name (rc mints
            # an access point per service per volume), which
            # validate_task_groups rejects, so this needs no dedupe pass.
            "mounts": [m for c in containers for m in c.get("mounts") or []],
        }
        for key in _GROUP_TASK_FIELDS:
            view[key] = anchor.get(key)
        # cpu sums for the same reason memory does. On FARGATE it is a required
        # task-level reservation shared by every container, and AWS only accepts
        # certain cpu/memory PAIRS -- taking one member's cpu beside the summed
        # memory would produce combinations Fargate rejects (256 cpu next to
        # 4096 memory). On EC2 it is not rendered at all (startsim-u88y), so
        # summing costs nothing there. A group of one sums to that member's own
        # value, which is what keeps the output byte-identical.
        view["cpu"] = sum(int(c.get("cpu") or 0) for c in containers)
        for key in _GROUP_INGRESS_FIELDS:
            view[key] = (ingress or {}).get(key)
        # container_name/container_port for the load_balancer block.
        view["ingress_container"] = (ingress or {}).get("name")
        # health_check is a CONTAINER field; a group has no single one. The
        # template reads it off each container instead.
        view["health_check"] = None
        out.append(view)

    return out


def _ecs_service_name(ctx: DeployContext, compose_name: str) -> str:
    """compose service name -> the live ECS service name."""
    return f"{_service_prefix(ctx)}{compose_name}"


def _compose_service_name(ctx: DeployContext, ecs_name: str) -> str:
    """Live ECS service name -> the compose name, for reporting.

    Only strips a prefix that is actually there, so an unprefixed project (the
    default) and a service whose own name happens to start with the prefix text
    both round-trip unchanged.
    """
    prefix = _service_prefix(ctx)
    if prefix and ecs_name.startswith(prefix):
        return ecs_name[len(prefix) :]
    return ecs_name


def _destroy_drain_timeout_s(default_s: int = 600) -> int:
    """Bound on each pre-drain poll loop in ``destroy()`` (rc-e5u.25.9).

    RC_DESTROY_DRAIN_TIMEOUT_S overrides the default; tests set it to 0
    to short-circuit the wait entirely without mocking time.sleep. A
    timeout here only ever produces a WARN -- terraform's own destroy
    timeout is the real backstop, never this one.
    """
    raw = os.environ.get("RC_DESTROY_DRAIN_TIMEOUT_S")
    if not raw:
        return default_s
    try:
        n = int(raw)
        return n if n >= 0 else default_s
    except ValueError:
        return default_s


def image_group_owners(
    services: dict[str, Any], share_repos: bool = True
) -> dict[str, str]:
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
    # Opt-out (provider_config.ecs.share_image_repos: false). Every build-having
    # service owns its OWN repo — no build-identity grouping. For stacks whose
    # live ECR layout predates rc-44i (per-service repos, still referenced by the
    # running task defs), grouping would emit fewer repos and a regen would try
    # to DESTROY the per-service repos that are in active use. Keeping them
    # per-service makes the config match reality (no destroys).
    if not share_repos:
        return {
            name: name
            for name, spec in services.items()
            if getattr(spec, "build_context", None)
        }

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


def _services_to_build(
    services: dict[str, Any], services_filter=None, share_repos: bool = True
) -> list:
    """The service specs to actually build+push (rc-44i): one OWNER per image
    group, never the siblings (their task def references the owner image). A
    services_filter naming a sibling maps to its owner so the shared image is
    still rebuilt. Order follows sorted service name for determinism. When
    share_repos is false, every build-having service is its own owner (built +
    pushed separately)."""
    owners = image_group_owners(services, share_repos=share_repos)
    members: dict[str, set] = {}
    for name, owner in owners.items():
        members.setdefault(owner, set()).add(name)
    build_owners = [
        spec
        for name, spec in sorted(services.items())
        if spec.build_context and owners.get(name, name) == name
    ]
    if services_filter is None:
        # rc-7ga: exclude opt-out services (auto_roll=False) from the DEFAULT
        # set so stateful single-task services (postgres) don't churn on every
        # app deploy. An explicit --services filter (below) overrides this.
        return [spec for spec in build_owners if getattr(spec, "auto_roll", True)]
    allowed = set(services_filter)
    return [
        spec for spec in build_owners if members.get(spec.name, {spec.name}) & allowed
    ]


def roll_targets_for_pushed(
    services: dict[str, Any], pushed: list[str], share_repos: bool = True
) -> list[str]:
    """Expand the pushed image-group OWNERS to every member of their groups
    (rc-wji.1).

    _build_and_push_images returns one OWNER per image group, but EVERY sibling
    that references that shared image must be force-rolled too — otherwise
    siblings keep running the old image while the new :latest sits unused (the
    django/celery-* staleness bug). Shared by both deploy() and
    _deploy_no_state() so the two roll paths can't drift. Sorted for
    determinism.
    """
    owners = image_group_owners(services, share_repos=share_repos)
    pushed_owners = set(pushed)
    return sorted(name for name in services if owners.get(name, name) in pushed_owners)


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
        # Plan-time findings raised from inside emit_terraform (and the
        # helpers it calls) -- see _warn() / _drain_warnings(). Kept on the
        # instance because emit_terraform returns a Path, and the resolvers
        # that notice these things (_resolve_ec2_capacity, aws_profile
        # resolution, the per-service EC2 loop) sit several frames down with
        # no return channel of their own.
        self._warnings: list[str] = []

    # -----------------------------------------------------------------
    # Plan-time warning sink
    # -----------------------------------------------------------------

    def _warn(self, message: str) -> None:
        """Record a non-fatal plan-time finding.

        Same prose convention as compose_warnings.py: each message is
        self-contained, so the user reading `rc plan` output never has to
        chase a code or look anything up to act on it. Drained by plan()
        into PlanResult.warnings and by deploy() into DeployResult.warnings.
        """
        if message not in self._warnings:
            self._warnings.append(message)

    def _drain_warnings(self) -> list[str]:
        """Return the accumulated warnings and reset the sink."""
        out = list(self._warnings)
        self._warnings = []
        return out

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
        # rc-rigk: preflight() may have determined this profile does not
        # resolve while the environment supplies credentials anyway (CI /
        # OIDC). Rendering `profile = "..."` there fails every apply, so the
        # line is dropped. Default False, so emit_terraform() called on its
        # own — tests, golden fixtures — renders exactly what rc.yml says and
        # never depends on the host's ~/.aws.
        aws_profile = (
            None
            if getattr(ctx, "omit_aws_profile", False)
            else ecs_cfg.get("aws_profile")
        )

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

        # rc-0cv: a service with no explicit subnet_group (the only opt-in
        # today, and a heavier one — it requires the full network: block,
        # which in existing-VPC mode always carves a BRAND NEW subnet, not
        # an adopted one) always landed on public_subnet_ids with a public
        # IP. There was no way to make "private by default" the environment-
        # wide behavior even when the caller already threaded real, adopted
        # private_subnet_ids through provider_config.ecs — e.g. every
        # Foundry tenant service in start-simpli-api ran with a public IP
        # solely because rc had no default to tell it otherwise (rc-0cv).
        default_subnet_placement = ecs_cfg.get("default_subnet_placement", "public")
        if default_subnet_placement not in {"public", "private"}:
            raise ProviderConfigError(
                "provider_config.ecs.default_subnet_placement must be "
                f"'public' or 'private', got {default_subnet_placement!r}"
            )

        # Existing-ALB adopt (rc-adopt, D4): reference a live ALB + its HTTPS
        # listener instead of creating one — for adopt-in-place of a stack
        # already fronted by an ALB (e.g. browser-mgr's Copilot ALB + the
        # Namecheap CNAME). rc emits no aws_lb / listeners / alb SG; it adds
        # host-based listener RULES + per-service target groups onto the
        # existing listener and reads dns_name/zone_id/security_groups off a
        # data source. The existing listener keeps its own default action, so
        # adopting an ALB requires the public service(s) to declare a domain
        # (host-based routing) — there's no rc-managed catch-all to point.
        # Shared-cluster adoption (startsim-wyn2). An ECS container instance
        # registers to exactly ONE cluster, so packing several projects onto the
        # same EC2 instances requires them to SHARE A CLUSTER — a per-project
        # cluster puts a hard floor of one instance under every project.
        #
        # The unit that binds is ENI SLOTS, not memory. Every awsvpc task consumes
        # one branch ENI, and with awsvpcTrunking an m6i.large carries 10 slots
        # (xlarge 20, 2xlarge 40). Measured on foundry-tenant-obwbqa: 6 tasks
        # declaring 2304 MiB on an m6i.large registering 7817 MiB is 60% of ENI
        # but only 29% of memory — so dividing memory overstates how much fits by
        # 3x. ECS places tasks INDIVIDUALLY, so a project's tasks need not be
        # co-located; what a box holds is 10 tasks from any mix of projects, and
        # today each project gets its own cluster and therefore its own instance.
        #
        # A shared stack owns the cluster, the ASG and the capacity provider; the
        # adopting project emits none of them and references the provider by name.
        existing_cluster_cfg = ecs_cfg.get("existing_cluster") or {}
        existing_cluster = bool(existing_cluster_cfg)
        shared_capacity_provider = existing_cluster_cfg.get("capacity_provider")
        # ECS service names are unique per CLUSTER. Every tenant has a `django`,
        # so without a prefix the second one into a shared cluster collides.
        service_name_prefix = str(ecs_cfg.get("service_name_prefix") or "")

        _lt_default = ecs_cfg.get("default_launch_type", "FARGATE")
        _any_ec2 = any(
            (sp.launch_type or _lt_default) == "EC2" for sp in ctx.services.values()
        )
        if existing_cluster and _any_ec2 and not shared_capacity_provider:
            raise ProviderConfigError(
                "provider_config.ecs.existing_cluster requires "
                "'capacity_provider' when any service uses the EC2 launch type: "
                "the adopting project emits no aws_ecs_capacity_provider of its "
                "own, so it must reference the one the shared cluster stack owns "
                "by name."
            )

        existing_alb_cfg = ecs_cfg.get("existing_alb") or {}
        existing_alb = bool(existing_alb_cfg)
        existing_alb_arn = existing_alb_cfg.get("arn")
        existing_alb_https_listener_arn = existing_alb_cfg.get("https_listener_arn")
        if existing_alb and not (existing_alb_arn and existing_alb_https_listener_arn):
            raise ProviderConfigError(
                "provider_config.ecs.existing_alb requires both 'arn' and "
                "'https_listener_arn' (the live ALB + its HTTPS listener)"
            )
        # rc-4tkc.2: listener-rule priorities are unique per LISTENER, but rc
        # numbers them per PROJECT (100, 110, ...). That is fine while rc owns
        # the listener — each project has its own. The moment two projects adopt
        # the SAME listener, both emit 100 and the second apply dies on
        # PriorityInUse. An explicit band per project is the fix; deliberately
        # NOT a hash of the project name, because a hash collision here is silent
        # and priorities are a scarce ordered resource (1..50000).
        listener_rule_priority_base = existing_alb_cfg.get(
            "listener_rule_priority_base", LISTENER_RULE_PRIORITY_BASE
        )
        if not isinstance(listener_rule_priority_base, int) or isinstance(
            listener_rule_priority_base, bool
        ):
            raise ProviderConfigError(
                "provider_config.ecs.existing_alb.listener_rule_priority_base must "
                f"be an integer, got {listener_rule_priority_base!r}"
            )
        if not 1 <= listener_rule_priority_base <= 50000:
            raise ProviderConfigError(
                "provider_config.ecs.existing_alb.listener_rule_priority_base must "
                "be within AWS's listener-rule priority range 1..50000, got "
                f"{listener_rule_priority_base}"
            )

        # Adopt-and-own ALB (rc-v4c): unlike existing_alb (pure read-only
        # reference — a data source rc never creates/updates/destroys),
        # adopt_owned.alb emits a real aws_lb/aws_lb_listener *resource* for
        # a foreign ALB, imported once via terraform import, so rc actually
        # holds delete/update authority afterward. The forcing case: a live
        # ALB whose CloudFormation (or other prior-IaC) stack must be
        # retired, but rc's own services already depend on that ALB.
        #
        # A blanket `lifecycle { ignore_changes = all }` on the adopted
        # resources means rc never diffs their live attributes against what
        # it would render from scratch — the adopted ALB's real name/SGs
        # almost never match rc's `${project}-alb` conventions, and forcing
        # that convention would replace (destroy) the live, traffic-serving
        # ALB. rc owns the resource for lifecycle (import/destroy) purposes
        # only, not for day-to-day config drift correction. Coarser than
        # ideal — same documented tradeoff as ignore_task_definition_changes
        # (rc-uky tracks field-level ownership as a general follow-up).
        adopt_owned_cfg = ecs_cfg.get("adopt_owned") or {}
        adopt_owned_alb_cfg = adopt_owned_cfg.get("alb") or {}
        adopt_owned_alb = bool(adopt_owned_alb_cfg)
        adopt_owned_alb_arn = adopt_owned_alb_cfg.get("arn")
        adopt_owned_alb_http_listener_arn = adopt_owned_alb_cfg.get("http_listener_arn")
        adopt_owned_alb_https_listener_arn = adopt_owned_alb_cfg.get(
            "https_listener_arn"
        )
        adopt_owned_alb_security_group_ids = list(
            adopt_owned_alb_cfg.get("security_group_ids") or []
        )
        if adopt_owned_alb and existing_alb:
            raise ProviderConfigError(
                "provider_config.ecs.adopt_owned.alb and existing_alb are "
                "mutually exclusive — adopt_owned OWNS the ALB's lifecycle "
                "(imports it into terraform state, rc can update/destroy "
                "it); existing_alb only REFERENCES it read-only. Pick one."
            )
        if adopt_owned_alb and not (
            adopt_owned_alb_arn
            and adopt_owned_alb_http_listener_arn
            and adopt_owned_alb_security_group_ids
        ):
            raise ProviderConfigError(
                "provider_config.ecs.adopt_owned.alb requires 'arn', "
                "'http_listener_arn', and a non-empty 'security_group_ids' "
                "(the live ALB + its HTTP listener + the security groups "
                "already attached to it)"
            )

        # Existing Cloud Map namespace (rc-adopt, D5): register services into a
        # live private DNS namespace instead of creating `<project>.local`. For
        # adopt-in-place where peers already resolve the existing names — e.g.
        # debuggai-api calls django.production.browser-mgr.local:5000, so that
        # namespace must be kept, not replaced.
        # Task-role app-IAM grants (rc-8y7): attach managed policies and/or an
        # inline policy to the shared task role (aws_iam_role.task) so apps can
        # reach AWS services (S3 media, SQS, SES, ...) without an out-of-band
        # reconcile script. Mirrors the live browser-mgr GrantAccessS3Media that
        # the migration had to apply by hand (the recording-403 root cause).
        iam_cfg = ecs_cfg.get("iam") or {}
        task_iam_managed = list(iam_cfg.get("managed_policies") or [])
        task_iam_statements: list[dict[str, Any]] = []
        for i, stmt in enumerate(iam_cfg.get("statements") or []):
            actions = stmt.get("actions") or []
            resources = stmt.get("resources") or []
            if not actions or not resources:
                raise ProviderConfigError(
                    "provider_config.ecs.iam.statements[%d] requires non-empty "
                    "'actions' and 'resources'" % i
                )
            task_iam_statements.append(
                {
                    "sid": stmt.get("sid") or ("AppGrant%d" % i),
                    "actions": list(actions),
                    "resources": list(resources),
                    "condition": stmt.get("condition") or None,
                }
            )
        has_task_iam = bool(task_iam_managed or task_iam_statements)
        # rc-h72: tags on the shared task role. Adopted resource policies gate on
        # the principal's tags (e.g. Copilot's EFS file-system policy requires
        # copilot-application/environment). IAM role tags surface as
        # aws:PrincipalTag, which is what those conditions check.
        task_role_tags = dict(iam_cfg.get("role_tags") or {})
        # Render the inline policy doc in Python (JSON heredoc in the template)
        # so optional IAM Conditions serialize correctly — an HCL jsonencode
        # block can't take a JSON-shaped condition map.
        task_iam_policy_json = None
        if task_iam_statements:
            import json as _json

            task_iam_policy_json = _json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": s["sid"],
                            "Effect": "Allow",
                            "Action": s["actions"],
                            "Resource": s["resources"],
                            **({"Condition": s["condition"]} if s["condition"] else {}),
                        }
                        for s in task_iam_statements
                    ],
                },
                indent=2,
            )

        existing_cloud_map_namespace_id = ecs_cfg.get("existing_cloud_map_namespace_id")
        existing_cloud_map_namespace = bool(existing_cloud_map_namespace_id)
        if existing_cloud_map_namespace:
            service_discovery_namespace_ref = f'"{existing_cloud_map_namespace_id}"'
        else:
            service_discovery_namespace_ref = (
                "aws_service_discovery_private_dns_namespace.main.id"
            )

        # Rendering aliases keep the create path byte-identical: in create mode
        # these are exactly the original resource references; in adopt mode they
        # point at the data source + network locals. Templates read these so they
        # never branch on existing_vpc themselves.
        if existing_vpc:
            vpc_id_ref = "data.aws_vpc.main.id"
            public_subnet_ids_ref = "local.rc_public_subnet_ids"
            public_subnet_idx_ref = "local.rc_public_subnet_ids[count.index]"
            public_subnet_first_ref = "local.rc_public_subnet_ids[0]"
            private_subnet_ids_ref = "local.rc_private_subnet_ids"
            # An adopted VPC's internet gateway is not rc's to name. A declared
            # public subnet group there would need a route to it, so require
            # the caller to say which one.
            igw_id_ref = (
                f'"{ecs_cfg["internet_gateway_id"]}"'
                if ecs_cfg.get("internet_gateway_id")
                else "null"
            )
        else:
            vpc_id_ref = "aws_vpc.main.id"
            public_subnet_ids_ref = "aws_subnet.public[*].id"
            public_subnet_idx_ref = "aws_subnet.public[count.index].id"
            public_subnet_first_ref = "aws_subnet.public[0].id"
            private_subnet_ids_ref = "aws_subnet.private[*].id"
            igw_id_ref = "aws_internet_gateway.main.id"

        # rc-0cv: environment-wide default for services with no explicit
        # subnet_group. "public" (default) is exactly today's behavior,
        # byte-identical. "private" points every such service at the
        # already-resolved private subnets instead — no separate feature to
        # opt into, no risk of carving a duplicate subnet next to ones a
        # parent stack already provisioned.
        if default_subnet_placement == "private":
            default_placement_subnets_ref = private_subnet_ids_ref
            default_placement_assign_public_ip = False
        else:
            default_placement_subnets_ref = public_subnet_ids_ref
            default_placement_assign_public_ip = True

        # --- Declared network (rc.yml `network:` / `repositories:`) ---------
        #
        # Standalone, nameable primitives that are NOT derived from a service:
        # security groups, subnet groups with an explicit egress mode, VPC
        # endpoints, and ECR repositories. Their ids land in outputs.tf so an
        # out-of-band consumer (a backend calling run_task, say) can attach to
        # them without rc managing that consumer.
        #
        # Wholly additive: with no `network:` / `repositories:` block the plan
        # is empty, every template below renders nothing, and the emitted
        # terraform is byte-identical to before this existed.
        network_cfg, repositories_cfg = _parse_declared_network(ctx)
        if adopt_owned_alb:
            # network_plan._resolve_sources' "alb" ref today only knows how
            # to fan out over existing_alb's data-source security groups or
            # rc's own aws_security_group.alb — neither exists in
            # adopt_owned mode (literal foreign ids instead). Fail loud
            # rather than silently emit a dangling reference; not needed by
            # the browser-mgr forcing case, follow-up if it's ever needed.
            _alb_ref_users = [
                sg_name
                for sg_name, sg in network_cfg.security_groups.items()
                for rule in (*sg.ingress, *sg.egress)
                if rule.ref.kind == "alb"
            ]
            if _alb_ref_users:
                raise ProviderConfigError(
                    "provider_config.ecs.adopt_owned.alb does not yet "
                    "support network.security_groups rules that reference "
                    f"'alb' as a source (used by: {sorted(set(_alb_ref_users))}). "
                    "Remove the 'alb' ref or use existing_alb instead."
                )
        service_sg_refs = {
            name: [
                f"aws_security_group.rc_{tf_ident(sg)}.id"
                for sg in spec.security_groups
            ]
            for name, spec in ctx.services.items()
            if getattr(spec, "security_groups", None)
        }
        # Second validation pass. parse() could not resolve 'service:<name>'
        # refs or the bare 'alb' ref because a service may live only in
        # docker-compose.yml; here the merged set is finally known.
        from ...config._network_types import validate_network_refs

        validate_network_refs(
            network_cfg,
            service_names=set(ctx.services),
            service_sg_overrides={
                n: list(s.security_groups)
                for n, s in ctx.services.items()
                if getattr(s, "security_groups", None)
            },
            service_subnet_placements={
                n: s.subnet_group
                for n, s in ctx.services.items()
                if getattr(s, "subnet_group", None)
            },
            public_services={n: s.port for n, s in ctx.services.items() if s.public},
            has_alb=any(s.public for s in ctx.services.values())
            or existing_alb
            or adopt_owned_alb,
        )
        # rc-e5u.25.6: ec2_capacity.subnet_group is a provider_config.ecs.*
        # knob, not an rc.yml network: block field, so it lives outside
        # validate_network_refs (config/_network_types.py has no business
        # knowing about provider_config) — a parallel, consistently-styled
        # check instead, catching a typo'd name here rather than letting
        # net_plan.subnet_group() raise a bare KeyError once has_ec2_service
        # is known. Checked unconditionally: a bad name is a bug whether or
        # not any service actually resolves to EC2 launch_type.
        ec2_capacity_subnet_group = (ecs_cfg.get("ec2_capacity") or {}).get(
            "subnet_group"
        )
        if (
            ec2_capacity_subnet_group is not None
            and ec2_capacity_subnet_group not in network_cfg.subnets
        ):
            raise ProviderConfigError(
                f"provider_config.ecs.ec2_capacity.subnet_group: "
                f"{ec2_capacity_subnet_group!r} does not name a declared "
                f"network.subnets group (known: "
                f"{sorted(network_cfg.subnets) or 'none'})"
            )
        # Names that collide with resources rc already creates fail at apply,
        # not at validate, so catch them here where the service set is known.
        check_reserved_names(
            network_cfg, repositories_cfg, service_names=set(ctx.services)
        )
        net_plan = build_network_plan(
            network_cfg,
            repositories_cfg,
            existing_vpc=existing_vpc,
            existing_alb=existing_alb,
            vpc_cidr=vpc_cidr,
            service_sg_refs=service_sg_refs,
        )
        # A task in a NAT-free subnet that cannot reach ECR does not fail at
        # apply time — it fails minutes into the rollout with an opaque
        # CannotPullContainerError. Check it while we can still name the
        # missing endpoint.
        reachability_placements = [
            {
                "service": name,
                "subnet_group": getattr(spec, "subnet_group", None),
                "security_groups": list(getattr(spec, "security_groups", None) or []),
                # Secrets are attached to every service's task def (see the
                # secrets fan-out below), so any stack-level secret means
                # every task needs to reach Secrets Manager.
                "needs_secrets": bool(ctx.secrets)
                or bool(getattr(spec, "env_from_secret", None)),
            }
            for name, spec in ctx.services.items()
        ]
        # rc-e5u.25.6: the EC2 capacity ASG's container instances, when
        # placed via ec2_capacity.subnet_group, get their own synthetic
        # placement in this same check — a container INSTANCE has to reach
        # AWS services no Fargate task (or the task-shaped check above) needs
        # to, just to register with the cluster at all. `has_ec2_service`
        # isn't resolved until later in this method (it needs the per-service
        # loop below), so this uses a deliberately cheap, tolerant peek at
        # default_launch_type — the real, VALIDATED local of the same name is
        # computed further down; re-ordering that validation earlier would
        # change which error surfaces first for a config with two independent
        # problems, which existing tests may pin.
        _early_default_launch_type = ecs_cfg.get("default_launch_type", "FARGATE")
        any_ec2_service = any(
            (spec.launch_type or _early_default_launch_type) == "EC2"
            for spec in ctx.services.values()
        )
        if any_ec2_service and ec2_capacity_subnet_group:
            reachability_placements.append(
                {
                    "service": "ec2_capacity",
                    "subnet_group": ec2_capacity_subnet_group,
                    "security_groups": [],
                    "needs_secrets": False,
                    "is_ec2_capacity": True,
                }
            )
        check_endpoint_reachability(network_cfg, placements=reachability_placements)
        # --- Declared task roles (rc.yml `iam_roles:`) -----------------------
        #
        # Opt-in per-service least privilege. The shared aws_iam_role.task is
        # emitted exactly as before and stays the default; an empty block means
        # every service still points at it and iam.tf is byte-identical.
        #
        # The reference check runs at parse() time (iam_role can only be
        # written on an rc.yml service, so compose adds no referrers), but a
        # DeployContext can be built in code — the ECS e2e helpers and every
        # provider test do exactly that — so re-check against the merged set
        # rather than trusting that parse() ran.
        iam_roles_cfg = _parse_declared_iam_roles(ctx)
        from ...config._iam_types import validate_iam_role_refs

        validate_iam_role_refs(
            iam_roles_cfg,
            service_roles={
                n: getattr(s, "iam_role", None) for n, s in ctx.services.items()
            },
        )
        iam_plan = build_iam_plan(iam_roles_cfg)
        if existing_vpc and igw_id_ref == "null":
            needs_igw = [s.name for s in net_plan.subnet_groups if s.egress == "igw"]
            if needs_igw:
                raise ProviderConfigError(
                    f"network.subnets {needs_igw} declare public: true, but this "
                    f"stack adopts an existing VPC and rc does not know its "
                    f"internet gateway. Set "
                    f"provider_config.ecs.internet_gateway_id, or make the "
                    f"group private."
                )

        # ALB rendering aliases — like the vpc refs, templates read these so
        # they don't branch on existing_alb. In create mode they're the
        # original resource references (byte-identical output); in adopt mode
        # they point at the data sources emitted by alb.tf.j2.
        alb_security_groups_ref = "[aws_security_group.alb.id]"
        if existing_alb:
            alb_dns_ref = "data.aws_lb.main.dns_name"
            alb_zone_ref = "data.aws_lb.main.zone_id"
            https_listener_ref = "data.aws_lb_listener.https.arn"
            # tasks SG ingress: from the existing ALB's own security groups.
            tasks_alb_ingress_ref = "data.aws_lb.main.security_groups"
        elif adopt_owned_alb:
            # aws_lb.main is a real (imported) resource here too, so these
            # resolve identically to create-mode's syntax — only the
            # security-group source differs (literal foreign ids; rc
            # creates no aws_security_group.alb for an adopted ALB).
            alb_dns_ref = "aws_lb.main.dns_name"
            alb_zone_ref = "aws_lb.main.zone_id"
            https_listener_ref = "aws_lb_listener.https.arn"
            alb_security_groups_ref = (
                "["
                + ", ".join(f'"{sg}"' for sg in adopt_owned_alb_security_group_ids)
                + "]"
            )
            tasks_alb_ingress_ref = alb_security_groups_ref
        else:
            alb_dns_ref = "aws_lb.main.dns_name"
            alb_zone_ref = "aws_lb.main.zone_id"
            https_listener_ref = "aws_lb_listener.https.arn"
            tasks_alb_ingress_ref = "[aws_security_group.alb.id]"

        default_launch_type = ecs_cfg.get("default_launch_type", "FARGATE")
        if default_launch_type not in {"FARGATE", "EC2"}:
            raise ProviderConfigError(
                f"provider_config.ecs.default_launch_type must be FARGATE or EC2, "
                f"got {default_launch_type!r}"
            )

        # Shared-image dedup (rc-44i): which service owns each build group's
        # ECR repo. Computed once; consulted per-service below.
        image_owners = image_group_owners(
            ctx.services, share_repos=_ecs_cfg(ctx).get("share_image_repos", True)
        )

        services_view = []
        default_public = None
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
                    # rc-hbjb: name the EC2-side equivalent HERE. Without it
                    # the only way past this error is to delete the setting,
                    # which silently drops the service onto the AMI's 30 GiB
                    # root volume shared with every binpacked neighbour --
                    # the user learns about it when a task fills the disk and
                    # takes its neighbours down too.
                    raise ProviderConfigError(
                        f"service {name!r}: ephemeral_storage is only supported "
                        f"on FARGATE launch_type, got {launch_type!r}. It is a "
                        f"per-task Fargate field with no EC2 equivalent: an EC2 "
                        f"task's scratch space is the container instance's root "
                        f"volume, shared with every other task binpacked onto "
                        f"that instance. Size it with "
                        f"provider_config.ecs.ec2_capacity.root_volume_size "
                        f"(GiB, applies to the whole instance), or keep this "
                        f"service on FARGATE if it needs "
                        f"{spec.ephemeral_storage} GiB of its own."
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
                # rc-adopt: reference an EXISTING EFS + access point instead of
                # creating one — for adopt-in-place of a stateful stack whose
                # data already lives on EFS (don't orphan it). When efs_id is
                # set, rc emits no aws_efs_file_system / mount_target; when
                # access_point_id is set, no aws_efs_access_point — the task-def
                # mount references the existing ids verbatim.
                existing_fs_id = vol_entry.get("efs_id")
                existing_ap_id = vol_entry.get("access_point_id")
                if existing_ap_id and not existing_fs_id:
                    raise ProviderConfigError(
                        f"service {name!r} volume {vol_name!r}: access_point_id "
                        f"requires efs_id (the existing file system it belongs to)"
                    )
                # EFS IAM authorization on the mount. Default DISABLED (rc-
                # created EFS has no restrictive file-system policy). An ADOPTED
                # EFS often does (e.g. Copilot's CopilotEFSPolicy requires
                # iam:ResourceTag + ClientMount via IAM) — set efs_iam_auth:
                # true so the mount authenticates as the task role (which then
                # needs ClientMount perms + the policy's tag conditions).
                iam_auth = "ENABLED" if vol_entry.get("efs_iam_auth") else "DISABLED"
                vol_tf = _tf_name(vol_name)
                # rc-56bq.1: EFS automatic backups. Declare with
                # efs_backups: true on the volume.
                #
                # An EFS holding a database with no recovery point is a
                # data-loss trap, and a silent one -- nothing in `rc status` or
                # the console's EFS list says "this has no backups", because
                # aws_efs_file_system has no backup argument to be missing.
                # startsimpli-prod's postgres ran that way for months
                # (startsim-36qr), found only when someone tried to take a
                # backup before a risky migration. So this SHOULD be the
                # default.
                #
                # It is opt-in anyway, and the reason is not caution about the
                # feature -- it is that flipping it on by default breaks
                # deploys. aws_efs_backup_policy needs
                # elasticfilesystem:PutBackupPolicy, and nothing grants it:
                # bootstrap's deploy-role policy is derived from the rc.yml
                # `permissions` block (build.derive_statements), which has no
                # EFS key at all, and hand-made roles predate the resource
                # entirely -- startsimpli-prod-github-deploy has ZERO
                # elasticfilesystem actions today. Rendering the resource by
                # default would AccessDenied at terraform apply on every
                # existing stack's next deploy, simultaneously. Loud and
                # non-destructive, but still an estate-wide outage.
                #
                # rc-56bq.2 does it in the safe order: teach derive_statements
                # to grant the actions, patch the live roles, then flip.
                efs_backups = bool(vol_entry.get("efs_backups", False))
                efs_volumes.setdefault(
                    vol_name,
                    {
                        "name": vol_name,
                        "tf_name": vol_tf,
                        "existing_fs_id": existing_fs_id,
                        "efs_backups": bool(efs_backups),
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
                    "existing_fs_id": existing_fs_id,
                    "existing_ap_id": existing_ap_id,
                    "iam_auth": iam_auth,
                    # rc-kr7: shared-scratch EFS where each task writes its own
                    # subdir (e.g. per-session recording dirs) — safe with
                    # replicas>1. Default False = single-writer (postgres data).
                    "shared": bool(vol_entry.get("shared", False)),
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
                        # Dev source mounts are always rc-created (never an
                        # existing-EFS adopt) — but they share services.tf.j2's
                        # volume block, which reads these keys.
                        "existing_fs_id": None,
                        "existing_ap_id": None,
                        "iam_auth": "DISABLED",
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
            stateful = _is_stateful_service(name, spec, mount_count=len(svc_mounts))
            # rc-6akx: rollout percentages. Defaults reproduce the literals
            # services.tf.j2 used to hardcode (100/200, or 0/100 stateful);
            # an rc.yml `deployment:` block overrides them, and the same
            # resolved numbers feed the ASG sizer below.
            deployment = _deployment_percents(name, spec, mount_count=len(svc_mounts))
            if (
                deployment.overridden
                and deployment.maximum <= 100
                and launch_type != "EC2"
            ):
                # ECS refuses availability_zone_rebalancing alongside
                # maximumPercent <= 100, so services.tf.j2 has to pin it OFF
                # for this service. On EC2 that costs nothing (binpack already
                # made the service ineligible, and rc-5a4g pins it there
                # anyway), but on Fargate rc deliberately leaves rebalancing
                # ON. Say so out loud: the alternative is a user discovering
                # it in generated terraform, or not at all. Also worth
                # knowing that the fleet-size argument for the knob does not
                # apply here — Fargate bills per task and has no ASG to
                # shrink, so this only caps concurrent tasks during the roll.
                self._warn(
                    f"service {name!r}: deployment.maximum_percent="
                    f"{deployment.maximum} on a FARGATE service turns AZ "
                    f"rebalancing OFF for it — ECS rejects "
                    f"availability_zone_rebalancing with maximumPercent <= "
                    f"100, so rc must pin it DISABLED. Fargate has no ASG to "
                    f"size down, so the usual reason for this knob (a fleet "
                    f"held at 2x steady state to cover the roll) does not "
                    f"apply; keep it if you are capping concurrent tasks "
                    f"during a roll on purpose, otherwise drop the block."
                )
            # rc-kr7: a single-writer EFS volume (postgres data, sqlite) is one
            # access point; replicas>1 runs concurrent tasks against the same
            # dir and corrupts it. min_healthy=0 only protects the ROLL window —
            # it can't make N steady-state tasks safe. Reject unless EVERY volume
            # is marked shared:true (a multi-writer scratch space where each task
            # writes its own subdir, e.g. per-session recording dirs).
            unshared_mounts = [m for m in svc_mounts if not m.get("shared")]
            if unshared_mounts and (spec.replicas or 1) > 1:
                names = ", ".join(m["volume"] for m in unshared_mounts)
                raise ProviderConfigError(
                    f"service {name!r}: replicas={spec.replicas} with EFS "
                    f"volume(s) [{names}] — two or more tasks mounting the same "
                    f"access point corrupt single-writer data. Use replicas=1, "
                    f"give each replica its own volume, or set shared:true on the "
                    f"volume if it's a multi-writer scratch space (per-task subdirs)."
                )
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
                "health_check": spec.health_check,
                "health_check_grace_period": effective_grace,
                "launch_type": launch_type,
                # rc-m2sn: containerDefinitions.essential. Default True — ECS's
                # own default and what the template hardcoded before groups,
                # so no existing task definition changes. Only load-bearing
                # inside a MULTI-container task, where false means this
                # container exiting leaves the task running without it (and
                # never restarts it — see the ServiceSpec docstring).
                "essential": bool(getattr(spec, "essential", True)),
                "mounts": svc_mounts,
                "stateful": stateful,
                "deployment_min_healthy_percent": deployment.minimum_healthy,
                "deployment_max_percent": deployment.maximum,
                "deployment_overridden": deployment.overridden,
                "env": dict(spec.env or {}),
                "command": list(spec.command or []),
                # Pre-built image, from compose `image:` or from rc.yml
                # `services.<svc>.image` (rc.yml wins and clears the build
                # context). If set, the task def uses it verbatim instead of
                # an ECR placeholder. Named compose_image for its original
                # source; the field is the same either way.
                #
                # Both None would mean services.tf.j2 emits an ECR tag rc
                # never pushes. No validated config reaches that state any
                # more: startsim-wxb7 made a missing or service-less
                # compose_file an error, and rc-2r1r closed the last route —
                # an rc.yml service that compose doesn't define must now
                # declare its own image. The template's final else branch
                # survives only as a backstop for ServiceSpecs built directly
                # in code, bypassing build_deploy_context.
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
            svc_view.update(
                _service_placement_view(
                    spec,
                    net_plan=net_plan,
                    default_subnets_ref=default_placement_subnets_ref,
                    extra_security_group_ids=extra_security_group_ids,
                    default_assign_public_ip=default_placement_assign_public_ip,
                )
            )
            svc_view.update(_service_role_view(spec, iam_plan=iam_plan))
            services_view.append(svc_view)

        # ---- Task groups (rc-ib01) -------------------------------------
        # One aws_ecs_task_definition + one aws_ecs_service per GROUP. With no
        # task_groups block every service is an implicit group of one named
        # after itself, so groups_view is services_view in the same order and
        # the rendered terraform is unchanged.
        #
        # Structure was validated at parse time; the rejects below need the
        # MERGED service set (compose union rc.yml), which only exists here.
        try:
            validate_task_groups(ctx.task_groups or {}, ctx.services)
        except ConfigError as exc:
            raise ProviderConfigError(str(exc)) from exc
        resolved_groups = resolve_task_groups(ctx.task_groups or {}, ctx.services)
        groups_view = _task_group_views(resolved_groups, services_view)

        # Everything ALB-facing keys off GROUPS from here down: a target group
        # attaches to an ECS service, and after grouping the ECS service is the
        # group's, not the member's. For an ungrouped stack these iterate the
        # same list in the same order as before.
        #
        # groups_view is sorted by name, so default_public is the
        # alphabetically-first public+port group — a silent, surprising choice
        # when several are public (e.g. celery-flower sorts before nginx and
        # would wrongly become the catch-all). When one explicitly sets
        # default_target=true, honor it: it wins regardless of name order.
        # First flagged wins if more than one is set.
        default_public = next(
            (g for g in groups_view if g.get("public") and g.get("port")),
            None,
        )
        default_target_view = next(
            (g for g in groups_view if g.get("default_target") and g.get("port")),
            None,
        )
        if default_target_view is not None:
            default_public = default_target_view

        # rc-8xvk: the ASG is sized from GROUPS, not from compose services.
        # auto_size's ENI dimension counts one branch ENI per EC2TaskDemand, so
        # this — not the template change — is what converts grouping into fewer
        # instances. Memory is the group's (the sum of its members unless
        # rc.yml overrides), and deployment_maximum_percent is the group's, so
        # rc-anl6's rolling-deploy headroom is modeled per TASK, which is the
        # thing that actually holds an ENI mid-roll.
        ec2_demands = [
            EC2TaskDemand(
                name=g["name"],
                # startsim-u88y: 0, not the group's cpu. services.tf.j2 omits
                # task-level cpu on EC2, so the task reserves none — sizing the
                # ASG for a reservation nobody makes just moves the same 95%
                # regression from the task definition into the instance count.
                cpu_units=0,
                memory_mib=g["memory"],
                replicas=g["replicas"],
                deployment_maximum_percent=g["deployment_max_percent"],
            )
            for g in groups_view
            if g["launch_type"] == "EC2"
        ]

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
            [g for g in groups_view if g.get("domain")],
            key=lambda g: g["domain"],
        )
        # Aliases attach to public services as extra hostnames. They feed
        # into the cert SAN list + R53 records but do NOT generate listener
        # rules — the default action catches traffic for them.
        alias_hostnames: list[str] = []
        for gv in groups_view:
            for a in gv.get("aliases", []) or []:
                alias_hostnames.append(a)
        # Listener rules need distinct priorities. Step by 10 so users can
        # hand-write rules in between later. The base is 100 unless this project
        # shares an adopted listener with others, in which case each project gets
        # its own band via existing_alb.listener_rule_priority_base (rc-4tkc.2).
        for i, dsvc in enumerate(domained_services):
            dsvc["listener_rule_priority"] = listener_rule_priority_base + i * 10
        has_domained_services = len(domained_services) > 0
        has_ec2_service = len(ec2_demands) > 0
        # An adopted ALB keeps its own (rc-unmanaged) default listener action,
        # so rc can only add host-based rules — every public service must
        # declare a domain. EC2 capacity's SG admits ALB traffic via
        # tasks_alb_ingress_ref (same source the tasks SG already uses),
        # which resolves to the adopted ALB's own security groups here —
        # no rc-created aws_security_group.alb needed.
        if existing_alb:
            public_without_domain = [
                g["name"]
                for g in groups_view
                if g.get("public") and not g.get("domain")
            ]
            if public_without_domain:
                raise ProviderConfigError(
                    "provider_config.ecs.existing_alb requires every public "
                    "service to set 'domain' (host-based routing onto the "
                    f"existing listener); missing on: {public_without_domain}"
                )
        # adopt_owned.alb DOES own the listener (a real, imported resource),
        # so unlike existing_alb it can set its own default_action — no
        # domain-per-service restriction needed.
        if adopt_owned_alb:
            # Mirrors _resolve_domain's all_domains truthiness check —
            # domain_info itself isn't computed until later, but every
            # input it depends on is already known here.
            _will_have_domain = bool(
                domained_services
                or alias_hostnames
                or ecs_cfg.get("domain")
                or (ctx.rc_yml_v2 or {}).get("domain")
            )
            if _will_have_domain and not adopt_owned_alb_https_listener_arn:
                raise ProviderConfigError(
                    "provider_config.ecs.adopt_owned.alb requires "
                    "'https_listener_arn' when any service declares a "
                    "domain (TLS terminates on the adopted ALB's HTTPS "
                    "listener, which rc must own to add per-service rules)"
                )
        # has_efs drives the EFS template (security group, file system,
        # mount targets, access points). True for either persistent OR
        # dev-mode source mounts since both need the same EFS plumbing.
        has_efs = len(efs_volumes) > 0 or dev_efs_volume is not None
        has_dev_efs = dev_efs_volume is not None
        # has_created_efs gates the rc-managed EFS resources (security group,
        # file systems, mount targets). False when EVERY persistent volume
        # references an EXISTING EFS (adopt-in-place: efs_id set on all) — then
        # rc creates no EFS at all and only emits task-def mounts of the
        # existing ids. A dev-mode source mount always needs rc-created EFS.
        has_created_efs = (
            any(not v.get("existing_fs_id") for v in efs_volumes.values())
            or dev_efs_volume is not None
        )
        # Service discovery is cheap (one Cloud Map namespace + one entry per
        # TASK GROUP) and turns multi-service compose into ECS that actually
        # talks to itself. Enable whenever there is more than one group.
        #
        # Counts groups, not services: ECS allows exactly one service registry
        # per service ("Multiple service registries for each service isn't
        # supported"), so a group registers ONE name, and collapsing every
        # service into a single group would otherwise emit a namespace plus a
        # record nobody resolves. Identical to the old service count for any
        # stack without task_groups.
        has_service_discovery = len(groups_view) > 1

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

        # rc-7yo: per-service env from an EXISTING SM secret. Build the per-key
        # secrets[] entries keyed by service name; collect ARNs for the
        # task-exec GetSecretValue grant. Keys are explicit (offline emit).
        env_from_secret_by_svc: dict[str, list[dict[str, Any]]] = {}
        env_from_secret_arns: list[str] = []
        for svc_name, spec in ctx.services.items():
            entries: list[dict[str, Any]] = []
            for ref in getattr(spec, "env_from_secret", []) or []:
                arn = ref.get("arn")
                keys = ref.get("keys") or []
                if not arn or not keys:
                    raise ProviderConfigError(
                        f"service {svc_name!r}: env_from_secret entry requires "
                        "'arn' and a non-empty 'keys' list"
                    )
                if arn not in env_from_secret_arns:
                    env_from_secret_arns.append(arn)
                for key in keys:
                    entries.append(
                        {
                            "env_name": key,
                            "value_from_ref": f'"{arn}:{key}::"',
                        }
                    )
            if entries:
                env_from_secret_by_svc[svc_name] = entries

        has_secrets = len(secrets_view) > 0 or bool(env_from_secret_arns)
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
        all_secret_arns.extend(env_from_secret_arns)

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
            # rc-7yo: append this service's env_from_secret keys (per-service,
            # not broadcast). Plaintext env wins on collision; skip keys already
            # wired (ECS rejects duplicate secret names).
            existing_names = {s["env_name"] for s in filtered}
            for e in env_from_secret_by_svc.get(svc_name, []):
                if e["env_name"] in override_keys or e["env_name"] in existing_names:
                    continue
                existing_names.add(e["env_name"])
                filtered.append(e)
            svc_view["secrets"] = filtered
        default_target_port = default_public["port"] if default_public else 80
        default_health_check_path = (default_public or {}).get(
            "health_check_path"
        ) or "/"

        ec2_capacity_cfg = (
            self._resolve_ec2_capacity(
                ecs_cfg, ec2_demands, eni_trunking=getattr(ctx, "eni_trunking", None)
            )
            if has_ec2_service
            else None
        )
        if ec2_capacity_cfg is not None:
            # rc-e5u.25.5 gave the ASG's instances the same placement a
            # Fargate task ENI gets when its service declares no explicit
            # subnet_group -- default_placement_subnets_ref /
            # default_placement_assign_public_ip (rc-0cv's
            # default_subnet_placement, "public" unless overridden).
            # rc-e5u.25.6 adds an opt-in on top: ec2_capacity.subnet_group
            # points the ASG at a DECLARED network.subnets group instead,
            # through the exact same resolver a service's own subnet_group:
            # uses (_resolve_subnet_group_placement) -- see its docstring.
            # Absent the knob (the common case), this is byte-identical to
            # rc-e5u.25.5: subnet_group_name is None, so it falls straight
            # through to the default_placement_* values above.
            ec2_placement = _resolve_subnet_group_placement(
                ec2_capacity_subnet_group,
                net_plan=net_plan,
                default_subnets_ref=default_placement_subnets_ref,
                default_assign_public_ip=default_placement_assign_public_ip,
                where="provider_config.ecs.ec2_capacity.subnet_group",
            )
            ec2_capacity_cfg["subnets_ref"] = ec2_placement["subnets_ref"]
            ec2_capacity_cfg["assign_public_ip"] = ec2_placement["assign_public_ip"]
            self._warn_on_ec2_one_off_capacity(
                ctx, ec2_capacity_cfg, ec2_demands, default_launch_type
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
            "existing_alb": existing_alb,
            "adopt_owned_alb": adopt_owned_alb,
            "alb_security_groups_ref": alb_security_groups_ref,
            "alb_makes_own_sg": not existing_alb and not adopt_owned_alb,
            "existing_alb_arn": existing_alb_arn,
            "existing_alb_https_listener_arn": existing_alb_https_listener_arn,
            "existing_cloud_map_namespace": existing_cloud_map_namespace,
            "service_discovery_namespace_ref": service_discovery_namespace_ref,
            "has_task_iam": has_task_iam,
            "task_iam_managed": task_iam_managed,
            "task_iam_statements": task_iam_statements,
            "task_iam_policy_json": task_iam_policy_json,
            "task_role_tags": task_role_tags,
            # Declared network (rc.yml `network:` / `repositories:`). Empty
            # plan => network_declared.tf / repositories.tf render to nothing
            # and outputs.tf emits only its historical entries.
            "net_plan": net_plan,
            # Declared task roles (rc.yml `iam_roles:`). Empty plan => iam.tf
            # emits only the shared task role it always has, and every service
            # keeps task_role_arn = aws_iam_role.task.arn.
            "iam_plan": iam_plan,
            "igw_id_ref": igw_id_ref,
            "public_subnet_first_ref": public_subnet_first_ref,
            "alb_dns_ref": alb_dns_ref,
            "alb_zone_ref": alb_zone_ref,
            "https_listener_ref": https_listener_ref,
            "tasks_alb_ingress_ref": tasks_alb_ingress_ref,
            "cluster_name": cluster_name,
            "aws_profile": aws_profile,
            "environment": environment,
            # When set, providers.tf default_tags adds Ephemeral=true +
            # ExpiresAt=<iso>. Drives `rc reap` discovery and any
            # out-of-band tag-scan reaper.
            "expires_at": ctx.expires_at,
            "services": services_view,
            # rc-ib01: one entry per ECS task. services.tf.j2 and
            # service_discovery.tf.j2 iterate THIS; outputs.tf.j2 still
            # iterates `services` because ECR repos are per-service, not
            # per-task.
            "groups": groups_view,
            "has_public_service": has_public_service,
            "has_build_context_service": has_build_context_service,
            "has_ec2_service": has_ec2_service,
            # startsim-wyn2: adopting a shared cluster suppresses everything
            # CLUSTER-scoped — the cluster itself, its capacity-provider
            # association, and the whole ASG/launch-template/instance-role
            # stack — because a shared stack owns them. A tenant that also
            # created an ASG would add its own box back and defeat the point.
            "existing_cluster": existing_cluster,
            # rc-py32/#67 follow-up: the ONE spelling of "how do I refer to the
            # cluster". Adoption turns the cluster from a managed resource into a
            # data source, and every template that names it has to follow. Both
            # earlier misses in this feature were the same shape -- a consumer
            # left behind when the producer changed -- so this is a single value
            # rather than the conditional repeated per template.
            "cluster_ref": (
                "data.aws_ecs_cluster.main"
                if existing_cluster
                else "aws_ecs_cluster.main"
            ),
            # The ATTRIBUTES differ too, not just the address. The managed
            # resource exports `.name`; the data source takes `cluster_name` as
            # its argument and exports no `.name` at all, so swapping only the
            # prefix renders `data.aws_ecs_cluster.main.name`, which terraform
            # rejects with "Unsupported attribute". Both spellings live here so a
            # template never has to know which side it is on.
            "cluster_name_expr": (
                "data.aws_ecs_cluster.main.cluster_name"
                if existing_cluster
                else "aws_ecs_cluster.main.name"
            ),
            "cluster_arn_expr": (
                "data.aws_ecs_cluster.main.arn"
                if existing_cluster
                else "aws_ecs_cluster.main.arn"
            ),
            # aws_ecs_service.cluster takes an ARN or a name; the data source has
            # no `.id`, so adoption passes the arn.
            "cluster_id_expr": (
                "data.aws_ecs_cluster.main.arn"
                if existing_cluster
                else "aws_ecs_cluster.main.id"
            ),
            "shared_capacity_provider": shared_capacity_provider,
            "service_name_prefix": service_name_prefix,
            "has_service_discovery": has_service_discovery,
            "ec2_capacity": ec2_capacity_cfg,
            "has_efs": has_efs,
            "has_created_efs": has_created_efs,
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
            # Opt-in (provider_config.ecs.ignore_task_definition_changes):
            # emit `lifecycle { ignore_changes = [container_definitions] }` on
            # every task def so terraform stops fighting container defs that
            # are owned out-of-band — e.g. adopted / `rc deploy --no-state`
            # stacks whose secrets are wired on by a reconcile script, or
            # whose images are force-rolled outside terraform. Default false:
            # normal stateful stacks keep terraform managing container defs
            # (that's how a stateful deploy ships a new image).
            "ignore_task_definition_changes": bool(
                ecs_cfg.get("ignore_task_definition_changes", False)
            ),
            # Opt-in (provider_config.ecs.container_insights). ECS Container
            # Insights ships per-task/-service metrics to CloudWatch — real
            # per-cluster ingestion cost that is rarely worth it. Default
            # OFF: the cluster setting is emitted as "disabled" and the
            # /aws/ecs/containerinsights/<cluster>/performance log group is
            # not managed (AWS never creates it when insights is off). Set
            # true only for a cluster you actually want the metrics on.
            "container_insights": bool(ecs_cfg.get("container_insights", False)),
            # provider_config.ecs.idle_timeout — the ALB's connection idle timeout
            # (seconds). AWS's default is 60; long-lived connections (WebSockets,
            # SSE, streaming responses) need it raised or the LB silently drops the
            # socket. Emitted explicitly on aws_lb.main so the value is TRACKED in
            # rc.yml instead of set out-of-band on the live LB (which then shows as
            # perpetual drift on every plan). Default 60 == AWS default, so a stack
            # that doesn't set it sees no change.
            "alb_idle_timeout": int(ecs_cfg.get("idle_timeout", 60)),
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
        # Drop a .gitignore so terraform's volatile artifacts (provider cache,
        # state, plan files) are never accidentally committed — rc regenerates
        # the .tf each run, but .terraform/ + *.tfstate must stay out of git.
        # Not added to the revision-id hash set (constant content). Only write
        # if absent so a project that hand-tunes it keeps its version.
        gitignore = out_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_TF_GITIGNORE)
        return out_dir

    # -----------------------------------------------------------------
    # Plan / Deploy / Destroy / Redeploy
    # -----------------------------------------------------------------

    def _preflight_aws_profile(self, ctx: DeployContext, ecs_cfg: dict) -> None:
        """Resolve provider_config.ecs.aws_profile before anything uses it.

        Sets ``ctx.omit_aws_profile`` so emit_terraform() renders the right
        provider block. Raises when the profile is definitively absent AND
        nothing else can supply credentials — that deploy cannot succeed, and
        failing here costs the user a second instead of a full apply cycle
        ending in terraform's "failed to get shared config profile".

        Skipped entirely on a --no-state deploy: that path never renders a
        terraform provider block, and its boto3 session already falls back to
        the ambient chain (see _default_session_factory).
        """
        if getattr(ctx, "skip_terraform", False):
            return
        profile = ecs_cfg.get("aws_profile")
        omit, message = check_aws_profile_for_terraform(profile)
        if omit:
            ctx.omit_aws_profile = True
            self._warn(message)
            return
        if _aws_profile_status(profile) == PROFILE_ABSENT:
            raise ProviderConfigError(
                f"provider_config.ecs.aws_profile is {profile!r}, but no such "
                f"profile exists in "
                f"{' or '.join(_aws_config_search_paths())}, and this "
                f"environment carries no ambient AWS credentials "
                f"({'/'.join(_AMBIENT_CREDENTIAL_ENV_VARS)} are all unset). "
                f'terraform would fail the apply with "failed to get shared '
                f'config profile, {profile}". Create the profile '
                f"(`aws configure --profile {profile}`), point aws_profile at "
                f"one that exists, or drop it and supply credentials in the "
                f"environment."
            )

    def _preflight_eni_trunking(self, ctx: DeployContext, ecs_cfg: dict) -> None:
        """Resolve whether awsvpcTrunking is on before anything sizes a fleet.

        rc-hguq. Without trunking an awsvpc task costs one whole ENI, and ENI
        counts are FLAT across the useful part of the m5 range -- m5.2xlarge
        is twice the box of an m5.xlarge with the same 3 task slots. A fleet
        sized against that ceiling exists to satisfy a networking artifact
        rather than a workload: 11 right-sized tasks needing 4.5 vCPU end up
        on 28 vCPU of instances.

        ``ec2_capacity.eni_trunking`` is the explicit override (true/false).
        Left unset ("auto"), rc asks ECS -- ``ListAccountSettings`` with
        effectiveSettings, which is what actually governs the behaviour.

        A failed lookup leaves ``ctx.eni_trunking`` as None, which every
        message downstream renders as "rc has not checked", never as
        "trunking is not enabled". That distinction IS the bug this fixes.
        """
        declared = (ecs_cfg.get("ec2_capacity") or {}).get("eni_trunking", "auto")
        if isinstance(declared, bool):
            ctx.eni_trunking = declared
            return
        if str(declared).lower() not in ("auto", "none", ""):
            raise ProviderConfigError(
                f"ec2_capacity.eni_trunking must be true, false, or 'auto', "
                f"got {declared!r}"
            )
        # Nothing to resolve for an all-Fargate stack -- and no reason to
        # spend an AWS call on one.
        default_launch_type = ecs_cfg.get("default_launch_type", "FARGATE")
        if not any(
            (spec.launch_type or default_launch_type) == "EC2"
            for spec in (ctx.services or {}).values()
        ):
            return
        try:
            client = self.session_factory(ctx).client(
                "ecs", region_name=ecs_cfg.get("region")
            )
            settings = client.list_account_settings(
                name="awsvpcTrunking", effectiveSettings=True
            )
        except Exception as exc:  # noqa: BLE001 — a failed probe is not a finding
            self._warn(
                f"could not read the account's awsvpcTrunking setting "
                f"({type(exc).__name__}: {exc}). rc is sizing EC2 capacity as "
                f"though ENI trunking were off, which may over-provision "
                f"badly. Grant ecs:ListAccountSettings, or set "
                f"provider_config.ecs.ec2_capacity.eni_trunking explicitly."
            )
            return
        for setting in settings.get("settings") or []:
            if setting.get("name") == "awsvpcTrunking":
                ctx.eni_trunking = str(setting.get("value", "")).lower() == "enabled"
                return
        # ECS returned no row for it: the setting has never been set, which
        # means disabled. That IS a finding, unlike a failed call.
        ctx.eni_trunking = False

    def preflight(self, ctx: DeployContext) -> None:
        """AWS pre-flight run at the head of plan() and deploy().

        Two checks today:

        * aws_profile resolution (rc-rigk) — decides whether the rendered
          terraform provider should pin the configured profile, omit it in
          favour of ambient credentials, or fail here with a message that
          names the profile instead of letting terraform fail mid-apply with
          its own opaque wording.
        * adopted-VPC ids (rc-a57) — no-op unless provider_config.ecs.vpc_id
          is set; otherwise verifies the VPC + subnets against live AWS so a
          bad id fails as a clear rc error, not a terraform stack trace.

        Deliberately ordered cheapest-and-most-local first: the profile check
        makes no AWS calls, and a stack whose credentials can't resolve
        cannot answer the VPC lookup anyway.
        """
        ecs_cfg = _ecs_cfg(ctx)
        self._preflight_aws_profile(ctx, ecs_cfg)
        self._preflight_eni_trunking(ctx, ecs_cfg)
        if not ecs_cfg.get("vpc_id"):
            return
        session = self.session_factory(ctx)
        ec2 = session.client("ec2", region_name=ecs_cfg.get("region"))
        preflight_existing_vpc(ecs_cfg, ec2)

    def plan(self, ctx: DeployContext) -> PlanResult:
        # Fresh sink per run: a provider instance is reused across
        # plan-then-deploy in `rc up`, and stale findings from the previous
        # pass would be reported as if they belonged to this one. Reset HERE
        # rather than in emit_terraform() — preflight() raises findings of
        # its own and runs first, so a reset inside the render would wipe
        # them before anyone read them.
        self._warnings = []
        self.preflight(ctx)
        out_dir = self._tf_dir(ctx)
        self.emit_terraform(ctx, out_dir)
        self.deploy_preflight(ctx, out_dir)
        runner = self.runner_factory(out_dir)
        runner.init()
        # rc-avcr: on an ignore_task_definition_changes stack, save the plan
        # so it can be inspected for FORCED task-definition replacements
        # (see _warn_on_task_def_replacement). Every other stack plans
        # exactly as before — no plan file, no extra terraform round trip.
        plan_file = (
            self._preapply_plan_path(out_dir)
            if self._needs_preapply_plan(ctx)
            else None
        )
        summary = runner.plan(out_file=plan_file)
        if plan_file is not None:
            self._inspect_plan(runner, plan_file)
        # Compose-file detectors (rc-e5u.44.6/.7/.8/.9) flag silently-
        # dropped bind mounts, ephemeral data, dev-only DNS, and
        # unreachable secondary ports. Run here so any caller of
        # provider.plan(ctx) — not only the CLI dispatcher — gets them.
        from ...compose_warnings import collect_compose_warnings

        warnings = self._drain_warnings()
        for w in collect_compose_warnings(ctx.compose_path, ctx.rc_yml_v2):
            if w not in warnings:
                warnings.append(w)
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

        self._warnings = []  # see plan() — reset before preflight, not in emit
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
        # Plan-time findings raised during the render (rc-anl6 sizing,
        # rc-hbjb root volume, rc-rigk profile). Surfaced HERE, before
        # terraform touches anything, rather than only in the returned
        # DeployResult -- a warning the user reads after the apply has
        # already run is a post-mortem, not a warning.
        warnings: list[str] = self._drain_warnings()
        for _w in warnings:
            self._emit(f"  warning: {_w}")
        self.deploy_preflight(ctx, out_dir)
        # rc-ysh: detect held local state lock BEFORE invoking terraform so
        # we surface the holder PID in <1s instead of inheriting terraform's
        # subprocess-output buffering and retry loops.
        self._check_local_state_lock(out_dir, ctx)
        runner = self.runner_factory(out_dir)
        runner.init()
        self._reconcile_orphan_log_groups(ctx, runner)
        self._reconcile_orphan_backup_bucket(ctx, runner)
        self._reconcile_adopt_owned_alb(ctx, runner)
        # rc-avcr: an ignore_task_definition_changes stack gets an explicit
        # plan before the apply, so a FORCED task-definition replacement is
        # reported while it can still be aborted. Deliberately AFTER the
        # reconcile-import steps above (they mutate state, so a plan taken
        # before them would describe a different apply) and reused as the
        # apply's input, which makes the warning describe exactly what runs
        # and costs no extra terraform cycle — apply would plan anyway.
        #
        # Reusing the plan file DOES change one failure mode, deliberately:
        # `terraform apply <saved-plan>` refuses a plan whose state moved
        # underneath it ("Saved plan is stale"), where a bare apply would
        # silently replan and proceed. That only happens when something else
        # wrote the state between these two calls — i.e. a concurrent apply —
        # and failing is the right answer there. If you land here, rc
        # introduced the strictness on purpose: re-run the deploy.
        plan_file = (
            self._preapply_plan_path(out_dir)
            if self._needs_preapply_plan(ctx)
            else None
        )
        if plan_file is not None:
            runner.plan(out_file=plan_file)
            for message in self._inspect_plan(runner, plan_file):
                warnings.append(message)
                self._emit(f"  warning: {message}")
        runner.apply(plan_file=plan_file)
        outputs = runner.output()

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
            # rc-wji.1: roll every member of each pushed image group, not just
            # the owner _build_and_push_images returned — siblings share the
            # image and must pick up the new :latest too (parity with the
            # --no-state path).
            self._force_new_deployments(
                ctx,
                roll_targets_for_pushed(
                    ctx.services,
                    pushed,
                    share_repos=_ecs_cfg(ctx).get("share_image_repos", True),
                ),
            )

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

        # --no-state never runs emit_terraform, so it can never create the
        # EC2 capacity provider / ASG / launch template capacity.tf.j2 emits
        # -- it only force-rolls (update_service) whatever ECS already has
        # live. A service declared launch_type: EC2 here is not silently
        # deployed as Fargate (no-state never touches launch type at all);
        # it runs however AWS already has it -- which is correct and
        # expected when that service's EC2 capacity was provisioned earlier
        # (a prior terraform apply, Copilot, CloudFormation, ...). Warn
        # rather than block: raising here would break every already-working
        # --no-state deploy of a service that legitimately runs on EC2.
        ec2_services = sorted(
            n for n, s in ctx.services.items() if s.launch_type == "EC2"
        )
        if ec2_services:
            msg = (
                f"launch_type: EC2 is declared on {ec2_services}, but this "
                "is a --no-state deploy: rc never runs emit_terraform here, "
                "so it did not provision (and cannot verify) EC2 capacity "
                "for them. They run however ECS already has them live; if "
                "that capacity was never provisioned, tasks will sit "
                "PENDING. Run `rc adopt` to bring this stack under "
                "terraform management, then `rc plan` and READ THE PLAN "
                "before applying: any live resource rc adopt could not "
                "import (no resolver, or an unrecognized name) shows up as "
                "a create, which for a named security group or EFS file "
                "system means a duplicate, not an adoption -- it is not "
                "automatically safe. Once the plan looks right, deploy "
                "without --no-state so rc creates/manages EC2 capacity "
                "for these services."
            )
            self._emit(f"  WARN: {msg}")
            warnings.append(msg)

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
        # Image-group siblings share ONE image in ONE repo (the owner's).
        # When the owner's own repo name doesn't exist — e.g. the repo was
        # created under a DIFFERENT group owner at cutover (rc.yml service
        # order changed since), so the live repo is named after a sibling —
        # fall back to a sibling's existing repo so the shared image still
        # rebuilds + pushes to the repo the task defs actually reference.
        owners_map = image_group_owners(
            ctx.services, share_repos=_ecs_cfg(ctx).get("share_image_repos", True)
        )
        group_members: dict[str, list[str]] = {}
        for member, owner in owners_map.items():
            group_members.setdefault(owner, []).append(member)

        def _repo_candidates(name: str) -> list[str]:
            cands = [f"{ctx.project}/{name}", f"{ctx.project}-{name}"]
            if cluster_prefix and cluster_prefix != ctx.project:
                cands.append(f"{cluster_prefix}/{name}")
                cands.append(f"{cluster_prefix}-{name}")
            return cands

        wanted_repos: dict[str, list[str]] = {}
        for svc_name, spec in ctx.services.items():
            if not spec.build_context:
                continue
            # Try this service's own repo names first, then its image-group
            # siblings' (so an owner can adopt a sibling-named repo).
            owner = owners_map.get(svc_name, svc_name)
            siblings = [m for m in group_members.get(owner, []) if m != svc_name]
            candidates = _repo_candidates(svc_name)
            for sib in siblings:
                candidates.extend(_repo_candidates(sib))
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
        if pushed and not getattr(ctx, "skip_force_roll", False):
            # rc-8j7.8: --no-roll (skip_force_roll) MUST suppress the roll here
            # too, exactly like the terraform path (see deploy() above). Without
            # this guard a `deploy --no-state --no-roll` still force-rolls and
            # blocks on _wait_for_services_stable (~5min) — the CI split builds
            # with --no-roll, reconciles secrets + migrates, THEN rolls in a
            # later step, so a roll here is a duplicate that also fires BEFORE
            # migrations run. This was the phantom ~5min "build step", not the
            # CodeBuild log drain.
            #
            # Only the image-group OWNER is built+pushed, but EVERY sibling
            # that references that shared image must be force-rolled too —
            # otherwise siblings keep running the old image while the new
            # :latest sits unused (the django/celery-* staleness bug). Expand
            # the pushed owners to all members of their groups (rc-wji.1:
            # shared with deploy() so the two roll paths can't drift).
            # reconcile_scale (rc-wji.2): terraform is bypassed here, so apply
            # rc.yml replicas to desiredCount as part of the roll.
            self._force_new_deployments(
                ctx,
                roll_targets_for_pushed(
                    ctx.services,
                    pushed,
                    share_repos=_ecs_cfg(ctx).get("share_image_repos", True),
                ),
                reconcile_scale=True,
            )

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
        to_build = _services_to_build(
            ctx.services,
            services_filter,
            share_repos=_ecs_cfg(ctx).get("share_image_repos", True),
        )
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

        # Shared BuildKit cache repo (rc-e5u.45.2). Managed stacks get it as a
        # terraform output. Adopted / `--no-state` stacks never run terraform,
        # so there's no output and every deploy rebuilds the (heavy pip/apt)
        # layers cold. For those, rc derives a sibling cache repo from the
        # project's own ECR registry and ensures it exists (see below) — same
        # layer caching with zero extra config. Resolution order:
        #   1. terraform `buildcache_repository` output (managed stacks)
        #   2. RC_BUILDCACHE_REPO env (explicit operator override)
        #   3. derived <registry>/<project>/buildcache (adopted/--no-state)
        # `buildcache_managed` tracks whether the repo already exists (cases 1
        # and 2 are caller-owned; case 3 rc must create). RC_DISABLE_BUILDCACHE
        # (handled in ImageBuilder) still opts out of caching entirely.
        buildcache_repo = (outputs.get("buildcache_repository") or {}).get("value")
        buildcache_managed = bool(buildcache_repo)
        if not buildcache_repo:
            buildcache_repo = os.environ.get("RC_BUILDCACHE_REPO") or None
            buildcache_managed = bool(buildcache_repo)
        if not buildcache_repo:
            registry_host = next(iter(repos.values())).split("/", 1)[0]
            buildcache_repo = f"{registry_host}/{ctx.project}/buildcache"
            buildcache_managed = False

        from ...image import ImageBuildSpec
        from ...image.backend import (
            UnknownBuildBackendError,
            create_build_backend,
            resolve_build_config,
        )
        from ...no_cache_state import consume_no_cache
        from .ecr_auth import ECRAuthenticator

        # rc-8j7.2/.4: resolve WHERE the build runs (backend) + cache mode +
        # push mode + concurrency from env > provider_config.ecs.build >
        # rc.yml build. A zero-config stack gets the all-default BuildConfig
        # (local backend, mode=max, serial-safe bounded pool). A typo'd
        # backend/cache_mode surfaces as a clean ProviderConfigError instead
        # of a stack trace.
        try:
            build_cfg = resolve_build_config(ctx.provider_config, ctx.rc_yml_v2)
        except (UnknownBuildBackendError, ValueError) as exc:
            raise ProviderConfigError(str(exc)) from exc

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
        session = self.session_factory(ctx)
        auth = ECRAuthenticator(session=session)

        # Pre-authenticate the cache registry so buildx can pull/push cache
        # layers. The SAME `auth` is handed to the build backend below, and
        # ECRAuthenticator caches by host — so per-service pushes to the same
        # ECR account/region (the common case) reuse this login instead of
        # re-authenticating, which also keeps parallel pushes to one login.
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
        # When rc derived the cache repo (adopted/--no-state stack), create it
        # if it doesn't exist yet — the terraform path provisions this repo for
        # managed stacks, so rc owns it here. Best-effort: a perms/quota failure
        # degrades to no-cache rather than blocking the deploy.
        if buildcache_repo and not buildcache_managed:
            if not self._ensure_buildcache_repo(session, buildcache_repo):
                buildcache_repo = None

        # When user passed --tag X (and X != latest), see if X already
        # exists in ECR and short-circuit to "re-tag existing → latest".
        ecr_client = None
        skip_when_tag_exists = requested_tag is not None and requested_tag != "latest"

        pushed: list[str] = []
        # rc-8j7.1: resolve each service to a ready-to-run ImageBuildSpec, then
        # hand the batch to the configured BuildBackend. The ECR re-tag
        # short-circuit (--tag re-use) stays here — it's a pure ECR API call,
        # not a build — and its services are recorded in `pushed` directly.
        build_specs: list[ImageBuildSpec] = []
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
            build_specs.append(
                ImageBuildSpec(
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
                    # rc-8j7.4: cache export mode + buildx --push are config-
                    # driven; defaults (mode=max, push False) reproduce today's
                    # --cache-to mode=max + --load + separate-push behavior.
                    cache_mode=build_cfg.cache_mode,
                    push=build_cfg.push,
                )
            )

        # rc-8j7.1/.3: build + push through the configured backend. The local
        # backend runs docker buildx here (parallelizing independent image
        # groups up to build_cfg.max_workers); a remote backend (rc-8j7.5
        # aws-codebuild) builds off the runner inside CodeBuild near ECR. The
        # session + resolved CodeBuild config + project + region are threaded
        # through so the remote backend can reach AWS; the local backend
        # ignores them. Returns the built service names in input order.
        backend = create_build_backend(
            build_cfg.backend,
            authenticator=auth,
            progress=self.progress,
            max_workers=build_cfg.max_workers,
            session=session,
            codebuild=build_cfg.codebuild,
            project=ctx.project,
            region=_ecs_cfg(ctx).get("region"),
        )
        pushed += backend.build_and_push(build_specs)
        return pushed

    def _ensure_buildcache_repo(self, session: Any, repo_url: str) -> bool:
        """Ensure the derived buildcache ECR repo exists (create if missing).

        Mirrors the terraform-managed buildcache repo for adopted/--no-state
        stacks: an untagged-expiry lifecycle keeps the cache from growing
        without bound. Returns True if the repo is usable, False on any
        permission/quota error so the caller degrades to no-cache builds
        rather than failing the deploy.
        """
        repo_name = repo_url.split("/", 1)[1] if "/" in repo_url else repo_url
        ecr = session.client("ecr")
        try:
            ecr.describe_repositories(repositoryNames=[repo_name])
            return True  # already exists
        except Exception:  # noqa: BLE001 - most likely RepositoryNotFound
            pass
        try:
            ecr.create_repository(repositoryName=repo_name)
        except Exception as exc:  # noqa: BLE001
            # A concurrent deploy may have created it between describe+create;
            # that's fine. Anything else (perms/quota) → degrade to no-cache.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code != "RepositoryAlreadyExistsException":
                self._emit(
                    f"  could not create buildcache repo {repo_name}: {exc!s} "
                    f"— falling back to no-cache builds"
                )
                return False
        # Best-effort: expire untagged cache blobs after 14 days so the repo
        # doesn't grow without bound (each service keeps one mutable
        # <svc>-cache tag, so only superseded layers age out). Never fatal.
        try:
            ecr.put_lifecycle_policy(
                repositoryName=repo_name,
                lifecyclePolicyText=(
                    '{"rules":[{"rulePriority":1,"selection":'
                    '{"tagStatus":"untagged","countType":"sinceImagePushed",'
                    '"countUnit":"days","countNumber":14},'
                    '"action":{"type":"expire"}}]}'
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        self._emit(f"  buildcache repo {repo_name} ready (rc-managed)")
        return True

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

    def deploy_preflight(
        self,
        ctx: DeployContext,
        out_dir: Path,
        force: bool = False,
        principal_arn: Optional[str] = None,
    ) -> Optional[Any]:
        """Verify the deploy principal BEFORE terraform touches anything.

        rc-g3jy. Checks the terraform binary and its version against the
        version that wrote the remote state, state read access, lock
        acquire/release, and every IAM action the just-rendered module will
        need — reporting the COMPLETE set at once rather than one failed
        production deploy at a time.

        Runs after emit_terraform because the IAM action set is derived from
        the emitted ``.tf`` files. Deriving it from a plan instead would need
        state access, which is one of the things being checked.

        Auto-runs only for a remote (s3) backend, which is what "this stack
        deploys somewhere that matters" looks like: a local-backend stack has
        no state bucket or lock table to check, and its credentials are a
        workstation's. ``force=True`` (``rc preflight``) runs it regardless.
        Set RC_SKIP_PREFLIGHT=1 to opt out entirely.

        Returns the report, or None when it did not run. Blocking findings
        raise ProviderConfigError; a failure of the CHECKER itself never
        breaks a deploy — it degrades to a warning, because a preflight that
        can grounded a working deploy is worse than no preflight.
        """
        if os.environ.get("RC_SKIP_PREFLIGHT"):
            return None
        if getattr(ctx, "skip_terraform", False):
            return None
        backend_cfg = ctx.tf_backend_config or {}
        if not force and backend_cfg.get("type") != "s3":
            return None
        ecs_cfg = _ecs_cfg(ctx)
        try:
            session = self.session_factory(ctx)
            report = run_preflight(
                tf_dir=Path(out_dir),
                backend_cfg=backend_cfg,
                session=session,
                region=ecs_cfg.get("region"),
                project=ctx.project,
                # rc-zu1x: the principal that matters is the one the deploy
                # will really run as. An explicit --principal wins; otherwise
                # rc.yml's deploy_role_arn, which also makes that role a
                # VERSIONED fact instead of a hand-made bootstrap artifact
                # that exists nowhere in git.
                deploy_principal_arn=principal_arn or ecs_cfg.get("deploy_role_arn"),
            )
        except Exception as exc:  # noqa: BLE001 — the checker is not the job
            self._warn(
                f"deploy preflight could not run ({type(exc).__name__}: "
                f"{exc}); proceeding unchecked."
            )
            return None

        for check in report.checks:
            if check.status != _PREFLIGHT_FAIL and check.remedy:
                self._warn(f"preflight — {check.name}: {check.detail}")
        if report.ok:
            return report

        fragment = report.policy_fragment()
        detail = report.render_table()
        # rc-u0wr: RC_SKIP_PREFLIGHT=1 was the only way past a blocking
        # finding, which turns the feature OFF on the stack that most needs
        # it -- a false positive then costs you the 22 true findings as well.
        # Advisory mode keeps every check running and every finding reported,
        # and only drops the block.
        if os.environ.get("RC_PREFLIGHT_ADVISORY"):
            self._emit(
                "  warning: deploy preflight found blocking issues "
                "(RC_PREFLIGHT_ADVISORY set — reporting, not blocking):\n" + detail
            )
            self._warn(
                "deploy preflight found blocking issues but RC_PREFLIGHT_ADVISORY "
                "is set, so the deploy proceeded unblocked."
            )
            return report
        message = (
            "deploy preflight failed — every problem found, not just the "
            "first:\n" + detail
        )
        if fragment:
            message += (
                '\n\n  Grant these to the deploy role (Resource is "*" for '
                "speed; narrow it once the deploy runs):\n" + fragment
            )
        message += (
            "\n\n  These checks are advisory: iam:SimulatePrincipalPolicy "
            "does not evaluate SCPs or permission boundaries, so a denial can "
            "be rc's error rather than yours. To proceed while keeping the "
            "report, set RC_PREFLIGHT_ADVISORY=1; RC_SKIP_PREFLIGHT=1 turns "
            "the checks off entirely."
        )
        raise ProviderConfigError(message)

    def _warn_on_eni_bound_fleet(
        self, pressure: Any, eni_trunking: Optional[bool], region: Optional[str] = None
    ) -> None:
        """Say WHY the fleet is this size when the answer is "a networking cap".

        rc-hguq ask 4. "You need 4 instances" and "you need 4 instances
        because awsvpc costs one ENI per task and this shape only has 4" are
        very different messages, and only the second is actionable -- the
        more so because ENI counts are FLAT across the useful part of the m5
        range, so the operator's instinct (buy a bigger box) does nothing
        until m5.4xlarge.
        """
        if pressure.binding_dimension != "eni":
            return
        shape = pressure.shape
        head = (
            f"EC2 fleet size is set by NETWORKING, not by CPU or memory: "
            f"{pressure.steady_task_count} awsvpc task(s) need "
            f"{pressure.steady_instances} {shape.name} instance(s) because "
            f"each task consumes one ENI and this shape has "
            f"{shape.task_eni_slots} task slot(s). CPU and memory would fit "
            f"on fewer."
        )
        if not pressure.eni_bound_but_trunkable:
            self._warn(
                head + " Note ENI counts are flat across much of an instance "
                "family, so a bigger shape in the same family may buy no extra "
                "task slots at all — check the slot count, not the vCPU count."
            )
            return
        state = _trunking_state(eni_trunking)
        where = f"in {region}" if region else "in this region"
        fix = (
            f"the awsvpcTrunking account setting is DISABLED {where}"
            if state == TRUNKING_DISABLED
            else f"rc has not checked whether awsvpcTrunking is enabled {where}"
        )
        region_flag = f" --region {region}" if region else ""
        self._warn(
            head + f" {shape.name} supports ENI trunking, which would raise it to "
            f"{shape.trunked_task_limit} task slot(s) per instance, but "
            f"{fix}. The setting is PER-REGION -- enabling it in another "
            f"region does not apply here. Enable it with `aws ecs "
            f"put-account-setting-default --name awsvpcTrunking --value "
            f"enabled{region_flag}` (it affects EC2 container instances "
            f"only), or set "
            f"provider_config.ecs.ec2_capacity.eni_trunking: true if it is "
            f"already on. This is the difference between paying for instances "
            f"your workload needs and paying for instances an ENI limit needs."
        )

    def _task_defs_are_owned_out_of_band(self, ctx: DeployContext) -> bool:
        """True when rc.yml declares terraform is not the source of truth for
        this stack's task definitions."""
        return bool(_ecs_cfg(ctx).get("ignore_task_definition_changes", False))

    def _needs_preapply_plan(self, ctx: DeployContext) -> bool:
        """Whether to save + inspect the plan before applying.

        Two triggers, both about failures only visible against LIVE state:

        * ignore_task_definition_changes (rc-avcr) -- a forced task-def
          replacement silently drops out-of-band secrets.
        * any EC2-launch service (rc-5a4g) -- binpack placement is rejected
          while the service's live availability_zone_rebalancing is ENABLED,
          and whether it is depends on how the service was first created.

        Nearly free: the saved plan is reused as the apply's input, so this
        costs no extra terraform cycle -- apply would plan anyway.
        """
        if self._task_defs_are_owned_out_of_band(ctx):
            return True
        default_launch_type = _ecs_cfg(ctx).get("default_launch_type", "FARGATE")
        return any(
            (spec.launch_type or default_launch_type) == "EC2"
            for spec in (ctx.services or {}).values()
        )

    @staticmethod
    def _preapply_plan_path(out_dir: Path) -> Path:
        """Where the inspected plan is saved. ``*.tfplan`` is already in the
        emitted .gitignore, so this never lands in a user's repo."""
        return Path(out_dir) / "rc-preapply.tfplan"

    def _inspect_plan(self, runner: Any, plan_file: Path) -> list[str]:
        """Run every live-state detector over the saved plan.

        Findings are recorded in the sink and returned so the deploy path can
        surface them before terraform touches anything.
        """
        messages = self._warn_on_task_def_replacement(runner, plan_file)
        try:
            plan_json = runner.show_json(plan_file)
        except Exception:  # noqa: BLE001 — already reported by the call above
            return messages
        conflict_msg = render_binpack_conflict_warning(
            detect_binpack_az_rebalancing_conflicts(plan_json)
        )
        if conflict_msg:
            self._warn(conflict_msg)
            messages.append(conflict_msg)
        return messages

    def _warn_on_task_def_replacement(self, runner: Any, plan_file: Path) -> list[str]:
        """Report task definitions this plan REPLACES rather than updates.

        rc-avcr. ``ignore_task_definition_changes: true`` exists so a stack
        whose task defs are reconciled out of band (secrets wired on by a
        post-deploy script) can run a stateful apply without terraform
        re-registering revisions that strip those values. It does not hold
        when the task def is REPLACED instead of updated: changing
        ``launch_type`` flips ``requires_compatibilities`` FARGATE -> EC2,
        which is ForceNew, and ``lifecycle ignore_changes`` suppresses
        diff-driven updates only — it cannot suppress a replacement forced by
        a different attribute. The rendered replacement carries none of the
        reconciled secrets, and the service is repointed at it in the same
        apply.

        Never fatal: the plan may be exactly what the operator intends (they
        may be about to re-run the reconcile). The value is that it is no
        longer silent. Returns the warnings raised, and records them in the
        sink for PlanResult.

        A failure to read or parse the plan is swallowed — this is a
        detector, and a detector that breaks a working deploy is worse than
        one that misses a case. It does report that it couldn't look.
        """
        try:
            plan_json = runner.show_json(plan_file)
        except Exception as exc:  # noqa: BLE001 — never break a deploy to warn
            message = (
                "ignore_task_definition_changes is on, but rc could not read "
                f"the terraform plan to check for forced task-definition "
                f"replacements ({type(exc).__name__}: {exc}). If this apply "
                "replaces a task definition, out-of-band secrets/env on the "
                "live revision will be dropped — re-run your task-definition "
                "reconcile afterwards."
            )
            self._warn(message)
            return [message]

        replacements = detect_task_definition_replacements(plan_json)
        message = render_replacement_warning(replacements)
        if not message:
            return []
        self._warn(message)
        return [message]

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
        # Container Insights is off by default (see cluster.tf.j2). When it's
        # off AWS never auto-creates the performance log group, and the
        # template emits no aws_cloudwatch_log_group.container_insights
        # resource to import into — so there's nothing to reconcile. Skip
        # the boto3 describe entirely. (Clusters that HAD insights on and
        # are now flipping off drop the resource from config, so terraform
        # apply destroys the now-managed log group on its own.)
        if not ecs_cfg.get("container_insights", False):
            return
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

    def _reconcile_adopt_owned_alb(
        self,
        ctx: DeployContext,
        runner: TerraformRunner,
    ) -> None:
        """Import an adopted ALB + its listeners into terraform state.

        Unlike _reconcile_orphan_log_groups / _reconcile_orphan_backup_bucket
        (which import resources whose IDs rc derives itself — a log group
        name from the cluster name, an S3 bucket name from rc.yml), the
        resource ids here are foreign LITERALS the user supplies
        (provider_config.ecs.adopt_owned.alb). That changes the failure
        mode: those reconcilers can safely no-op when the AWS probe finds
        nothing ("terraform will create it fresh on apply" is a normal,
        expected outcome for a self-derived id). Here, "the ARN the user
        gave me isn't live" means MISCONFIGURATION — letting apply proceed
        would have terraform CREATE A BRAND NEW ALB under this project's
        name while the real, traffic-serving ALB is untouched and the
        stack silently points nowhere useful. So: hard error, not a
        silent skip.

        Also unlike those two: there is no delete-and-recreate fallback on
        import failure. This is live foreign prod infra (someone else's
        ALB), not something rc can safely destroy and let terraform
        rebuild — surface the failure and stop.
        """
        ecs_cfg = _ecs_cfg(ctx)
        adopt_owned_alb_cfg = (ecs_cfg.get("adopt_owned") or {}).get("alb") or {}
        if not adopt_owned_alb_cfg:
            return

        alb_arn = adopt_owned_alb_cfg["arn"]
        http_listener_arn = adopt_owned_alb_cfg["http_listener_arn"]
        https_listener_arn = adopt_owned_alb_cfg.get("https_listener_arn")

        session = self.session_factory(ctx)
        elbv2 = session.client("elbv2")

        try:
            resp = elbv2.describe_load_balancers(LoadBalancerArns=[alb_arn])
            live_albs = resp.get("LoadBalancers", [])
        except Exception as exc:  # noqa: BLE001
            raise ProviderConfigError(
                f"provider_config.ecs.adopt_owned.alb: could not verify "
                f"{alb_arn} is live — {type(exc).__name__}: {exc}"
            ) from exc
        if not live_albs:
            raise ProviderConfigError(
                f"provider_config.ecs.adopt_owned.alb.arn {alb_arn} is not "
                f"a live ALB in this account/region. Refusing to proceed: "
                f"apply would otherwise CREATE A NEW ALB under this "
                f"project's name while any real ALB at that arn keeps "
                f"serving traffic, untouched. Fix the arn, or remove "
                f"adopt_owned.alb to create fresh."
            )

        listener_arns_to_check = [http_listener_arn] + (
            [https_listener_arn] if https_listener_arn else []
        )
        try:
            resp = elbv2.describe_listeners(ListenerArns=listener_arns_to_check)
            live_listener_arns = {
                listener["ListenerArn"] for listener in resp.get("Listeners", [])
            }
        except Exception as exc:  # noqa: BLE001
            raise ProviderConfigError(
                f"provider_config.ecs.adopt_owned.alb: could not verify "
                f"listener(s) {listener_arns_to_check} are live — "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        missing = [a for a in listener_arns_to_check if a not in live_listener_arns]
        if missing:
            raise ProviderConfigError(
                f"provider_config.ecs.adopt_owned.alb: listener arn(s) "
                f"{missing} are not live. Refusing to proceed — same "
                f"reasoning as the ALB check above."
            )

        imports = [
            ("aws_lb.main", alb_arn),
            ("aws_lb_listener.http", http_listener_arn),
        ]
        if https_listener_arn:
            imports.append(("aws_lb_listener.https", https_listener_arn))

        for address, resource_id in imports:
            try:
                runner.import_resource(address, resource_id)
                self._emit(
                    f"imported adopt_owned ALB resource {address} "
                    f"({resource_id}) into terraform state"
                )
            except TerraformError as exc:
                msg = ((exc.stderr or "") + (exc.stdout or "")).lower()
                already_managed_signals = (
                    "already managed",
                    "is already managing",
                    "already exists in state",
                )
                if any(s in msg for s in already_managed_signals):
                    self._emit(
                        f"  ✓ {address} already in terraform state — prior "
                        f"'Error: Resource already managed' is "
                        f"informational; deploy continues."
                    )
                    continue
                # No safe fallback (see docstring) — surface and stop.
                raise

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

    def _force_new_deployments(
        self,
        ctx: DeployContext,
        services: list[str],
        reconcile_scale: bool = False,
    ) -> None:
        """Force-roll the named ECS services in dependency order (.46.5).

        reconcile_scale (rc-wji.2): when True, also set desiredCount =
        spec.replicas on each rolled service. Used by the --no-state path,
        which skips terraform (the terraform path already sets desired_count =
        svc.replicas via services.tf.j2). Makes rc.yml replicas the source of
        truth on a no-state roll instead of leaving the count to drift.

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
        # rc-6akx: resolve EVERY service's rollout percentages before rolling
        # ANY of them. On the --no-state path terraform never runs, so this is
        # the first (and only) validation an rc.yml `deployment:` block gets —
        # resolving inside the loop would roll services 1..n-1 and only then
        # raise on service n, leaving the stack half-deployed on a config rc
        # could have rejected before touching AWS at all.
        deployments = {
            svc: _deployment_percents(svc, ctx.services.get(svc)) for svc in ordered
        }
        rolled_names: list[str] = []
        for svc in ordered:
            spec = ctx.services.get(svc)
            # Stateful services roll one-at-a-time (min=0/max=100); everything
            # else gets zero-downtime config: keep 100% of old tasks until new
            # ones are healthy, up to 200% during the roll, + circuit breaker to
            # auto-roll-back a bad deploy. This mirrors the terraform template
            # for stacks that deploy --no-state (terraform bypassed), so the live
            # service still gets the right rollout behavior.
            #
            # rc-usk0: it only ACTUALLY mirrors it because both sides now call
            # _is_stateful_service. This line used to be
            #     bool(getattr(spec, "volumes", None))
            # which is volumes-only and misses singleton schedulers, so a
            # celery-beat was rolled with an overlap window on every deploy —
            # two beat schedulers double-firing every periodic task — and each
            # deploy silently reverted any hand-applied correction.
            #
            # rc-6akx: for the same reason, the percentages come from
            # _deployment_percents rather than being written out here. This
            # call runs on EVERY --no-state deploy, so a literal 100/200
            # would quietly overwrite an rc.yml `deployment:` override on the
            # live service — the identical silent-revert failure, one field
            # over. Stateful services keep dep_cfg=None (leave the live
            # config alone), which is what they have always done.
            stateful = _is_stateful_service(svc, spec)
            deployment = deployments[svc]
            dep_cfg = (
                None
                if stateful
                else {
                    "minimumHealthyPercent": deployment.minimum_healthy,
                    "maximumPercent": deployment.maximum,
                    "deploymentCircuitBreaker": {"enable": True, "rollback": True},
                }
            )
            # rc-py32: the rendered name comes first. Under a shared cluster the
            # live service is "<prefix><svc>" and a bare "django" simply is not
            # there; the legacy cluster-prefix probe below cannot supply it,
            # because cluster_prefix derives from the CLUSTER name (it would try
            # "foundry-tenants-django", never "acme-django").
            candidates = []
            rendered = _ecs_service_name(ctx, svc)
            if rendered != svc:
                candidates.append(rendered)
            candidates.append(svc)
            if cluster_prefix and cluster_prefix != ctx.project:
                candidates.append(f"{cluster_prefix}-{svc}")
            last_err: Exception | None = None
            for name in candidates:
                try:
                    kwargs: dict[str, Any] = dict(
                        cluster=cluster,
                        service=name,
                        forceNewDeployment=True,
                    )
                    if dep_cfg is not None:
                        kwargs["deploymentConfiguration"] = dep_cfg
                    if reconcile_scale and spec is not None:
                        # rc-wji.2: mirror services.tf.j2 desired_count so a
                        # --no-state roll applies rc.yml replicas (terraform is
                        # bypassed and won't set it otherwise).
                        kwargs["desiredCount"] = spec.replicas
                    client.update_service(**kwargs)
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

        # Wait for every rolled service to reach steady state (new tasks
        # healthy, deployment COMPLETED) so `rc deploy` GATES on the roll
        # finishing — otherwise a worker stuck mid-roll (no ALB 200 check
        # covers it) reports a green deploy. RC_DEPLOY_WAIT_S=0 skips (tests).
        self._wait_for_services_stable(client, cluster, rolled_names)

    def _wait_for_services_stable(
        self,
        client: Any,
        cluster: str,
        services: list[str],
    ) -> None:
        """Block until each rolled service is stable (runningCount==desired,
        single COMPLETED deployment) or the budget elapses.

        Gives `rc deploy` a real completion gate for workers (which no HTTP
        check covers). RC_DEPLOY_WAIT_S (default 900) sets the budget; 0
        skips entirely (unit tests). On timeout, raises ProviderError with
        the lagging services so a stuck roll fails the deploy loudly.
        """
        import os as _os

        budget = int(_os.environ.get("RC_DEPLOY_WAIT_S", "900"))
        if budget <= 0 or not services:
            return
        interval = float(_os.environ.get("RC_DEPLOY_WAIT_INTERVAL_S", "15"))
        deadline = time.monotonic() + budget

        from ...heartbeat import heartbeat as _hb

        def _stable(name: str) -> bool:
            resp = client.describe_services(cluster=cluster, services=[name])
            svcs = resp.get("services") or []
            if not svcs:
                return False
            s = svcs[0]
            deps = s.get("deployments") or []
            primary = [d for d in deps if d.get("status") == "PRIMARY"]
            return (
                len(deps) == 1
                and s.get("runningCount") == s.get("desiredCount")
                and bool(primary)
                and primary[0].get("rolloutState", "COMPLETED") == "COMPLETED"
            )

        with _hb(self.progress, "waiting for services to reach steady state"):
            pending = list(services)
            while pending:
                pending = [n for n in pending if not _stable(n)]
                if not pending:
                    return
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        "deploy did not stabilize within "
                        f"{budget}s — still rolling: {', '.join(pending)}. "
                        "A task is likely failing its health check or crash-"
                        "looping; check service events / task logs."
                    )
                time.sleep(interval)

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
        # rc-e5u.25.9: for EC2-launch-type stacks, drain services + scale
        # the capacity ASG to zero via the AWS SDK BEFORE terraform ever
        # touches the module. See _predrain_ec2_capacity's docstring for
        # the full mechanism this closes. No-ops (zero AWS calls) for a
        # Fargate-only stack.
        self._predrain_ec2_capacity(ctx)
        runner = self.runner_factory(out_dir)
        runner.init()
        runner.destroy()

    def _ec2_service_names(self, ctx: DeployContext) -> list[str]:
        """Service names that resolve to EC2 launch_type -- local/pure,
        no AWS calls, so callers can decide whether to touch AWS at all
        (a Fargate-only stack must make zero SDK calls here).

        May UNDER-report on the destroy path: ctx.services is built with
        require_compose_file=False there, so a compose-only service is
        absent from it entirely once the compose file is gone. See
        _local_ec2_list_is_complete for when that can bite.
        """
        ecs_cfg = _ecs_cfg(ctx)
        default_launch_type = ecs_cfg.get("default_launch_type", "FARGATE")
        return [
            name
            for name, spec in sorted(ctx.services.items())
            if (spec.launch_type or default_launch_type) == "EC2"
        ]

    def _local_ec2_list_is_complete(self, ctx: DeployContext) -> bool:
        """Whether _ec2_service_names can be trusted to name every EC2
        service in the stack (rc-915l).

        The gate is exact, not a heuristic, and rests on two facts:

        1. ``launch_type`` is an rc.yml-only field -- docker-compose has no
           way to express it. So a service that is EC2 *without* an rc.yml
           entry can only have got there via ``default_launch_type: EC2``.
        2. ``build_deploy_context`` resolves the deploy set as
           ``compose_names | rc_names``, and only the compose half can go
           empty (a missing compose file is tolerated on the destroy path).
           rc.yml-declared services never drop out.

        Therefore with default_launch_type != EC2, every EC2 service must
        be rc.yml-declared, and every rc.yml-declared service is present:
        the local list is provably complete and asking AWS would be a
        pointless call on every Fargate destroy. With it set to EC2, a
        compose-only service is EC2 too -- and that is exactly the service
        that vanishes when the compose file does.
        """
        return _ecs_cfg(ctx).get("default_launch_type", "FARGATE") != "EC2"

    @staticmethod
    def _describe_services_all(ecs: Any, cluster: str, names: list[str]) -> list[dict]:
        """describe_services over any number of names.

        The API takes at most 10 per call and rejects the whole request
        past that ("Member must have length less than or equal to 10"), so
        a caller that passes its full service list works right up until a
        stack has eleven services. Returns the merged services[]; anything
        in failures[] is dropped, which is what every caller here wants
        (a service AWS can't describe is one that no longer exists).
        """
        out: list[dict] = []
        for i in range(0, len(names), 10):
            desc = ecs.describe_services(cluster=cluster, services=names[i : i + 10])
            out.extend(desc.get("services") or [])
        return out

    def _live_ec2_service_names(
        self, ecs: Any, cluster: str, project: str
    ) -> list[str]:
        """Services in the live cluster running on THIS project's EC2
        capacity provider (rc-915l). Best-effort: returns [] on any error,
        because every caller treats it as an addition to the local list,
        never as a replacement for it.

        Scoping. Cluster-wide enumeration is safe here because rc always
        creates its own cluster (cluster.tf.j2 emits
        ``resource "aws_ecs_cluster" "main"`` unconditionally; adopt_owned
        covers the ALB, not the cluster), so a foreign stack cannot be
        sharing it. The capacity-provider filter is the second line: rc's
        EC2 services carry a capacity_provider_strategy naming
        ``${project}-ec2-cp`` and NO launchType (services.tf.j2), while
        Fargate ones carry launch_type = "FARGATE", so keying on the
        strategy scopes the drain to this project's EC2 capacity even if
        something else did end up in the cluster.
        """
        capacity_provider = f"{project}-ec2-cp"
        try:
            arns: list[str] = []
            token: Optional[str] = None
            while True:
                kwargs: dict[str, Any] = {"cluster": cluster, "maxResults": 100}
                if token:
                    kwargs["nextToken"] = token
                page = ecs.list_services(**kwargs)
                arns.extend(page.get("serviceArns") or [])
                token = page.get("nextToken")
                if not token:
                    break

            # Anything that lands in failures[] vanished between the list
            # and the describe -- already gone, which is the end state the
            # drain is trying to reach, so dropping it is correct.
            names: list[str] = []
            for svc in self._describe_services_all(ecs, cluster, arns):
                strategy = svc.get("capacityProviderStrategy") or []
                on_our_ec2 = any(
                    e.get("capacityProvider") == capacity_provider for e in strategy
                )
                if on_our_ec2 and svc.get("serviceName"):
                    names.append(svc["serviceName"])
            return sorted(names)
        except Exception as exc:  # noqa: BLE001
            self._emit(
                f"  WARN: destroy pre-drain: could not enumerate live "
                f"services in cluster {cluster!r} ({exc!s}); draining only "
                f"the services named in rc.yml."
            )
            return []

    def _predrain_ec2_capacity(self, ctx: DeployContext) -> None:
        """rc-e5u.25.9: drain EC2-launch-type services and scale their
        capacity ASG to zero via the AWS SDK before ``terraform destroy``.

        The real-AWS failure this fixes (bd rc-e5u.25.9): after a
        successful EC2-launch deploy, ``terraform destroy`` (1) hung the
        full 20-minute default timeout waiting for the ECS service to go
        DRAINING -> INACTIVE, and (2) then failed detaching the Internet
        Gateway with a DependencyViolation because the ASG's EC2
        instance still had an ENI with a mapped public IP attached.

        Root cause, reasoned through against AWS's own docs (see bead
        for full citations) -- NOT terraform dependency-graph ordering:
        terraform already sequences aws_ecs_service ->
        aws_ecs_capacity_provider -> aws_autoscaling_group correctly (a
        capacity_provider_strategy / auto_scaling_group_arn reference
        chain makes each dependent destroyed before its dependency), so
        the ASG is never touched by terraform until the service is
        already gone. The deadlock is inside the SERVICE's own delete:
        every non-stateful service (services.tf.j2) sets
        deployment_minimum_healthy_percent = 100, and AWS's container-
        instance-draining docs state plainly that "[i]f the minimum is
        100%, the service scheduler can't remove existing tasks until
        the replacement tasks are considered healthy"
        (https://docs.aws.amazon.com/AmazonECS/latest/developerguide/container-instance-draining.html).
        The capacity provider's managed_scaling reacts to
        CapacityProviderReservation independently of terraform -- the
        instant desiredCount starts heading toward 0, AWS's own scaling
        automation can put the (often sole) EC2 instance into container-
        instance DRAINING while desiredCount is still > 0, and the
        service scheduler then wants a REPLACEMENT task placed before it
        will stop the one running -- but a single-instance ASG has
        nowhere to place it. Genuine deadlock, not just a slow drain;
        Fargate has no container instances to drain so it never hits
        this path (test_ecs_full_lifecycle.py's history is clean).

        The fix: force desiredCount to 0 via the SDK, and wait for it to
        actually take effect, BEFORE terraform (or anything terraform's
        own calls might trigger AWS-side) gets involved at all. Scaling
        DOWN to a lower desired count needs no replacement placement, so
        the deadlock precondition never arises. Then explicitly resize
        the ASG to 0 and wait for its instance(s) to terminate, so by
        the time `terraform destroy` starts there is no live EC2
        instance (and no ENI holding a mapped public IP) left for the
        Internet Gateway's delete to race against -- closing the second
        failure by construction, independent of whether the first one
        would otherwise have been slow.

        Scoped strictly to EC2-launch-type services (`_ec2_service_names`
        short-circuits with zero AWS calls when there are none) so the
        proven Fargate destroy path is completely untouched.

        Best-effort throughout and must NEVER raise: this runs against
        stacks that may already be partially destroyed (cluster gone,
        ASG never created, expired creds, ...), and `runner.destroy()`
        remains the backstop either way. A raise here would make a
        broken stack permanently undestroyable via `rc destroy`, which
        is strictly worse than the bug being fixed.

        Known limitation (not handled here): standalone tasks started
        via `rc run` / `run_one_off` are invisible to
        `update_service(desiredCount=0)` and AWS's own container-
        instance draining leaves them running until they stop on their
        own or are stopped manually -- see this method's unit tests and
        the bead for the tracked follow-up.
        """
        # rc-py32: rendered ECS names, not compose names. _live_ec2_service_names
        # below returns what AWS actually calls the services, and the two lists
        # are unioned -- so they have to be in the same namespace or a prefixed
        # stack drains nothing and "missed" reports every live service as
        # unknown.
        ec2_services = [_ecs_service_name(ctx, n) for n in self._ec2_service_names(ctx)]
        # rc-915l: ctx.services is built with require_compose_file=False on
        # this path, so a compose-only service is missing from it entirely
        # once the compose file is gone (ephemeral stacks delete theirs the
        # moment the deploy lands). Such a service is still deployed, still
        # holds the EC2 instance, and was silently never drained -- the exact
        # deadlock the pre-drain exists to prevent, reintroduced by a deleted
        # file. When the local list can't be trusted, ask the cluster.
        local_is_complete = self._local_ec2_list_is_complete(ctx)
        if not ec2_services and local_is_complete:
            return  # Fargate-only (or plan-only) stack -- no AWS calls.

        try:
            ecs_cfg = _ecs_cfg(ctx)
            cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
            session = self.session_factory(ctx)
            ecs = session.client("ecs")
        except Exception as exc:  # noqa: BLE001
            self._emit(
                f"  WARN: destroy pre-drain: could not create an AWS ECS "
                f"client ({exc!s}); skipping pre-drain -- terraform "
                f"destroy will attempt teardown directly."
            )
            return

        if not local_is_complete:
            live = self._live_ec2_service_names(ecs, cluster, ctx.project)
            missed = sorted(set(live) - set(ec2_services))
            if missed:
                self._emit(
                    f"  destroy: {len(missed)} deployed EC2 service(s) are "
                    f"not in the local config (compose file gone?) but are "
                    f"live in {cluster!r}; draining them too: "
                    f"{', '.join(missed)}"
                )
            ec2_services = sorted(set(ec2_services) | set(live))

        if not ec2_services:
            # Nothing declared locally and nothing live -- e.g. an EC2-default
            # stack that was already torn down. Still let terraform run.
            return

        self._emit(
            f"  destroy: pre-draining {len(ec2_services)} EC2-launch-type "
            f"service(s) before terraform destroy: {', '.join(ec2_services)}"
        )
        for name in ec2_services:
            try:
                ecs.update_service(cluster=cluster, service=name, desiredCount=0)
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    f"  WARN: destroy pre-drain: update_service"
                    f"(desiredCount=0) failed for {name!r} ({exc!s}) -- "
                    f"service may already be gone; continuing."
                )

        self._wait_for_zero_running_services(ecs, cluster, ec2_services)
        self._scale_down_ec2_capacity(ctx, cluster)

    def _wait_for_zero_running_services(
        self, ecs: Any, cluster: str, service_names: list[str]
    ) -> None:
        """Poll until every named service reports runningCount == 0 (or is
        no longer reported at all -- already deleted out of band), bounded
        by RC_DESTROY_DRAIN_TIMEOUT_S. Never raises; a timeout just emits a
        WARN and lets terraform's own destroy timeout be the backstop."""
        from ...heartbeat import heartbeat as _hb

        timeout_s = _destroy_drain_timeout_s()
        deadline = time.monotonic() + timeout_s
        remaining = set(service_names)
        with _hb(self.progress, "destroy pre-drain: waiting for tasks to stop"):
            # Poll-then-check-deadline (not check-then-poll): guarantees at
            # least one describe_services call even when the timeout
            # budget is 0 (tests short-circuit the wait this way).
            while True:
                try:
                    described = self._describe_services_all(
                        ecs, cluster, sorted(remaining)
                    )
                except Exception as exc:  # noqa: BLE001
                    self._emit(
                        f"  WARN: destroy pre-drain: describe_services "
                        f"failed ({exc!s}); giving up on the drain wait "
                        f"and proceeding to terraform destroy."
                    )
                    return
                reported = {
                    s.get("serviceName") for s in described if s.get("serviceName")
                }
                still_running = {
                    s.get("serviceName")
                    for s in described
                    if int(s.get("runningCount", 0) or 0) > 0
                }
                # Anything ECS no longer reports at all is treated as
                # already drained (deleted out of band).
                remaining = (remaining & reported) & still_running
                if not remaining:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(max(0.0, min(5.0, deadline - time.monotonic())))
        if remaining:
            self._emit(
                f"  WARN: destroy pre-drain: {len(remaining)} service(s) "
                f"still report running tasks after {timeout_s}s "
                f"({', '.join(sorted(remaining))}) -- proceeding to "
                f"terraform destroy anyway; it may hit its own drain "
                f"timeout."
            )

    def _scale_down_ec2_capacity(self, ctx: DeployContext, cluster: str) -> None:
        """Resize the project's EC2 capacity ASG (capacity.tf.j2's
        ``${var.project}-ec2-asg``) to zero and wait for its instance(s)
        to actually terminate, then defensively deregister any leftover
        container instances. Never raises."""
        from ...heartbeat import heartbeat as _hb

        asg_name = f"{ctx.project}-ec2-asg"
        try:
            session = self.session_factory(ctx)
            autoscaling = session.client("autoscaling")
        except Exception as exc:  # noqa: BLE001
            self._emit(
                f"  WARN: destroy pre-drain: could not create an "
                f"autoscaling client ({exc!s}); skipping ASG scale-down."
            )
            return

        try:
            autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                MinSize=0,
                MaxSize=0,
                DesiredCapacity=0,
            )
        except Exception as exc:  # noqa: BLE001
            self._emit(
                f"  WARN: destroy pre-drain: could not scale down ASG "
                f"{asg_name!r} ({exc!s}) -- it may not exist (yet), or "
                f"may already be gone; continuing to terraform destroy."
            )
            return

        self._emit(
            f"  destroy: scaled {asg_name!r} to 0; waiting for its "
            f"instance(s) to terminate"
        )
        timeout_s = _destroy_drain_timeout_s()
        deadline = time.monotonic() + timeout_s
        drained = False
        with _hb(
            self.progress,
            f"destroy pre-drain: waiting for {asg_name} instances to terminate",
        ):
            # Poll-then-check-deadline: guarantees at least one
            # describe_auto_scaling_groups call even with a 0s budget.
            while True:
                try:
                    resp = autoscaling.describe_auto_scaling_groups(
                        AutoScalingGroupNames=[asg_name]
                    )
                except Exception as exc:  # noqa: BLE001
                    self._emit(
                        f"  WARN: destroy pre-drain: "
                        f"describe_auto_scaling_groups failed ({exc!s}); "
                        f"giving up on the ASG drain wait."
                    )
                    break
                groups = resp.get("AutoScalingGroups") or []
                if not groups or not groups[0].get("Instances"):
                    drained = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(max(0.0, min(5.0, deadline - time.monotonic())))
        if not drained:
            self._emit(
                f"  WARN: destroy pre-drain: {asg_name!r} still shows "
                f"running instance(s) after {timeout_s}s -- proceeding "
                f"to terraform destroy anyway."
            )

        self._deregister_leftover_container_instances(ctx, cluster)

    def _deregister_leftover_container_instances(
        self, ctx: DeployContext, cluster: str
    ) -> None:
        """Belt-and-suspenders for the sibling bead (rc-e5u.25.8): force-
        deregister any container instances the cluster still knows about
        after the ASG scale-down, so a leftover record can't block
        `aws_ecs_cluster` destroy with
        ClusterContainsContainerInstancesException. Never raises."""
        try:
            session = self.session_factory(ctx)
            ecs = session.client("ecs")
            arns = (
                ecs.list_container_instances(cluster=cluster).get(
                    "containerInstanceArns"
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001
            self._emit(
                f"  WARN: destroy pre-drain: could not list container "
                f"instances on {cluster!r} ({exc!s})."
            )
            return
        for arn in arns:
            try:
                ecs.deregister_container_instance(
                    cluster=cluster, containerInstance=arn, force=True
                )
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    f"  WARN: destroy pre-drain: could not deregister "
                    f"container instance {arn} ({exc!s})."
                )

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
                service=_ecs_service_name(ctx, svc),
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

        resp = client.describe_services(
            cluster=cluster,
            services=[_ecs_service_name(ctx, n) for n in service_names],
        )
        # Key the report by the COMPOSE name: everything downstream (and the
        # user) refers to services the way rc.yml does, not the way they are
        # named in a shared cluster.
        reported = {
            _compose_service_name(ctx, s["serviceName"]): s
            for s in resp.get("services", [])
        }

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
        """Run a command as a FRESH one-off task on a service's task def.

        Unlike :meth:`exec` (``aws ecs execute-command`` into a running task,
        whose child process does NOT inherit the task's Secrets-Manager
        secrets), this launches a new task from the service's current task
        definition — so the command gets the task role AND the SM secrets
        injected by ECS, exactly like the real container. This is the right
        primitive for secret-dependent management commands (Django/Rails
        migrate, template sync, ...).

        Reuses the live service's task definition, network config and launch
        mechanism (see ``resolve_run_launch``) so the one-off lands in the
        same VPC/subnets/SGs and on the same capacity as the service. With
        ``wait``
        (default), blocks until the task stops, fetches its CloudWatch logs,
        and returns the container's real exit code; without it, returns the
        task ARN immediately (exit 0).
        """
        ecs_cfg = _ecs_cfg(ctx)
        cluster = ecs_cfg.get("cluster") or f"{ctx.project}-cluster"
        session = self.session_factory(ctx)
        ecs = session.client("ecs")

        svcs = (
            ecs.describe_services(
                cluster=cluster, services=[_ecs_service_name(ctx, service)]
            ).get("services")
            or []
        )
        if not svcs:
            raise ProviderError(
                f"rc run: service {service!r} not found in cluster {cluster!r}"
            )
        svc = svcs[0]
        task_def = svc.get("taskDefinition")
        if not task_def:
            raise ProviderError(f"rc run: service {service!r} has no task definition")
        net = svc.get("networkConfiguration")

        # Describe the task def unconditionally: it supplies the container
        # name to override AND (rc-fg83) the requiresCompatibilities that
        # decide how this task may legally be launched.
        td_desc = ecs.describe_task_definition(taskDefinition=task_def).get(
            "taskDefinition", {}
        )

        # Resolve the container to override. Default to the container whose
        # name matches the service, else the first one in the task def.
        cname = container
        if not cname:
            cdefs = td_desc.get("containerDefinitions") or []
            cname = next(
                (c["name"] for c in cdefs if c.get("name") == service),
                (cdefs[0]["name"] if cdefs else service),
            )

        run_kwargs: dict[str, Any] = {
            "cluster": cluster,
            "taskDefinition": task_def,
            "count": 1,
            "startedBy": "rc-run",
            "overrides": {
                "containerOverrides": [{"name": cname, "command": list(command)}]
            },
            **resolve_run_launch(svc, td_desc),
        }
        if net:
            run_kwargs["networkConfiguration"] = net

        self._emit(
            f"  rc run: one-off task on {service!r} ({cname}): " f"{' '.join(command)}"
        )
        resp = ecs.run_task(**run_kwargs)
        failures = resp.get("failures") or []
        tasks = resp.get("tasks") or []
        if failures or not tasks:
            raise ProviderError(f"rc run: run_task failed for {service!r}: {failures}")
        task_arn = tasks[0]["taskArn"]

        if not wait:
            return ExecResult(exit_code=0, stdout=f"{task_arn}\n", stderr="")

        from ...heartbeat import heartbeat as _hb

        with _hb(self.progress, f"running one-off task on {service!r}"):
            waiter = ecs.get_waiter("tasks_stopped")
            waiter.wait(
                cluster=cluster,
                tasks=[task_arn],
                WaiterConfig={"Delay": 6, "MaxAttempts": max(1, int(timeout / 6))},
            )

        desc = (
            ecs.describe_tasks(cluster=cluster, tasks=[task_arn]).get("tasks") or [{}]
        )[0]
        containers = desc.get("containers") or [{}]
        cont = next((c for c in containers if c.get("name") == cname), containers[0])
        exit_code = cont.get("exitCode")
        stopped_reason = desc.get("stoppedReason") or ""

        task_id = task_arn.rsplit("/", 1)[-1]
        out = self._fetch_run_logs(ctx, service, cname, task_id)

        if exit_code is None:
            # No exit code = container never ran to completion (image pull
            # failure, OOM kill, stopped before start). Surface as failure.
            return ExecResult(
                exit_code=1,
                stdout=out,
                stderr=stopped_reason or "task stopped without an exit code",
            )
        return ExecResult(
            exit_code=int(exit_code),
            stdout=out,
            stderr=stopped_reason if exit_code != 0 else "",
        )

    def _fetch_run_logs(
        self,
        ctx: DeployContext,
        service: str,
        container: str,
        task_id: str,
        tail: int = 500,
    ) -> str:
        """Best-effort fetch of a one-off task's awslogs stream.

        rc task defs log to ``/ecs/<project>`` with stream
        ``<service>/<container>/<task-id>`` (awslogs-stream-prefix = service
        name). Returns "" on any failure — logs are a convenience, never a
        reason to fail the run.
        """
        try:
            session = self.session_factory(ctx)
            client = session.client("logs")
            events = client.get_log_events(
                logGroupName=f"/ecs/{ctx.project}",
                logStreamName=f"{service}/{container}/{task_id}",
                limit=tail,
                startFromHead=True,
            ).get("events", [])
            return "".join(e["message"] + "\n" for e in events)
        except Exception:  # noqa: BLE001
            return ""

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
                services=[_ecs_service_name(ctx, service)],
            )
            services_list = svc_desc.get("services") or []
            if services_list:
                current_td_arn = services_list[0].get("taskDefinition")
        except Exception:  # noqa: BLE001
            current_td_arn = None
        while True:
            tasks = (
                ecs_client.list_tasks(
                    cluster=cluster,
                    serviceName=_ecs_service_name(ctx, service),
                    desiredStatus="RUNNING",
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
        # Only pin AWS_PROFILE when the profile actually resolves — otherwise
        # the aws CLI subprocess hard-fails under OIDC/env creds (rc-run/exec
        # in CI). Mirrors _default_session_factory's ProfileNotFound fallback.
        if _profile_is_resolvable(profile):
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
        self,
        ecs_cfg: dict,
        ec2_demands: list[EC2TaskDemand],
        eni_trunking: Optional[bool] = None,
    ) -> dict:
        """Merge user-supplied ec2_capacity with auto-sized defaults.

        User-supplied instance_type, min/max/desired all take precedence.
        Auto-sizing fills in gaps.

        ``eni_trunking`` is what preflight resolved about the account
        (True/False/None-for-unchecked). It is applied to the SHAPE via
        InstanceShape.with_trunking() rather than threaded as a flag through
        the sizing functions, so auto_size/measure_fleet/
        check_fixed_shape_capacity keep working on one consistent notion of
        "task ENI slots" and no call site has to know trunking exists.
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
            # Thread the user's explicit `max` through as auto_size()'s
            # max_cap ceiling (default 10 when unset) rather than only
            # applying it after the fact: auto_size() can now raise
            # ValueError telling the user to "raise ec2_capacity.max" when
            # declared EC2 task demand needs more instances than fit (see
            # rc-e5u.25.1's ENI-density constraint, which made this
            # reachable with an ordinary high-replica-count config) — that
            # advice has to actually change what auto_size() computes, or
            # it's a dead end.
            try:
                sizing = auto_size(
                    ec2_demands,
                    max_cap=user_cfg.get("max", 10),
                    # rc-anl6: opt-in. Off (default) sizes for steady state,
                    # exactly as before, and the fleet-pressure warning below
                    # reports what a roll would need. On sizes for peak
                    # rolling-deploy demand instead — no PENDING window, at
                    # typically 1.5-2x the instances.
                    size_for_rolling_deploy=bool(
                        user_cfg.get("size_for_rolling_deploy", False)
                    ),
                )
            except ValueError as exc:
                # auto_size() raises bare ValueError (it's a standalone,
                # provider-agnostic module with no ProviderConfigError
                # dependency); every other user-input validation failure in
                # this method raises ProviderConfigError, so re-wrap here
                # rather than let a ValueError leak past this method's
                # otherwise-consistent error type.
                raise ProviderConfigError(str(exc)) from exc
            instance_type = sizing.instance_type
            min_size = user_cfg.get("min", sizing.min_size)
            desired_size = user_cfg.get("desired", sizing.desired_size)
            max_size = user_cfg.get("max", sizing.max_size)
        else:
            # User picked the shape; ASG numbers come from config (with sane defaults).
            min_size = user_cfg.get("min", 1)
            desired_size = user_cfg.get("desired", 1)
            max_size = user_cfg.get("max", 3)
            # auto_size() never runs on this branch, so nothing else checks
            # whether desired_size instances of this shape can actually host
            # the declared EC2 task demand (cpu/memory/ENI) -- rc-e5u.25.10.
            # Only for instance types rc has verified numbers for
            # (KNOWN_INSTANCE_SHAPES); an unlisted/unverified type is not
            # modeled and skips this, same as a custom auto_size() ladder.
            known_shape = KNOWN_INSTANCE_SHAPES.get(instance_type)
            if known_shape is not None:
                effective = self._effective_shape(known_shape, eni_trunking)
                try:
                    check_fixed_shape_capacity(
                        effective,
                        ec2_demands,
                        desired_size,
                        trunking_state=_trunking_state(eni_trunking),
                        region=ecs_cfg.get("region"),
                    )
                except ValueError as exc:
                    raise ProviderConfigError(str(exc)) from exc

        self._check_trunking_assertion(user_cfg, instance_type, eni_trunking)
        resolved = {
            "instance_type": instance_type,
            "min_size": min_size,
            "desired_size": desired_size,
            "max_size": max_size,
            "capacity_type": capacity_type,
            "spot_weight": user_cfg.get("spot_weight", 3),
            **_resolve_imds_options(user_cfg),
            **_resolve_root_volume_options(user_cfg),
            **_resolve_managed_scaling(user_cfg),
        }
        self._warn_on_shared_root_volume(resolved, ec2_demands, eni_trunking)
        self._warn_on_ec2_fleet_pressure(
            resolved, ec2_demands, eni_trunking, ecs_cfg.get("region")
        )
        return resolved

    @staticmethod
    def _effective_shape(shape, eni_trunking: Optional[bool]):
        """The shape as it behaves under the resolved trunking state.

        with_trunking() is a no-op for an ineligible type, so this is safe to
        apply unconditionally -- which is why the default t3 ladder needs no
        special-casing anywhere.
        """
        return shape.with_trunking() if eni_trunking else shape

    def _check_trunking_assertion(
        self, user_cfg: dict, instance_type: str, eni_trunking: Optional[bool]
    ) -> None:
        """Reject `eni_trunking: true` on a family AWS cannot trunk (ask 2).

        Silently accepting it would size the fleet against a ceiling that
        does not exist and leave tasks PENDING forever. Correctly rejects the
        entire t3/t3a/t4g ladder, and m5.metal / c5.metal, which AWS names in
        its not-supported list despite their families being eligible.
        """
        if user_cfg.get("eni_trunking") is not True:
            return
        shape = KNOWN_INSTANCE_SHAPES.get(instance_type)
        if shape is None:
            self._warn(
                f"ec2_capacity.eni_trunking: true was asserted for "
                f"{instance_type!r}, which rc has no verified numbers for. rc "
                f"is taking your word for it and skipping the ENI density "
                f"check for this shape; confirm the type appears in AWS's "
                f"eni-trunking-supported-instance-types tables."
            )
            return
        if not shape.trunking_supported:
            raise ProviderConfigError(
                f"ec2_capacity.eni_trunking: true, but AWS does not support "
                f"ENI trunking on {instance_type}. It is absent from the "
                f"eni-trunking-supported-instance-types tables (the entire "
                f"t3/t3a/t4g burstable family is, and m5.metal / c5.metal are "
                f"named in the explicit not-supported list). Sizing against a "
                f"ceiling that does not exist would leave tasks PENDING "
                f"forever. Pick a trunking-eligible shape (m5/m6i/c5/c6i, "
                f"non-metal) or drop the assertion."
            )
        if eni_trunking is not True:  # pragma: no cover - defensive
            return

    def _warn_on_ec2_one_off_capacity(
        self,
        ctx: DeployContext,
        resolved: dict,
        ec2_demands: list[EC2TaskDemand],
        default_launch_type: str,
    ) -> None:
        """Flag one-off tasks competing for EC2 capacity nothing sized for.

        rc-fg83 fixed `rc run` launching at all on an EC2 stack. This is the
        hazard that survives the fix: on Fargate a one-off task always has
        somewhere to run, but on EC2 it needs a free slot on an instance that
        already exists. auto_size() models declared SERVICES only, so a
        `mode: task` lifecycle hook -- the migrate-then-roll ordering rc
        itself recommends -- is invisible to sizing. If the fleet is full the
        one-off sits PENDING while the ASG boots an instance, and the deploy
        step times out.

        Statically detectable, which is the point: today the only signal is a
        deploy that fails after terraform has already converged.
        """
        hooks: list[str] = []
        for name, spec in sorted((ctx.services or {}).items()):
            if (spec.launch_type or default_launch_type) != "EC2":
                continue
            for hook_name, hook in (getattr(spec, "lifecycle", None) or {}).items():
                if str((hook or {}).get("mode", "exec")).lower() == "task":
                    hooks.append(f"{name}.{hook_name}")
        if not hooks:
            return
        shape = KNOWN_INSTANCE_SHAPES.get(resolved["instance_type"])
        slots = ""
        if shape is not None:
            eni_slots = self._effective_shape(
                shape, getattr(ctx, "eni_trunking", None)
            ).task_eni_slots
            if eni_slots is not None:
                capacity = eni_slots * int(resolved.get("desired_size") or 1)
                declared = sum(t.replicas for t in ec2_demands)
                slots = (
                    f" This fleet holds about {capacity} awsvpc task(s) against "
                    f"{declared} declared, leaving roughly "
                    f"{max(0, capacity - declared)} slot(s) for one-offs."
                )
        self._warn(
            f"EC2 one-off tasks: lifecycle hook(s) {', '.join(hooks)} run as "
            f"their own ECS task (mode: task), and auto-sizing models declared "
            f"services only — a one-off needs a free slot on an instance that "
            f"already exists.{slots} On Fargate a one-off always has somewhere "
            f"to run; on EC2 a full fleet leaves it PENDING while the ASG boots, "
            f"and a migrate-before-roll step that times out is a deploy that "
            f"cannot safely ship a migration. Keep headroom via "
            f"ec2_capacity.desired, or run migrations with `mode: exec` into a "
            f"running task."
        )

    def _warn_on_ec2_fleet_pressure(
        self,
        resolved: dict,
        ec2_demands: list[EC2TaskDemand],
        eni_trunking: Optional[bool] = None,
        region: Optional[str] = None,
    ) -> None:
        """Flag EC2 fleets that are correct at rest and wrong during a deploy.

        rc-anl6. auto_size() picks the smallest shape that fits the largest
        single task and sizes for STEADY STATE. ECS permits up to 200% task
        duplication during a rolling deploy, so the fleet it produces can be
        right at rest and badly undersized at the only moment that matters.
        Three separate findings, all cheap and all silent before this:

        1. A service whose cpu request meets or exceeds the whole instance's
           CPU. debuggai-api's celery-worker requests exactly 2048 units and
           the smallest fitting shape, t3.large, is exactly 2048. Both facts
           are individually reasonable; together they mean one task per
           instance, zero binpacking, and therefore Fargate economics on an
           EC2 bill — which defeats the entire reason for choosing EC2.
        2. A fleet that binpacks nothing at all (tasks == instances), the
           general form of (1).
        3. A roll that needs more instances than the fleet holds. Managed
           scaling (capacity.tf.j2) will scale the ASG out, but EC2 boot plus
           ECS agent registration takes minutes, and tasks sit PENDING
           throughout. For this stack that state is not hypothetical —
           it is the production bug celery-worker's replicas: 3 exists to
           fix.

        Silently skips instance types rc has no verified numbers for, same
        "not modeled" convention as InstanceShape.max_enis=None.
        """
        if not ec2_demands:
            return
        base_shape = KNOWN_INSTANCE_SHAPES.get(resolved["instance_type"])
        if base_shape is None:
            return
        shape = self._effective_shape(base_shape, eni_trunking)
        pressure = measure_fleet(shape, ec2_demands)
        self._warn_on_eni_bound_fleet(pressure, eni_trunking, region)
        instance_cpu = shape.vcpu * 1024
        desired = resolved["desired_size"]
        max_size = resolved["max_size"]

        if pressure.cpu_saturating_tasks:
            names = ", ".join(pressure.cpu_saturating_tasks)
            self._warn(
                f"EC2 sizing: service(s) {names} request at least "
                f"{instance_cpu} CPU units, which is the ENTIRE CPU of a "
                f"{shape.name} ({shape.vcpu} vCPU). One task fills one "
                f"instance, so nothing binpacks and this fleet costs EC2 "
                f"prices for Fargate density — the cost premise of running "
                f"on EC2 does not hold. Either lower those services' cpu, or "
                f"set provider_config.ecs.ec2_capacity.instance_type to a "
                f"larger shape so more than one task fits per instance."
            )
        elif not pressure.binpacks:
            self._warn(
                f"EC2 sizing: {pressure.steady_task_count} task(s) across "
                f"{pressure.steady_instances} {shape.name} instance(s) — no "
                f"binpacking at all. Paying for EC2 capacity is only cheaper "
                f"than Fargate when instances host several tasks; at one task "
                f"per instance there is no saving to collect. Consider a "
                f"larger instance_type, or staying on FARGATE."
            )

        if pressure.peak_instances > desired:
            headroom = (
                f"and its max of {max_size} cannot reach that either"
                if pressure.peak_instances > max_size
                else f"so the ASG must scale out from {desired} to "
                f"{pressure.peak_instances} mid-deploy"
            )
            self._warn(
                f"EC2 sizing models steady state, not deploys: this stack "
                f"runs {pressure.steady_task_count} task(s) at rest but ECS "
                f"permits up to {pressure.peak_task_count} while a rolling "
                f"deploy is in flight (deployment_maximum_percent). That "
                f"peak needs ~{pressure.peak_instances} {shape.name} "
                f"instance(s) against a desired of {desired}, {headroom}. "
                f"EC2 boot plus ECS agent registration takes minutes, and "
                f"tasks sit PENDING for the whole window — the "
                f"'worker did not pick up the task' failure mode. Set "
                f"provider_config.ecs.ec2_capacity.size_for_rolling_deploy: "
                f"true to have rc size for the peak instead (it costs the "
                f"extra instances continuously), pin "
                f"ec2_capacity.desired to {pressure.peak_instances} yourself, "
                f"or accept the PENDING window deliberately."
            )

    def _warn_on_shared_root_volume(
        self,
        resolved: dict,
        ec2_demands: list[EC2TaskDemand],
        eni_trunking: Optional[bool] = None,
    ) -> None:
        """Flag EC2 tasks sharing the AMI's default root volume (rc-hbjb).

        Only fires when more than one task can land on an instance, because
        that is where the hazard actually is: the disk is shared, so a task
        that fills it takes its NEIGHBOURS down, not just itself. A stack
        that binpacks one task per instance has a private 30 GiB and nothing
        to warn about.

        Density comes from the ENI ceiling rc already models (max_enis minus
        the instance's own primary ENI) — for an awsvpc task that is a hard
        per-instance limit, so it is the honest upper bound on neighbours.
        Unmodeled instance types report nothing rather than guess.
        """
        if resolved.get("root_volume_size") is not None:
            return
        shape = KNOWN_INSTANCE_SHAPES.get(resolved["instance_type"])
        if shape is None:
            return
        shape = self._effective_shape(shape, eni_trunking)
        eni_ceiling = shape.task_eni_slots
        if eni_ceiling is None:
            return
        total_tasks = sum(t.replicas for t in ec2_demands)
        # With trunking the ENI ceiling stops being the real density (m5.xlarge
        # allows 20 tasks but CPU/memory bind long before that), so bound it by
        # what the sized fleet can actually pack. Otherwise this would report
        # "30 GiB shared 20 ways" for a fleet that will never place 20 tasks on
        # one instance.
        desired = max(1, int(resolved.get("desired_size") or 1))
        tasks_per_instance = min(eni_ceiling, math.ceil(total_tasks / desired))
        neighbours = min(tasks_per_instance, total_tasks)
        if neighbours < 2:
            return
        per_task = ECS_AMI_DEFAULT_ROOT_VOLUME_GIB // neighbours
        self._warn(
            f"EC2 launch type: no ec2_capacity.root_volume_size is set, so "
            f"every {resolved['instance_type']} container instance takes the "
            f"ECS-optimized AMI's default "
            f"{ECS_AMI_DEFAULT_ROOT_VOLUME_GIB} GiB root volume — and that "
            f"one disk is SHARED by every task binpacked onto it. This shape "
            f"holds up to {tasks_per_instance} awsvpc tasks, so a full "
            f"instance leaves roughly {per_task} GiB of scratch per task, not "
            f"{ECS_AMI_DEFAULT_ROOT_VOLUME_GIB}. Unlike Fargate's per-task "
            f"ephemeral_storage this space is not private: one task filling "
            f"the disk takes its neighbours down with it. Set "
            f"provider_config.ecs.ec2_capacity.root_volume_size (GiB) to size "
            f"it deliberately."
        )


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


_TF_GITIGNORE = """# Managed by remote-compose. Terraform volatile artifacts — never commit.
.terraform/
*.tfstate
*.tfstate.backup
*.tfplan
crash.log
crash.*.log
"""


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
