"""rc audit — sweep an AWS account for resources matching a project name.

Reverse of `rc destroy`. Finds anything terraform might have left
behind — orphan log groups, target groups stuck waiting for an old
listener, dangling S3 buckets — so users can verify cleanup or hunt
down account-hygiene issues before going public.

Pure-function `audit_project(session, project, region)` so tests can
inject a fake boto3 session. The CLI (rc audit, in cli.py) wraps this
with rc.yml resolution + an optional --delete path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditFinding:
    """One leftover AWS resource matched against a project name."""

    resource_type: str  # 'ecs_cluster' / 'vpc' / 'log_group' / ...
    identifier: str  # name or id (for human readability)
    arn: str | None = None  # full ARN when available (for --delete)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    project: str
    region: str
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0

    def by_type(self) -> dict[str, list[AuditFinding]]:
        out: dict[str, list[AuditFinding]] = {}
        for f in self.findings:
            out.setdefault(f.resource_type, []).append(f)
        return out

    def render(self) -> str:
        lines = [f"rc audit — {self.project} in {self.region}"]
        if self.is_clean:
            lines.append("  ✓ clean — no resources match this project")
            return "\n".join(lines)
        groups = self.by_type()
        lines.append(
            f"  {len(self.findings)} leftover resource(s) across "
            f"{len(groups)} class(es):"
        )
        for kind in sorted(groups):
            items = groups[kind]
            lines.append(f"    {kind} ({len(items)}):")
            for f in items:
                lines.append(f"      - {f.identifier}")
        return "\n".join(lines)


def audit_project(session, project: str, region: str) -> AuditReport:
    """Inspect every AWS resource class terraform owns + flag anything
    that names or tags itself with `project`.

    `session` must be a boto3.Session-shaped object (real or mocked).
    """
    findings: list[AuditFinding] = []
    findings.extend(_audit_ecs_clusters(session, project))
    findings.extend(_audit_vpcs(session, project))
    findings.extend(_audit_albs(session, project))
    findings.extend(_audit_target_groups(session, project))
    findings.extend(_audit_efs(session, project))
    findings.extend(_audit_ecr(session, project))
    findings.extend(_audit_service_discovery(session, project))
    findings.extend(_audit_log_groups(session, project))
    findings.extend(_audit_iam_roles(session, project))
    findings.extend(_audit_secrets(session, project))
    findings.extend(_audit_security_groups(session, project))
    findings.extend(_audit_s3_buckets(session, project))
    return AuditReport(project=project, region=region, findings=findings)


# ---------------------------------------------------------------------
# Per-class auditors. Each returns [AuditFinding].
# Wrap every API call in try/except so a single missing-permission
# doesn't blow up the whole sweep.
# ---------------------------------------------------------------------


def _safe(fn, default):
    """Call fn() and return its result if it matches `default`'s type;
    otherwise return `default`. Catches exceptions (e.g. AccessDenied)
    AND short-circuits when the response isn't iterable as expected
    (Mock instances returning Mock instead of dicts/lists during tests)."""
    try:
        result = fn()
    except Exception:
        return default
    if not isinstance(result, type(default)):
        return default
    return result


def _has_project_tag(tags: list[dict[str, Any]] | None, project: str) -> bool:
    for t in tags or []:
        if t.get("Key") == "Project" and t.get("Value") == project:
            return True
    return False


def _audit_ecs_clusters(session, project: str) -> list[AuditFinding]:
    ecs = session.client("ecs")
    arns = _safe(lambda: ecs.list_clusters().get("clusterArns") or [], [])
    out: list[AuditFinding] = []
    for arn in arns:
        # cluster ARN format: arn:aws:ecs:<region>:<acct>:cluster/<name>
        name = arn.split("/", 1)[-1]
        if project in name:
            out.append(AuditFinding("ecs_cluster", name, arn=arn))
    return out


def _audit_vpcs(session, project: str) -> list[AuditFinding]:
    ec2 = session.client("ec2")
    vpcs = _safe(lambda: ec2.describe_vpcs().get("Vpcs") or [], [])
    out: list[AuditFinding] = []
    for v in vpcs:
        if _has_project_tag(v.get("Tags"), project):
            out.append(
                AuditFinding(
                    "vpc",
                    v.get("VpcId", ""),
                    arn=None,
                    extra={"cidr": v.get("CidrBlock")},
                )
            )
    return out


def _audit_albs(session, project: str) -> list[AuditFinding]:
    elb = session.client("elbv2")
    lbs = _safe(lambda: elb.describe_load_balancers().get("LoadBalancers") or [], [])
    out: list[AuditFinding] = []
    for lb in lbs:
        name = lb.get("LoadBalancerName", "")
        if project in name:
            out.append(
                AuditFinding(
                    "alb",
                    name,
                    arn=lb.get("LoadBalancerArn"),
                )
            )
    return out


def _audit_target_groups(session, project: str) -> list[AuditFinding]:
    elb = session.client("elbv2")
    tgs = _safe(lambda: elb.describe_target_groups().get("TargetGroups") or [], [])
    out: list[AuditFinding] = []
    for tg in tgs:
        name = tg.get("TargetGroupName", "")
        if project in name:
            out.append(
                AuditFinding(
                    "target_group",
                    name,
                    arn=tg.get("TargetGroupArn"),
                )
            )
    return out


def _audit_efs(session, project: str) -> list[AuditFinding]:
    efs = session.client("efs")
    fss = _safe(lambda: efs.describe_file_systems().get("FileSystems") or [], [])
    out: list[AuditFinding] = []
    for f in fss:
        # describe_file_systems doesn't return tags inline; need a
        # separate call per fs. For the audit, accept name-token match.
        name = f.get("Name") or ""
        if project in name:
            out.append(
                AuditFinding(
                    "efs",
                    f.get("FileSystemId", ""),
                    arn=f.get("FileSystemArn"),
                )
            )
    return out


def _audit_ecr(session, project: str) -> list[AuditFinding]:
    ecr = session.client("ecr")
    repos = _safe(lambda: ecr.describe_repositories().get("repositories") or [], [])
    out: list[AuditFinding] = []
    prefix = f"{project}/"
    for r in repos:
        name = r.get("repositoryName", "")
        if name == project or name.startswith(prefix):
            out.append(
                AuditFinding(
                    "ecr_repository",
                    name,
                    arn=r.get("repositoryArn"),
                )
            )
    return out


def _audit_service_discovery(session, project: str) -> list[AuditFinding]:
    sd = session.client("servicediscovery")
    nss = _safe(lambda: sd.list_namespaces().get("Namespaces") or [], [])
    out: list[AuditFinding] = []
    for ns in nss:
        name = ns.get("Name", "")
        if project in name:
            out.append(
                AuditFinding(
                    "service_discovery_namespace",
                    name,
                    arn=ns.get("Arn"),
                )
            )
    return out


def _audit_log_groups(session, project: str) -> list[AuditFinding]:
    logs = session.client("logs")
    groups = _safe(lambda: logs.describe_log_groups().get("logGroups") or [], [])
    out: list[AuditFinding] = []
    for g in groups:
        name = g.get("logGroupName", "")
        if project in name:
            out.append(
                AuditFinding(
                    "log_group",
                    name,
                    arn=g.get("arn"),
                )
            )
    return out


def _audit_iam_roles(session, project: str) -> list[AuditFinding]:
    iam = session.client("iam")
    roles = _safe(lambda: iam.list_roles().get("Roles") or [], [])
    out: list[AuditFinding] = []
    prefix = f"{project}-"
    for r in roles:
        name = r.get("RoleName", "")
        if name == project or name.startswith(prefix):
            out.append(
                AuditFinding(
                    "iam_role",
                    name,
                    arn=r.get("Arn"),
                )
            )
    return out


def _audit_secrets(session, project: str) -> list[AuditFinding]:
    sm = session.client("secretsmanager")
    secs = _safe(lambda: sm.list_secrets().get("SecretList") or [], [])
    out: list[AuditFinding] = []
    prefix = f"{project}/"
    for s in secs:
        name = s.get("Name", "")
        if name == project or name.startswith(prefix):
            out.append(
                AuditFinding(
                    "secret",
                    name,
                    arn=s.get("ARN"),
                )
            )
    return out


def _audit_security_groups(session, project: str) -> list[AuditFinding]:
    ec2 = session.client("ec2")
    sgs = _safe(lambda: ec2.describe_security_groups().get("SecurityGroups") or [], [])
    out: list[AuditFinding] = []
    for sg in sgs:
        if _has_project_tag(sg.get("Tags"), project):
            out.append(
                AuditFinding(
                    "security_group",
                    sg.get("GroupId", ""),
                    arn=None,
                    extra={"name": sg.get("GroupName")},
                )
            )
    return out


def _audit_s3_buckets(session, project: str) -> list[AuditFinding]:
    s3 = session.client("s3")
    buckets = _safe(lambda: s3.list_buckets().get("Buckets") or [], [])
    out: list[AuditFinding] = []
    for b in buckets:
        name = b.get("Name", "")
        if project in name:
            out.append(AuditFinding("s3_bucket", name, arn=None))
    return out
