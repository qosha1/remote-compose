"""Everything rc can check about a deploy BEFORE it touches anything.

rc-g3jy. Moving debuggai-api-prod off ``--no-state`` onto full terraform cost
three failed production deploys in a row, and every one was knowable up
front: no terraform binary, an S3 403 on the state object, and an
``aws_profile`` that doesn't resolve on an OIDC runner. Each failed safe, but
each burned a full deploy cycle, and the pattern was "rc discovers a missing
prerequisite one deploy at a time". Diffing the deploy role against a stack
that already applies terraform successfully then found 36 MORE missing IAM
actions -- every one of which would have been another serial failure.

rc has the information to report all of that at once, because it has just
rendered the terraform and therefore knows exactly which resource types it is
about to create, read and modify.

Design notes:

* The required action set is derived from the EMITTED ``.tf`` files, not from
  a terraform plan. A plan needs state access, which is one of the things
  being checked -- deriving from the plan would make the IAM check
  unreachable in exactly the situation it exists for.
* Everything here is advisory. ``iam:SimulatePrincipalPolicy`` is itself a
  permission the caller may lack, and it does not fully evaluate SCPs or
  permission boundaries, so a clean report is evidence and never proof.
* Unmodeled resource types are REPORTED as unchecked rather than silently
  passing -- the same "not modeled" convention as ``InstanceShape.max_enis =
  None`` in autosize.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# Status values for a single check.
OK = "ok"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"

_STATUS_MARK = {OK: "✓", FAIL: "✗", WARN: "!", SKIP: "-"}


@dataclass
class PreflightCheck:
    name: str
    status: str
    detail: str
    # What to do about it. Empty for passing checks.
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


@dataclass
class PreflightReport:
    checks: list[PreflightCheck] = field(default_factory=list)
    # IAM actions the principal was shown NOT to have, in a stable order.
    missing_actions: list[str] = field(default_factory=list)
    # Resource types rc emits but has no action mapping for.
    unmodeled_resource_types: list[str] = field(default_factory=list)
    # Which principal the IAM simulation actually ran against, and whether it
    # is the configured deploy principal rather than whoever happens to be
    # logged in (rc-zu1x).
    checked_principal: str = ""
    checked_deploy_principal: bool = False

    def add(self, check: PreflightCheck) -> None:
        self.checks.append(check)

    @property
    def ok(self) -> bool:
        return not any(c.blocking for c in self.checks)

    def render_table(self) -> str:
        """Every finding at once — the whole point of this module.

        Deliberately not "first failure wins": the value is that a user fixes
        one round of problems rather than discovering them serially, one
        failed production deploy each.
        """
        if not self.checks:
            return "  (no checks ran)"
        width = max(len(c.name) for c in self.checks)
        lines = []
        for c in self.checks:
            lines.append(
                f"  {_STATUS_MARK.get(c.status, '?')} {c.name.ljust(width)}  "
                f"{c.detail}"
            )
            if c.remedy:
                for remedy_line in c.remedy.splitlines():
                    lines.append(f"      {remedy_line}")
        return "\n".join(lines)

    def policy_fragment(self) -> str:
        """A paste-ready IAM policy statement granting the missing actions.

        Resource "*" deliberately: this is the fastest path from "my deploy
        is broken" to "my deploy runs", and narrowing it is a judgement call
        about the account's other principals that rc cannot make. The comment
        in the rendered fragment says so.
        """
        if not self.missing_actions:
            return ""
        return json.dumps(
            {
                "Sid": "RemoteComposeDeployMissingActions",
                "Effect": "Allow",
                "Action": sorted(self.missing_actions),
                "Resource": "*",
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Terraform resource type -> IAM actions
# ---------------------------------------------------------------------------
#
# What terraform needs to CREATE, READ, UPDATE and DELETE each resource type
# rc's templates emit, plus the tagging calls the provider makes because
# providers.tf.j2 sets default_tags on everything.
#
# This is a model, not a derivation from AWS's own data -- there is no
# machine-readable mapping from a terraform resource type to its IAM actions.
# It is therefore explicitly incomplete-by-design in one direction only:
# every entry here is an action terraform really does call, and a type
# missing from this table is reported as UNCHECKED rather than assumed fine
# (see PreflightReport.unmodeled_resource_types). Add types as rc's templates
# grow; `grep -ho '^resource "[a-z0-9_]*"' templates/*.j2 | sort -u` lists
# what needs covering.
RESOURCE_TYPE_ACTIONS: dict[str, list[str]] = {
    # --- ECS ---------------------------------------------------------------
    "aws_ecs_cluster": [
        "ecs:CreateCluster",
        "ecs:DeleteCluster",
        "ecs:DescribeClusters",
        "ecs:TagResource",
        "ecs:UntagResource",
    ],
    "aws_ecs_cluster_capacity_providers": [
        "ecs:DescribeClusters",
        "ecs:PutClusterCapacityProviders",
    ],
    "aws_ecs_capacity_provider": [
        "ecs:CreateCapacityProvider",
        "ecs:DeleteCapacityProvider",
        "ecs:DescribeCapacityProviders",
        "ecs:TagResource",
    ],
    "aws_ecs_service": [
        "ecs:CreateService",
        "ecs:DeleteService",
        "ecs:DescribeServices",
        "ecs:UpdateService",
        "ecs:TagResource",
    ],
    "aws_ecs_task_definition": [
        "ecs:DeregisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:RegisterTaskDefinition",
        # Terraform passes the task + execution roles to ECS on register.
        "iam:PassRole",
    ],
    # --- ECR ---------------------------------------------------------------
    "aws_ecr_repository": [
        "ecr:CreateRepository",
        "ecr:DeleteRepository",
        "ecr:DescribeRepositories",
        "ecr:ListTagsForResource",
        "ecr:TagResource",
    ],
    "aws_ecr_lifecycle_policy": [
        "ecr:DeleteLifecyclePolicy",
        "ecr:GetLifecyclePolicy",
        "ecr:PutLifecyclePolicy",
    ],
    # --- Networking --------------------------------------------------------
    "aws_vpc": [
        "ec2:CreateVpc",
        "ec2:DeleteVpc",
        "ec2:DescribeVpcs",
        "ec2:DescribeVpcAttribute",
        "ec2:ModifyVpcAttribute",
        "ec2:CreateTags",
        "ec2:DeleteTags",
    ],
    "aws_subnet": [
        "ec2:CreateSubnet",
        "ec2:DeleteSubnet",
        "ec2:DescribeSubnets",
        "ec2:ModifySubnetAttribute",
        "ec2:CreateTags",
    ],
    "aws_internet_gateway": [
        "ec2:AttachInternetGateway",
        "ec2:CreateInternetGateway",
        "ec2:DeleteInternetGateway",
        "ec2:DescribeInternetGateways",
        "ec2:DetachInternetGateway",
        "ec2:CreateTags",
    ],
    "aws_nat_gateway": [
        "ec2:CreateNatGateway",
        "ec2:DeleteNatGateway",
        "ec2:DescribeNatGateways",
        "ec2:CreateTags",
    ],
    "aws_eip": [
        "ec2:AllocateAddress",
        "ec2:DescribeAddresses",
        "ec2:ReleaseAddress",
        "ec2:CreateTags",
    ],
    "aws_route_table": [
        "ec2:CreateRouteTable",
        "ec2:DeleteRouteTable",
        "ec2:DescribeRouteTables",
        "ec2:CreateTags",
    ],
    "aws_route": [
        "ec2:CreateRoute",
        "ec2:DeleteRoute",
        "ec2:DescribeRouteTables",
    ],
    "aws_route_table_association": [
        "ec2:AssociateRouteTable",
        "ec2:DescribeRouteTables",
        "ec2:DisassociateRouteTable",
    ],
    "aws_security_group": [
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:DescribeSecurityGroups",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:CreateTags",
    ],
    # Emitted by network_declared.tf.j2 as
    # `aws_vpc_security_group_{{ rule.direction }}_rule` -- the type NAME is
    # Jinja-interpolated, which is why a scan of the .j2 templates never saw
    # these two (rc-zu1x). Same actions as the inline ingress/egress blocks on
    # aws_security_group.
    "aws_vpc_security_group_ingress_rule": [
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:DescribeSecurityGroupRules",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:CreateTags",
    ],
    "aws_vpc_security_group_egress_rule": [
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:DescribeSecurityGroupRules",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:CreateTags",
    ],
    "aws_vpc_endpoint": [
        "ec2:CreateVpcEndpoint",
        "ec2:DeleteVpcEndpoints",
        "ec2:DescribeVpcEndpoints",
        "ec2:ModifyVpcEndpoint",
        "ec2:CreateTags",
    ],
    "aws_vpc_dhcp_options": [
        "ec2:CreateDhcpOptions",
        "ec2:DeleteDhcpOptions",
        "ec2:DescribeDhcpOptions",
        "ec2:CreateTags",
    ],
    "aws_vpc_dhcp_options_association": [
        "ec2:AssociateDhcpOptions",
        "ec2:DescribeDhcpOptions",
    ],
    # --- Load balancing ----------------------------------------------------
    "aws_lb": [
        "elasticloadbalancing:AddTags",
        "elasticloadbalancing:CreateLoadBalancer",
        "elasticloadbalancing:DeleteLoadBalancer",
        "elasticloadbalancing:DescribeLoadBalancerAttributes",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTags",
        "elasticloadbalancing:ModifyLoadBalancerAttributes",
    ],
    "aws_lb_target_group": [
        "elasticloadbalancing:AddTags",
        "elasticloadbalancing:CreateTargetGroup",
        "elasticloadbalancing:DeleteTargetGroup",
        "elasticloadbalancing:DescribeTargetGroupAttributes",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:ModifyTargetGroup",
        "elasticloadbalancing:ModifyTargetGroupAttributes",
    ],
    "aws_lb_listener": [
        "elasticloadbalancing:CreateListener",
        "elasticloadbalancing:DeleteListener",
        "elasticloadbalancing:DescribeListeners",
        "elasticloadbalancing:ModifyListener",
    ],
    "aws_lb_listener_rule": [
        "elasticloadbalancing:CreateRule",
        "elasticloadbalancing:DeleteRule",
        "elasticloadbalancing:DescribeRules",
        "elasticloadbalancing:ModifyRule",
    ],
    # --- IAM ---------------------------------------------------------------
    "aws_iam_role": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:TagRole",
        "iam:UpdateAssumeRolePolicy",
    ],
    "aws_iam_role_policy": [
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:PutRolePolicy",
    ],
    "aws_iam_role_policy_attachment": [
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies",
    ],
    "aws_iam_instance_profile": [
        "iam:AddRoleToInstanceProfile",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile",
        "iam:TagInstanceProfile",
    ],
    # --- EC2 capacity ------------------------------------------------------
    "aws_launch_template": [
        "ec2:CreateLaunchTemplate",
        "ec2:CreateLaunchTemplateVersion",
        "ec2:DeleteLaunchTemplate",
        "ec2:DescribeLaunchTemplates",
        "ec2:DescribeLaunchTemplateVersions",
        "ec2:CreateTags",
        "ec2:RunInstances",
    ],
    "aws_autoscaling_group": [
        "autoscaling:CreateAutoScalingGroup",
        "autoscaling:CreateOrUpdateTags",
        "autoscaling:DeleteAutoScalingGroup",
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeScalingActivities",
        "autoscaling:UpdateAutoScalingGroup",
        "iam:CreateServiceLinkedRole",
    ],
    # --- EFS ---------------------------------------------------------------
    "aws_efs_file_system": [
        "elasticfilesystem:CreateFileSystem",
        "elasticfilesystem:DeleteFileSystem",
        "elasticfilesystem:DescribeFileSystems",
        "elasticfilesystem:DescribeLifecycleConfiguration",
        "elasticfilesystem:PutLifecycleConfiguration",
        "elasticfilesystem:TagResource",
    ],
    "aws_efs_mount_target": [
        "elasticfilesystem:CreateMountTarget",
        "elasticfilesystem:DeleteMountTarget",
        "elasticfilesystem:DescribeMountTargets",
        "elasticfilesystem:DescribeMountTargetSecurityGroups",
    ],
    "aws_efs_access_point": [
        "elasticfilesystem:CreateAccessPoint",
        "elasticfilesystem:DeleteAccessPoint",
        "elasticfilesystem:DescribeAccessPoints",
        "elasticfilesystem:TagResource",
    ],
    # --- Observability -----------------------------------------------------
    "aws_cloudwatch_log_group": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:DescribeLogGroups",
        "logs:ListTagsForResource",
        "logs:PutRetentionPolicy",
        # rc-g3jy: log-group TAGGING is separate from creation and was one of
        # the 36 actions the deploy role was missing.
        "logs:TagResource",
        "logs:UntagResource",
    ],
    # --- Secrets -----------------------------------------------------------
    "aws_secretsmanager_secret": [
        "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetResourcePolicy",
        "secretsmanager:TagResource",
    ],
    "aws_secretsmanager_secret_version": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecretVersionStage",
    ],
    # --- DNS / discovery ---------------------------------------------------
    "aws_route53_record": [
        "route53:ChangeResourceRecordSets",
        "route53:GetChange",
        "route53:GetHostedZone",
        "route53:ListResourceRecordSets",
    ],
    "aws_acm_certificate": [
        "acm:AddTagsToCertificate",
        "acm:DeleteCertificate",
        "acm:DescribeCertificate",
        "acm:ListTagsForCertificate",
        "acm:RequestCertificate",
    ],
    "aws_acm_certificate_validation": [
        "acm:DescribeCertificate",
    ],
    "aws_service_discovery_private_dns_namespace": [
        "route53:CreateHostedZone",
        "route53:DeleteHostedZone",
        "servicediscovery:CreatePrivateDnsNamespace",
        "servicediscovery:DeleteNamespace",
        "servicediscovery:GetNamespace",
        "servicediscovery:GetOperation",
        "servicediscovery:ListNamespaces",
        "servicediscovery:TagResource",
    ],
    "aws_service_discovery_service": [
        "servicediscovery:CreateService",
        "servicediscovery:DeleteService",
        "servicediscovery:GetService",
        "servicediscovery:ListServices",
        "servicediscovery:TagResource",
    ],
    # --- S3 (backup bucket) ------------------------------------------------
    "aws_s3_bucket": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketTagging",
        "s3:ListBucket",
        "s3:PutBucketTagging",
    ],
    "aws_s3_bucket_versioning": [
        "s3:GetBucketVersioning",
        "s3:PutBucketVersioning",
    ],
    "aws_s3_bucket_lifecycle_configuration": [
        "s3:GetLifecycleConfiguration",
        "s3:PutLifecycleConfiguration",
    ],
    "aws_s3_bucket_public_access_block": [
        "s3:GetBucketPublicAccessBlock",
        "s3:PutBucketPublicAccessBlock",
    ],
    "aws_s3_bucket_server_side_encryption_configuration": [
        "s3:GetEncryptionConfiguration",
        "s3:PutEncryptionConfiguration",
    ],
}

# Terraform DATA sources rc's templates read. Read-only, but a missing one
# fails the plan just as hard as a missing write permission.
DATA_SOURCE_ACTIONS: dict[str, list[str]] = {
    "aws_availability_zones": ["ec2:DescribeAvailabilityZones"],
    "aws_vpc": ["ec2:DescribeVpcs"],
    "aws_lb": ["elasticloadbalancing:DescribeLoadBalancers"],
    "aws_lb_listener": ["elasticloadbalancing:DescribeListeners"],
    "aws_route53_zone": ["route53:ListHostedZones", "route53:GetHostedZone"],
    "aws_ssm_parameter": ["ssm:GetParameter", "ssm:GetParameters"],
}

# Always needed, regardless of what the module declares: terraform reads the
# caller identity and the region on every run.
BASELINE_ACTIONS = [
    "sts:GetCallerIdentity",
]

_RESOURCE_RE = re.compile(r'^resource\s+"([a-z0-9_]+)"', re.MULTILINE)
_DATA_RE = re.compile(r'^data\s+"([a-z0-9_]+)"', re.MULTILINE)


def scan_terraform_dir(tf_dir: Path) -> tuple[set[str], set[str]]:
    """Return ``(resource_types, data_source_types)`` declared in ``tf_dir``.

    Reads the EMITTED HCL rather than a plan, deliberately: a plan requires
    state access, which is one of the things preflight exists to check. This
    only needs the files rc just wrote.
    """
    resources: set[str] = set()
    data_sources: set[str] = set()
    for tf_file in sorted(Path(tf_dir).glob("*.tf")):
        try:
            text = tf_file.read_text()
        except OSError:
            continue
        resources.update(_RESOURCE_RE.findall(text))
        data_sources.update(_DATA_RE.findall(text))
    return resources, data_sources


def derive_required_actions(
    resource_types: Iterable[str],
    data_source_types: Iterable[str] = (),
    backend_cfg: Optional[dict] = None,
) -> tuple[list[str], list[str]]:
    """Map declared types to the IAM actions terraform will call.

    Returns ``(actions, unmodeled_types)``, both sorted. ``unmodeled_types``
    are resource types with no entry in RESOURCE_TYPE_ACTIONS: rc reports
    them as unchecked rather than passing them silently.
    """
    actions: set[str] = set(BASELINE_ACTIONS)
    unmodeled: set[str] = set()
    for rtype in resource_types:
        mapped = RESOURCE_TYPE_ACTIONS.get(rtype)
        if mapped is None:
            unmodeled.add(rtype)
            continue
        actions.update(mapped)
    for dtype in data_source_types:
        actions.update(DATA_SOURCE_ACTIONS.get(dtype, []))
    # The s3 backend's own state + lock access. Not derivable from any
    # resource type -- the state bucket is not something the module creates.
    if (backend_cfg or {}).get("type") == "s3":
        actions.update(BACKEND_STATE_ACTIONS)
        if backend_cfg.get("dynamodb_table"):
            actions.update(BACKEND_LOCK_ACTIONS)
    return sorted(actions), sorted(unmodeled)


def group_by_service(actions: Iterable[str]) -> dict[str, list[str]]:
    """Group ``service:Action`` strings by their service prefix."""
    grouped: dict[str, list[str]] = {}
    for action in sorted(actions):
        service = action.split(":", 1)[0]
        grouped.setdefault(service, []).append(action)
    return grouped


def canonical_principal_arn(caller_arn: str) -> Optional[str]:
    """Convert an STS caller ARN into one SimulatePrincipalPolicy accepts.

    ``sts:GetCallerIdentity`` under an assumed role returns
    ``arn:aws:sts::123456789012:assumed-role/RoleName/session-name``.
    ``iam:SimulatePrincipalPolicy`` requires an IAM entity ARN --
    ``arn:aws:iam::123456789012:role/RoleName``. Passing the session ARN
    straight through makes the simulation fail (or, worse, report against
    nothing), which on an OIDC runner is EVERY run.

    A user ARN passes through unchanged. Anything else (a root ARN, an
    assumed federated session) returns None -- the caller reports "could not
    simulate" rather than guessing at an ARN.
    """
    if not caller_arn:
        return None
    parts = caller_arn.split(":")
    if len(parts) < 6 or parts[2] not in ("iam", "sts"):
        return None
    account = parts[4]
    resource = parts[5]
    if resource.startswith("assumed-role/"):
        # assumed-role/<RoleName>/<session> -> role/<RoleName>. The role name
        # itself never contains "/", but a role in a PATH does
        # (assumed-role/ signals the name only, paths are not echoed here),
        # so splitting on the first two segments is correct.
        segments = resource.split("/")
        if len(segments) < 2 or not segments[1]:
            return None
        return f"arn:aws:iam::{account}:role/{segments[1]}"
    if resource.startswith("role/") or resource.startswith("user/"):
        return f"arn:aws:iam::{account}:{resource}"
    return None


# ---------------------------------------------------------------------------
# Resource-scoped simulation (rc-zu1x)
# ---------------------------------------------------------------------------
#
# SimulatePrincipalPolicy applies ``ResourceArns`` to EVERY action in the
# call. Verified live against a real least-privileged deploy role
# (2026-08-19):
#
#   --action-names iam:CreateInstanceProfile ecs:RegisterTaskDefinition \
#     --resource-arns arn:...:instance-profile/debuggai-api-ec2-instance
#   -> iam:CreateInstanceProfile   allowed
#      ecs:RegisterTaskDefinition  implicitDeny   <- evaluated against an
#                                                    instance-profile ARN
#
# So actions cannot be batched freely once a resource is supplied. Equally,
# simulating a resource-scoped statement against the default "*" reports a
# denial that is not real: the same role returned implicitDeny for
# iam:CreateRole, iam:CreateInstanceProfile, iam:AddRoleToInstanceProfile,
# iam:DeleteInstanceProfile, s3:PutObject and dynamodb:PutItem against "*"
# and allowed for every one of them against the concrete ARNs it will
# actually touch.
#
# That direction is the dangerous one: rc would tell a correctly
# least-privileged stack it was missing IAM, and the natural fix an operator
# reaches for is widening those statements to Resource: "*" -- the tool would
# actively push people from scoped policies toward admin-shaped ones, which
# is worse than the gap it replaces.
#
# Hence: group actions by the resource class they act on, simulate each group
# against the concrete ARNs rc's own templates produce, and simulate the
# remainder against "*" while MARKING those results as possibly-false.

# What terraform's own s3 backend calls, independent of any resource the
# module declares. These never appear in RESOURCE_TYPE_ACTIONS because the
# state bucket and lock table are not resources rc creates -- which is why
# the first version of this check omitted them entirely even though every
# stateful deploy needs them (they were among the denials found by hand).
BACKEND_STATE_ACTIONS = [
    "s3:DeleteObject",
    "s3:GetObject",
    "s3:ListBucket",
    "s3:PutObject",
]
BACKEND_LOCK_ACTIONS = [
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
]

# Action -> resource class, by explicit membership rather than by service
# prefix. Prefix matching would be wrong in both directions here: s3 actions
# for the BACKUP bucket (aws_s3_bucket in the module) must not be evaluated
# against the STATE bucket's ARN, and iam:CreateServiceLinkedRole acts on a
# service-linked role rc does not name.
_INSTANCE_PROFILE_MARKER = "InstanceProfile"
_UNSCOPED_IAM_ACTIONS = {"iam:CreateServiceLinkedRole"}


def classify_action(action: str) -> str:
    """Which resource class an action's ARN should come from.

    Returns "wildcard" for anything rc cannot pin to a concrete ARN. Only
    classes rc constructs from its OWN template output (project-named roles
    and instance profiles) or from the backend config (state object, lock
    table) are ever scoped.
    """
    if action in _UNSCOPED_IAM_ACTIONS:
        return "wildcard"
    if _INSTANCE_PROFILE_MARKER in action:
        return "iam_instance_profile"
    if action.startswith("iam:"):
        return "iam_role"
    if action in BACKEND_STATE_ACTIONS:
        # s3:ListBucket is needed on the state bucket AND (for the backup
        # bucket) elsewhere. Scoping it to state is the safe direction: a
        # denial here is a real finding, and the backup-bucket need simply
        # goes unchecked rather than being falsely reported.
        return "s3_state"
    if action in BACKEND_LOCK_ACTIONS:
        return "dynamodb_lock"
    return "wildcard"


@dataclass
class SimulationGroup:
    """One SimulatePrincipalPolicy call: actions + the ARNs to evaluate them on."""

    label: str
    actions: list[str]
    resource_arns: list[str] = field(default_factory=list)

    @property
    def scoped(self) -> bool:
        return bool(self.resource_arns)


def project_resource_arns(
    *,
    account_id: str,
    region: Optional[str],
    project: str,
    backend_cfg: dict,
) -> dict[str, list[str]]:
    """Concrete ARNs rc's own templates will create, per resource class.

    rc renders these names itself, so they are facts rather than guesses:
    iam.tf.j2 emits ``${var.project}-task`` / ``-task-exec`` and capacity.tf.j2
    emits ``${var.project}-ec2-instance`` for both the role and the instance
    profile. The state bucket/key and lock table come from the backend config.

    A class with no constructible ARN is simply absent, and its actions fall
    back to the wildcard group (reported with a caveat).
    """
    arns: dict[str, list[str]] = {}
    if account_id:
        arns["iam_role"] = [
            f"arn:aws:iam::{account_id}:role/{project}-task",
            f"arn:aws:iam::{account_id}:role/{project}-task-exec",
            f"arn:aws:iam::{account_id}:role/{project}-ec2-instance",
        ]
        arns["iam_instance_profile"] = [
            f"arn:aws:iam::{account_id}:instance-profile/{project}-ec2-instance"
        ]
    bucket = (backend_cfg or {}).get("bucket")
    key = (backend_cfg or {}).get("key")
    if bucket:
        # Both the bucket (ListBucket) and the object (Get/Put/DeleteObject);
        # terraform's backend needs each on its own ARN, and supplying both
        # lets one call cover the group.
        state = [f"arn:aws:s3:::{bucket}"]
        if key:
            state.append(f"arn:aws:s3:::{bucket}/{key}")
        arns["s3_state"] = state
    table = (backend_cfg or {}).get("dynamodb_table")
    if table and account_id and region:
        arns["dynamodb_lock"] = [
            f"arn:aws:dynamodb:{region}:{account_id}:table/{table}"
        ]
    return arns


def build_simulation_groups(
    actions: Iterable[str], resource_arns: dict[str, list[str]]
) -> list[SimulationGroup]:
    """Split actions into per-resource-class calls plus a wildcard remainder.

    Each group is one SimulatePrincipalPolicy invocation. Actions whose class
    has no constructible ARN join the wildcard group rather than being
    dropped -- an unchecked action is worse than one checked imprecisely, as
    long as the imprecision is reported.
    """
    by_class: dict[str, list[str]] = {}
    for action in sorted(set(actions)):
        klass = classify_action(action)
        if klass != "wildcard" and klass not in resource_arns:
            klass = "wildcard"
        by_class.setdefault(klass, []).append(action)

    groups: list[SimulationGroup] = []
    for klass in sorted(k for k in by_class if k != "wildcard"):
        groups.append(
            SimulationGroup(
                label=klass, actions=by_class[klass], resource_arns=resource_arns[klass]
            )
        )
    if by_class.get("wildcard"):
        groups.append(SimulationGroup(label="wildcard", actions=by_class["wildcard"]))
    return groups


def simulate_groups(
    iam_client: Any, principal_arn: str, groups: list[SimulationGroup]
) -> tuple[list[tuple[str, bool]], Optional[str]]:
    """Simulate each group against its own resources.

    Returns ``(denials, error)`` where each denial is
    ``(action, scoped)`` -- ``scoped`` False means it was evaluated against
    "*" and may therefore be a FALSE denial for a resource-scoped policy.
    ``error`` non-None means the simulation could not be performed at all.

    One call per group, because ResourceArns applies to every action in a
    call (verified live -- see the module comment above). Batched within a
    group only, where every action shares the same resources.
    """
    denials: list[tuple[str, bool]] = []
    batch_size = 100
    try:
        for group in groups:
            for start in range(0, len(group.actions), batch_size):
                batch = group.actions[start : start + batch_size]
                kwargs: dict[str, Any] = {
                    "PolicySourceArn": principal_arn,
                    "ActionNames": batch,
                }
                if group.resource_arns:
                    kwargs["ResourceArns"] = group.resource_arns
                response = iam_client.simulate_principal_policy(**kwargs)
                for result in response.get("EvaluationResults") or []:
                    if result.get("EvalDecision") != "allowed":
                        denials.append(
                            (result.get("EvalActionName", "?"), group.scoped)
                        )
    except Exception as exc:  # noqa: BLE001 — advisory check, never fatal
        return [], f"{type(exc).__name__}: {exc}"
    return sorted(set(denials)), None


# ---------------------------------------------------------------------------
# terraform binary / state-recorded version
# ---------------------------------------------------------------------------


def _parse_version(text: str) -> Optional[tuple[int, ...]]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def local_terraform_version(
    terraform_bin: Optional[str] = None,
) -> tuple[Optional[str], Optional[tuple[int, ...]]]:
    """Return ``(path, version_tuple)`` for the terraform on PATH."""
    path = terraform_bin or shutil.which("terraform")
    if not path:
        return None, None
    try:
        proc = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return path, None
    return path, _parse_version(proc.stdout)


def state_recorded_version(state_json: dict) -> Optional[tuple[int, ...]]:
    """The terraform_version stamped into a state file, if any.

    Terraform refuses to operate on a state written by a NEWER version:
    "state snapshot was created by Terraform vX, which is newer than current".
    So the pinned CI version has to be >= what wrote the state -- which is
    exactly how copying another repo's pinned 1.9.8 onto a state written by
    1.15.5 would have failed every deploy.
    """
    return _parse_version(str(state_json.get("terraform_version") or ""))


def format_version(version: Optional[tuple[int, ...]]) -> str:
    return ".".join(str(p) for p in version) if version else "unknown"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_terraform_binary(
    required_min: tuple[int, ...] = (1, 5),
    terraform_bin: Optional[str] = None,
) -> tuple[PreflightCheck, Optional[tuple[int, ...]]]:
    """Is there a terraform to run at all, and is it new enough?

    The very first failed production deploy in rc-g3jy was
    ``FileNotFoundError: 'terraform'`` — the runner had never needed one
    because the workflow had only ever run ``--no-state``.
    """
    path, version = local_terraform_version(terraform_bin)
    if not path:
        return (
            PreflightCheck(
                name="terraform binary",
                status=FAIL,
                detail="not found on PATH",
                remedy=(
                    "Install it (`rc doctor --fix`, or hashicorp/setup-terraform "
                    "in CI). A --no-state deploy never needed one; a stateful "
                    "deploy shells out to it for every step."
                ),
            ),
            None,
        )
    if version is None:
        return (
            PreflightCheck(
                name="terraform binary",
                status=WARN,
                detail=f"{path} did not report a parseable version",
                remedy="Check `terraform -version` runs cleanly.",
            ),
            None,
        )
    if version < required_min:
        return (
            PreflightCheck(
                name="terraform binary",
                status=FAIL,
                detail=f"{format_version(version)} at {path}, need >= "
                f"{format_version(required_min)}",
                remedy="Upgrade terraform.",
            ),
            version,
        )
    return (
        PreflightCheck(
            name="terraform binary",
            status=OK,
            detail=f"{format_version(version)} at {path}",
        ),
        version,
    )


def check_state_backend(
    session: Any, backend_cfg: dict, local_version: Optional[tuple[int, ...]]
) -> PreflightCheck:
    """Can this principal READ the remote state, and does its version fit?

    Both questions are answered by the same GetObject — the state document
    carries ``terraform_version``, so reading it proves access AND yields the
    version constraint in one call.

    A state object that does not exist yet is not a failure: that is a
    first-ever apply, and terraform will create it. The 403 case is the one
    that cost a production deploy, and it is reported as exactly that rather
    than as "not found".
    """
    btype = (backend_cfg or {}).get("type", "local")
    if btype != "s3":
        return PreflightCheck(
            name="state backend",
            status=SKIP,
            detail=f"backend type {btype!r} — only s3 is checked",
        )
    bucket = backend_cfg.get("bucket")
    key = backend_cfg.get("key")
    if not bucket or not key:
        return PreflightCheck(
            name="state backend",
            status=FAIL,
            detail="s3 backend is missing bucket and/or key",
            remedy="Set terraform.backend.bucket and terraform.backend.key.",
        )
    s3 = session.client("s3", region_name=backend_cfg.get("region"))
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — classified below
        code = _error_code(exc)
        if code in ("NoSuchKey", "404"):
            return PreflightCheck(
                name="state backend",
                status=OK,
                detail=f"s3://{bucket}/{key} does not exist yet "
                f"(first apply will create it)",
            )
        if code in ("AccessDenied", "403"):
            return PreflightCheck(
                name="state backend",
                status=FAIL,
                detail=f"s3://{bucket}/{key} — access denied reading state",
                remedy=(
                    "Grant the deploy principal s3:GetObject/s3:PutObject on "
                    f"arn:aws:s3:::{bucket}/{key} and s3:ListBucket on "
                    f"arn:aws:s3:::{bucket}. A --no-state deploy never touched "
                    "the state bucket, so this is routinely missing on the "
                    "first stateful deploy."
                ),
            )
        return PreflightCheck(
            name="state backend",
            status=FAIL,
            detail=f"s3://{bucket}/{key} — {type(exc).__name__}: {exc}",
        )

    try:
        state = json.loads(body)
    except ValueError:
        return PreflightCheck(
            name="state backend",
            status=WARN,
            detail=f"s3://{bucket}/{key} is readable but is not JSON",
        )
    recorded = state_recorded_version(state)
    if recorded is None:
        return PreflightCheck(
            name="state backend",
            status=OK,
            detail=f"s3://{bucket}/{key} readable (no terraform_version " f"recorded)",
        )
    if local_version is not None and local_version < recorded:
        return PreflightCheck(
            name="state backend",
            status=FAIL,
            detail=(
                f"state was written by terraform "
                f"{format_version(recorded)}, local terraform is "
                f"{format_version(local_version)}"
            ),
            remedy=(
                "terraform refuses to operate on state created by a newer "
                "version ('state snapshot was created by Terraform vX, which "
                f"is newer than current'). Pin >= {format_version(recorded)} "
                "— do not copy another repo's pinned version without "
                "checking it against THIS state."
            ),
        )
    return PreflightCheck(
        name="state backend",
        status=OK,
        detail=f"s3://{bucket}/{key} readable, written by terraform "
        f"{format_version(recorded)}",
    )


def check_state_lock(session: Any, backend_cfg: dict) -> PreflightCheck:
    """Can this principal ACQUIRE and RELEASE the state lock?

    Reading state is not enough — an apply that can read but not lock dies
    partway. Two probes:

    * GetItem on the real ``<bucket>/<key>`` LockID, read-only, to report a
      lock somebody else is holding. Never steals it: a held lock means
      another apply is in flight, and breaking it is strictly worse than
      saying so.
    * Put + Delete of a distinct ``-rc-preflight-probe`` item, which exercises
      exactly the write and delete permissions terraform's own locking needs
      on the same table, with no possibility of colliding with a real lock.
    """
    btype = (backend_cfg or {}).get("type", "local")
    if btype != "s3":
        return PreflightCheck(
            name="state lock",
            status=SKIP,
            detail=f"backend type {btype!r} — only s3 is checked",
        )
    table = backend_cfg.get("dynamodb_table")
    if not table:
        return PreflightCheck(
            name="state lock",
            status=SKIP,
            detail="no dynamodb_table configured (S3-native locking or "
            "unlocked backend)",
        )
    lock_id = f"{backend_cfg.get('bucket')}/{backend_cfg.get('key')}"
    ddb = session.client("dynamodb", region_name=backend_cfg.get("region"))

    try:
        held = ddb.get_item(TableName=table, Key={"LockID": {"S": lock_id}})
    except Exception as exc:  # noqa: BLE001
        return _lock_permission_failure(table, exc)
    if held.get("Item"):
        info = (held["Item"].get("Info") or {}).get("S", "")
        return PreflightCheck(
            name="state lock",
            status=WARN,
            detail=f"a lock is currently HELD on {lock_id}",
            remedy=(
                f"Another apply is in flight, or a previous one died holding "
                f"it. rc will not break it. Lock info: {info or '(none)'}"
            ),
        )

    probe_id = f"{lock_id}-rc-preflight-probe"
    try:
        ddb.put_item(
            TableName=table,
            Item={
                "LockID": {"S": probe_id},
                "Info": {"S": "remote-compose preflight write probe"},
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _lock_permission_failure(table, exc)
    try:
        ddb.delete_item(TableName=table, Key={"LockID": {"S": probe_id}})
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck(
            name="state lock",
            status=WARN,
            detail=f"{table}: can write but not delete ({_error_code(exc)})",
            remedy=(
                f"Grant dynamodb:DeleteItem on the lock table, and remove the "
                f"leftover probe item {probe_id!r}. Without DeleteItem, "
                "terraform acquires a lock it can never release."
            ),
        )
    return PreflightCheck(
        name="state lock",
        status=OK,
        detail=f"{table}: acquire + release verified, no lock held",
    )


def _lock_permission_failure(table: str, exc: Exception) -> PreflightCheck:
    return PreflightCheck(
        name="state lock",
        status=FAIL,
        detail=f"{table}: {_error_code(exc)} ({type(exc).__name__})",
        remedy=(
            "Grant dynamodb:GetItem, dynamodb:PutItem and dynamodb:DeleteItem "
            f"on the lock table {table}. terraform acquires the lock before "
            "it does anything else, so this fails every apply."
        ),
    )


def _account_from_arn(arn: str) -> str:
    """Account id out of any ARN, for building sibling resource ARNs."""
    parts = (arn or "").split(":")
    return parts[4] if len(parts) > 4 else ""


def _error_code(exc: Exception) -> str:
    """Best-effort AWS error code out of a botocore ClientError."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        return str(error.get("Code") or "") or str(exc)
    return str(exc)


def check_iam_actions(
    session: Any,
    caller_arn: str,
    actions: list[str],
    region: Optional[str] = None,
    *,
    principal_arn: Optional[str] = None,
    is_deploy_principal: bool = False,
    resource_arns: Optional[dict[str, list[str]]] = None,
) -> tuple[PreflightCheck, list[str]]:
    """Simulate every action terraform will call, and report ALL that fail.

    ``principal_arn`` overrides the caller as the simulated principal
    (rc-zu1x): the identity that matters is the CI role the deploy will
    actually run as, which is precisely the identity a laptop run is NOT.
    ``is_deploy_principal`` records whether that is the configured deploy
    role, so the report can say plainly which principal it checked.

    Advisory by construction. ``iam:SimulatePrincipalPolicy`` is itself a
    permission, and it does not fully evaluate SCPs or permission boundaries
    — a clean result is evidence, not proof, and an unavailable simulation is
    reported as "could not check" rather than as a pass.
    """
    target = principal_arn or caller_arn
    principal = canonical_principal_arn(target) or (
        target if target.startswith("arn:aws:iam:") else None
    )
    if principal is None:
        return (
            PreflightCheck(
                name="deploy principal IAM",
                status=SKIP,
                detail=f"cannot derive an IAM entity ARN from {target!r}",
                remedy="Only role and user principals can be simulated.",
            ),
            [],
        )

    provenance = (
        f"checked {principal} (the configured deploy principal)"
        if is_deploy_principal
        else f"checked {principal} — NOT a configured deploy role"
    )
    iam = session.client("iam", region_name=region)
    groups = build_simulation_groups(actions, resource_arns or {})
    denials, error = simulate_groups(iam, principal, groups)
    if error:
        return (
            PreflightCheck(
                name="deploy principal IAM",
                status=WARN,
                detail=f"could not simulate ({error}) — {provenance}",
                remedy=(
                    "Grant iam:SimulatePrincipalPolicy to check permissions "
                    "up front. Without it rc cannot tell you what is missing "
                    "before the apply — it is not evidence that anything is "
                    "wrong, only that rc could not look."
                ),
            ),
            [],
        )

    scoped_count = sum(1 for g in groups if g.scoped)
    if not denials:
        detail = f"{len(actions)} action(s) allowed — {provenance}"
        remedy = ""
        if not is_deploy_principal:
            remedy = (
                "This says nothing about whether CI can deploy. Point rc at "
                "the principal that will really run it: set "
                "provider_config.ecs.deploy_role_arn in rc.yml, or pass "
                "`rc preflight --principal <arn>`."
            )
        return (
            PreflightCheck(
                name="deploy principal IAM",
                status=OK if is_deploy_principal else WARN,
                detail=detail,
                remedy=remedy,
            ),
            [],
        )

    denied = [a for a, _ in denials]
    grouped = group_by_service(denied)
    summary = ", ".join(
        f"{service} ({len(items)})" for service, items in sorted(grouped.items())
    )
    lines = [f"{provenance}."]
    if scoped_count:
        lines.append(
            f"{scoped_count} action group(s) were evaluated against the "
            f'concrete ARNs rc will create; the rest against "*".'
        )
    for service, items in sorted(grouped.items()):
        lines.append(f"{service}: {', '.join(items)}")
    unscoped_denials = sorted({a for a, scoped in denials if not scoped})
    if unscoped_denials:
        lines.append(
            'NOTE — the following were checked against "*" because rc '
            "could not construct a concrete ARN for them, so a "
            "resource-scoped policy that genuinely permits them will still "
            "show here. Verify with `aws iam simulate-principal-policy "
            "--resource-arns <the real arn>` before widening any statement: "
            + ", ".join(unscoped_denials[:12])
            + (
                f", +{len(unscoped_denials) - 12} more"
                if len(unscoped_denials) > 12
                else ""
            )
        )
    return (
        PreflightCheck(
            name="deploy principal IAM",
            status=FAIL,
            detail=f"{len(denied)} action(s) denied — {summary}",
            remedy="\n".join(lines),
        ),
        denied,
    )


def run_preflight(
    tf_dir: Path,
    backend_cfg: dict,
    session: Any,
    region: Optional[str] = None,
    required_terraform: tuple[int, ...] = (1, 5),
    *,
    project: str = "",
    deploy_principal_arn: Optional[str] = None,
) -> PreflightReport:
    """Run every check and return the COMPLETE set of findings.

    Deliberately runs all of them regardless of earlier failures. Stopping at
    the first problem is what produced rc-g3jy's three serial failed deploys;
    the whole value here is one round of fixes instead of one per deploy.
    """
    report = PreflightReport()

    tf_check, local_version = check_terraform_binary(required_terraform)
    report.add(tf_check)

    caller_arn = ""
    account_id = ""
    try:
        identity = session.client("sts", region_name=region).get_caller_identity()
        caller_arn = str(identity.get("Arn") or "")
        account_id = str(identity.get("Account") or "")
        detail = caller_arn or "(no arn)"
        if deploy_principal_arn:
            detail += f"; simulating against {deploy_principal_arn}"
        report.add(PreflightCheck(name="aws identity", status=OK, detail=detail))
    except Exception as exc:  # noqa: BLE001
        report.add(
            PreflightCheck(
                name="aws identity",
                status=FAIL,
                detail=f"sts:GetCallerIdentity failed ({type(exc).__name__}: {exc})",
                remedy=(
                    "No usable AWS credentials. Nothing else in this report "
                    "can be trusted until this passes."
                ),
            )
        )

    report.add(check_state_backend(session, backend_cfg, local_version))
    report.add(check_state_lock(session, backend_cfg))

    resources, data_sources = scan_terraform_dir(tf_dir)
    actions, unmodeled = derive_required_actions(resources, data_sources, backend_cfg)
    report.unmodeled_resource_types = unmodeled
    if unmodeled:
        report.add(
            PreflightCheck(
                name="action coverage",
                status=WARN,
                detail=f"{len(unmodeled)} emitted resource type(s) have no IAM "
                f"action mapping and were NOT checked",
                remedy=", ".join(unmodeled),
            )
        )
    if caller_arn or deploy_principal_arn:
        iam_check, denied = check_iam_actions(
            session,
            caller_arn,
            actions,
            region,
            principal_arn=deploy_principal_arn,
            is_deploy_principal=bool(deploy_principal_arn),
            resource_arns=project_resource_arns(
                account_id=account_id
                or _account_from_arn(deploy_principal_arn or caller_arn),
                region=region,
                project=project,
                backend_cfg=backend_cfg,
            ),
        )
        report.add(iam_check)
        report.missing_actions = denied
        report.checked_principal = (
            canonical_principal_arn(deploy_principal_arn or caller_arn) or ""
        )
        report.checked_deploy_principal = bool(deploy_principal_arn)
    else:
        report.add(
            PreflightCheck(
                name="deploy principal IAM",
                status=SKIP,
                detail="skipped — no caller identity",
            )
        )
    return report
