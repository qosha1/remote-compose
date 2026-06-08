"""Build the terraform import set for `rc adopt`.

Given a v2 rc.yml and the *already-emitted* terraform module, produce the
list of ``(terraform_address, aws_resource_id)`` pairs needed to bring a
live ECS stack under terraform management.

Design (rc-6o3):

  * Address enumeration is driven by parsing the emitted ``*.tf`` files in
    the working dir — NOT by re-deriving the provider's template
    conditionals (owns_image_repo / has_domain / has_service_discovery /
    tls_mode / …). Whatever the provider chose to emit is exactly the set
    terraform will manage, so reading it back is the robust source of
    truth and keeps this module decoupled from template logic.

  * Per-resource-type resolvers turn each address into the import id the
    AWS terraform provider expects. Two flavours:
      - deterministic: constructed from rc.yml-derived names (project,
        cluster, service names) — no AWS call (e.g. aws_ecs_cluster.main
        → cluster name, aws_ecs_service.django → "<cluster>/django").
      - discovered: AWS-generated opaque ids (ARNs, sg-/srv-/ns- ids,
        task-def revisions, route53 zone ids) looked up via boto3.

  * A resolver returns ``None`` when the live resource doesn't exist
    (e.g. an ECR repo terraform would create fresh). Those addresses are
    reported as ``skipped`` so the caller can show "will be created on
    first apply" rather than failing the run.

This module performs NO terraform calls and never mutates AWS — it only
reads. The caller (state_backend.adopt) feeds the result into
``terraform import``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# Matches `resource "aws_lb" "main" {` → ("aws_lb", "main").
_RESOURCE_RE = re.compile(
    r'^\s*resource\s+"([a-z0-9_]+)"\s+"([a-zA-Z0-9_]+)"\s*\{',
    re.MULTILINE,
)

# AWS managed policy attached to the task-execution role by iam.tf.j2.
_TASK_EXEC_MANAGED_POLICY = (
    "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
)


@dataclass
class ImportPlan:
    """Result of building the import set.

    imports: (terraform_address, aws_resource_id) pairs ready for
        ``terraform import``, in a dependency-friendly order (parents
        before children — cluster before services, lb before listeners).
    skipped: (terraform_address, reason) for addresses whose live
        resource was not found (terraform will create them on apply) or
        which have no resolver.
    """

    imports: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


# Order resources so parents import before children. terraform import
# itself doesn't require this, but it keeps partial-failure state coherent
# and the progress output readable.
_TYPE_ORDER = {
    "aws_cloudwatch_log_group": 0,
    "aws_iam_role": 0,
    "aws_iam_role_policy": 1,
    "aws_iam_role_policy_attachment": 1,
    "aws_ecr_repository": 0,
    "aws_ecr_lifecycle_policy": 1,
    "aws_security_group": 0,
    "aws_ecs_cluster": 0,
    "aws_ecs_cluster_capacity_providers": 1,
    "aws_service_discovery_private_dns_namespace": 0,
    "aws_service_discovery_service": 1,
    "aws_lb": 0,
    "aws_lb_target_group": 1,
    "aws_lb_listener": 1,
    "aws_lb_listener_rule": 2,
    "aws_ecs_task_definition": 2,
    "aws_ecs_service": 3,
    "aws_route53_record": 2,
}


def parse_emitted_addresses(working_dir: Path) -> list[tuple[str, str]]:
    """Return ``(resource_type, local_name)`` for every resource block in
    the emitted ``*.tf`` files under ``working_dir``.

    Skips ``data`` blocks (only ``resource`` blocks are importable) and the
    hidden ``.terraform`` plugin cache.
    """
    found: list[tuple[str, str]] = []
    for tf in sorted(working_dir.glob("*.tf")):
        text = tf.read_text()
        for m in _RESOURCE_RE.finditer(text):
            found.append((m.group(1), m.group(2)))
    return found


def build_import_plan(
    rc_yml_path: Path,
    working_dir: Path,
    *,
    session: Optional[Any] = None,
) -> ImportPlan:
    """Build the import set for the stack described by ``rc_yml_path``.

    ``working_dir`` must already contain the emitted terraform module
    (the provider's ``emit_terraform`` output) so addresses can be read
    back. ``session`` is an optional boto3 Session for the AWS lookups;
    when None one is constructed from the rc.yml's aws_profile + region.
    """
    from remote_compose.cli_v2 import load_rc_yml

    _version, _raw, v2 = load_rc_yml(rc_yml_path)
    if v2 is None:
        return ImportPlan()

    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    resolver = _Resolver(
        project=v2.project,
        cluster=ecs_cfg.get("cluster") or f"{v2.project}-cluster",
        region=ecs_cfg.get("region"),
        zone=ecs_cfg.get("route53_zone") or v2.domain,
        services=v2.services or {},
        session=session,
        aws_profile=ecs_cfg.get("aws_profile"),
    )

    addresses = parse_emitted_addresses(working_dir)
    # Stable, parent-before-child ordering.
    addresses.sort(key=lambda ta: (_TYPE_ORDER.get(ta[0], 9), ta[1]))

    plan = ImportPlan()
    for rtype, local in addresses:
        address = f"{rtype}.{local}"
        handler = resolver.dispatch.get(rtype)
        if handler is None:
            plan.skipped.append((address, f"no resolver for {rtype}"))
            continue
        try:
            rid = handler(local)
        except _NotLive as exc:
            plan.skipped.append((address, str(exc)))
            continue
        if rid is None:
            plan.skipped.append((address, "live resource not found"))
            continue
        plan.imports.append((address, rid))
    return plan


class _NotLive(Exception):
    """Raised by a resolver when the resource is provably not live."""


class _Resolver:
    """Per-resource-type id resolvers, bound to one stack's config.

    boto3 clients are created lazily + cached so a stack with no ALB never
    constructs an elbv2 client, and tests can inject a fake session.
    """

    def __init__(
        self,
        *,
        project: str,
        cluster: str,
        region: Optional[str],
        zone: Optional[str],
        services: dict,
        session: Optional[Any],
        aws_profile: Optional[str],
    ) -> None:
        self.project = project
        self.cluster = cluster
        self.region = region
        self.zone = zone
        self.services = services
        self._session = session
        self._aws_profile = aws_profile
        self._clients: dict[str, Any] = {}
        # tf_name (hyphens → underscores) → service name as declared.
        self._tf_to_name = {name.replace("-", "_"): name for name in services}
        self.dispatch: dict[str, Callable[[str], Optional[str]]] = {
            "aws_ecs_cluster": self._ecs_cluster,
            "aws_ecs_cluster_capacity_providers": self._ecs_cluster,
            "aws_cloudwatch_log_group": self._log_group,
            "aws_ecr_repository": self._ecr_repository,
            "aws_ecr_lifecycle_policy": self._ecr_lifecycle_policy,
            "aws_iam_role": self._iam_role,
            "aws_iam_role_policy": self._iam_role_policy,
            "aws_iam_role_policy_attachment": self._iam_role_policy_attachment,
            "aws_security_group": self._security_group,
            "aws_ecs_service": self._ecs_service,
            "aws_ecs_task_definition": self._ecs_task_definition,
            "aws_lb": self._lb,
            "aws_lb_listener": self._lb_listener,
            "aws_lb_listener_rule": self._lb_listener_rule,
            "aws_lb_target_group": self._lb_target_group,
            "aws_service_discovery_private_dns_namespace": self._sd_namespace,
            "aws_service_discovery_service": self._sd_service,
            "aws_route53_record": self._route53_record,
        }

    # -- boto3 plumbing -------------------------------------------------

    def _client(self, name: str) -> Any:
        if name not in self._clients:
            session = self._session
            if session is None:
                import boto3

                session = boto3.Session(
                    profile_name=self._aws_profile,
                    region_name=self.region,
                )
                self._session = session
            self._clients[name] = session.client(name, region_name=self.region)
        return self._clients[name]

    def _svc_name(self, tf_name: str) -> Optional[str]:
        return self._tf_to_name.get(tf_name)

    # -- deterministic resolvers (no AWS call) --------------------------

    def _ecs_cluster(self, _local: str) -> str:
        # aws_ecs_cluster.main and aws_ecs_cluster_capacity_providers.main
        # both import by cluster name.
        return self.cluster

    def _log_group(self, local: str) -> Optional[str]:
        if local == "tasks":
            return f"/ecs/{self.project}"
        if local == "container_insights":
            return f"/aws/ecs/containerinsights/{self.cluster}/performance"
        return None

    def _iam_role(self, local: str) -> Optional[str]:
        # iam.tf.j2: task_execution → "<project>-task-exec",
        #            task           → "<project>-task".
        return {
            "task_execution": f"{self.project}-task-exec",
            "task": f"{self.project}-task",
        }.get(local)

    def _iam_role_policy(self, local: str) -> Optional[str]:
        # Import id is "<role_name>:<policy_name>".
        mapping = {
            "task_execute_command": (
                f"{self.project}-task",
                f"{self.project}-task-exec-cmd",
            ),
            "task_execution_secrets": (
                f"{self.project}-task-exec",
                f"{self.project}-task-exec-secrets",
            ),
        }
        pair = mapping.get(local)
        if pair is None:
            return None
        return f"{pair[0]}:{pair[1]}"

    def _iam_role_policy_attachment(self, local: str) -> Optional[str]:
        if local == "task_execution":
            return f"{self.project}-task-exec/{_TASK_EXEC_MANAGED_POLICY}"
        return None

    def _ecs_service(self, local: str) -> Optional[str]:
        name = self._svc_name(local)
        if name is None:
            return None
        return f"{self.cluster}/{name}"

    # -- discovered resolvers (boto3) -----------------------------------

    def _ecr_repository(self, local: str) -> Optional[str]:
        if local == "buildcache":
            repo = f"{self.project}/buildcache"
        else:
            name = self._svc_name(local)
            if name is None:
                return None
            repo = f"{self.project}/{name}"
        # Import id is the repo name, but only if it exists live; a fresh
        # repo terraform will create is skipped.
        client = self._client("ecr")
        try:
            client.describe_repositories(repositoryNames=[repo])
        except Exception as exc:  # noqa: BLE001
            if _err_code(exc) == "RepositoryNotFoundException":
                raise _NotLive(f"ECR repo {repo} not live") from exc
            raise
        return repo

    def _ecr_lifecycle_policy(self, local: str) -> Optional[str]:
        if local != "buildcache":
            return None
        repo = f"{self.project}/buildcache"
        client = self._client("ecr")
        try:
            client.get_lifecycle_policy(repositoryName=repo)
        except Exception as exc:  # noqa: BLE001
            code = _err_code(exc)
            if code in (
                "LifecyclePolicyNotFoundException",
                "RepositoryNotFoundException",
            ):
                raise _NotLive(f"ECR lifecycle policy for {repo} not live") from exc
            raise
        return repo

    def _security_group(self, local: str) -> Optional[str]:
        # security_groups.tf.j2: "<project>-alb" / "<project>-tasks".
        name = {
            "alb": f"{self.project}-alb",
            "tasks": f"{self.project}-tasks",
        }.get(local)
        if name is None:
            return None
        client = self._client("ec2")
        resp = client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )
        groups = resp.get("SecurityGroups") or []
        if not groups:
            raise _NotLive(f"security group {name} not live")
        return groups[0]["GroupId"]

    def _ecs_task_definition(self, local: str) -> Optional[str]:
        name = self._svc_name(local)
        if name is None:
            return None
        family = f"{self.project}-{name}"
        client = self._client("ecs")
        try:
            resp = client.describe_task_definition(taskDefinition=family)
        except Exception as exc:  # noqa: BLE001
            if _err_code(exc) in ("ClientException", "ClientError"):
                raise _NotLive(f"task def {family} not live") from exc
            raise
        td = resp.get("taskDefinition") or {}
        rev = td.get("revision")
        if rev is None:
            raise _NotLive(f"task def {family} has no active revision")
        return f"{family}:{rev}"

    def _lb(self, _local: str) -> Optional[str]:
        return self._alb_arn()

    def _alb_arn(self) -> Optional[str]:
        if "_alb_arn" not in self._clients:
            client = self._client("elbv2")
            name = f"{self.project}-alb"
            try:
                resp = client.describe_load_balancers(Names=[name])
            except Exception as exc:  # noqa: BLE001
                if _err_code(exc) == "LoadBalancerNotFound":
                    self._clients["_alb_arn"] = None
                    raise _NotLive(f"ALB {name} not live") from exc
                raise
            lbs = resp.get("LoadBalancers") or []
            self._clients["_alb_arn"] = lbs[0]["LoadBalancerArn"] if lbs else None
        arn = self._clients["_alb_arn"]
        if arn is None:
            raise _NotLive("ALB not live")
        return arn

    def _listeners(self) -> list[dict]:
        if "_listeners" not in self._clients:
            client = self._client("elbv2")
            resp = client.describe_listeners(LoadBalancerArn=self._alb_arn())
            self._clients["_listeners"] = resp.get("Listeners") or []
        return self._clients["_listeners"]

    def _lb_listener(self, local: str) -> Optional[str]:
        want_port = {"http": 80, "https": 443}.get(local)
        if want_port is None:
            return None
        for lst in self._listeners():
            if lst.get("Port") == want_port:
                return lst["ListenerArn"]
        raise _NotLive(f"listener :{want_port} not live")

    def _https_listener_arn(self) -> Optional[str]:
        for lst in self._listeners():
            if lst.get("Port") == 443:
                return lst["ListenerArn"]
        return None

    def _rules(self) -> list[dict]:
        if "_rules" not in self._clients:
            arn = self._https_listener_arn()
            if arn is None:
                self._clients["_rules"] = []
            else:
                client = self._client("elbv2")
                resp = client.describe_rules(ListenerArn=arn)
                self._clients["_rules"] = resp.get("Rules") or []
        return self._clients["_rules"]

    def _rule_for_domain(self, domain: str) -> Optional[dict]:
        for rule in self._rules():
            for cond in rule.get("Conditions") or []:
                vals = (
                    (cond.get("HostHeaderConfig") or {}).get("Values")
                    or cond.get("Values")
                    or []
                )
                if domain in vals:
                    return rule
        return None

    def _lb_listener_rule(self, local: str) -> Optional[str]:
        name = self._svc_name(local)
        if name is None:
            return None
        svc = self.services.get(name)
        domain = getattr(svc, "domain", None) if svc else None
        if not domain:
            raise _NotLive(f"service {name} has no domain")
        rule = self._rule_for_domain(domain)
        if rule is None:
            raise _NotLive(f"listener rule for {domain} not live")
        return rule["RuleArn"]

    def _lb_target_group(self, local: str) -> Optional[str]:
        # Target groups use name_prefix (auto-suffixed), so resolve via the
        # listener rule whose host_header matches the service's domain →
        # its forward action target group. Robust against the random
        # name suffix.
        name = self._svc_name(local)
        if name is None:
            return None
        svc = self.services.get(name)
        domain = getattr(svc, "domain", None) if svc else None
        if not domain:
            raise _NotLive(f"service {name} has no domain")
        rule = self._rule_for_domain(domain)
        if rule is None:
            raise _NotLive(f"target group for {domain} not live")
        for action in rule.get("Actions") or []:
            tg = action.get("TargetGroupArn")
            if tg:
                return tg
        raise _NotLive(f"target group arn for {domain} not found")

    def _sd_namespace(self, _local: str) -> Optional[str]:
        return self._namespace_id()

    def _namespace_id(self) -> Optional[str]:
        if "_ns_id" not in self._clients:
            client = self._client("servicediscovery")
            want = f"{self.project}.local"
            ns_id = None
            paginator = client.get_paginator("list_namespaces")
            for page in paginator.paginate():
                for ns in page.get("Namespaces") or []:
                    if ns.get("Name") == want:
                        ns_id = ns.get("Id")
                        break
                if ns_id:
                    break
            self._clients["_ns_id"] = ns_id
        ns_id = self._clients["_ns_id"]
        if ns_id is None:
            raise _NotLive(f"service discovery namespace {self.project}.local not live")
        return ns_id

    def _sd_service(self, local: str) -> Optional[str]:
        name = self._svc_name(local)
        if name is None:
            return None
        ns_id = self._namespace_id()
        client = self._client("servicediscovery")
        paginator = client.get_paginator("list_services")
        for page in paginator.paginate(
            Filters=[
                {
                    "Name": "NAMESPACE_ID",
                    "Values": [ns_id],
                    "Condition": "EQ",
                }
            ]
        ):
            for sd in page.get("Services") or []:
                if sd.get("Name") == name:
                    return sd.get("Id")
        raise _NotLive(f"service discovery service {name} not live")

    def _zone_id(self) -> Optional[str]:
        if "_zone_id" not in self._clients:
            if not self.zone:
                self._clients["_zone_id"] = None
            else:
                client = self._client("route53")
                want = self.zone.rstrip(".") + "."
                zid = None
                paginator = client.get_paginator("list_hosted_zones")
                for page in paginator.paginate():
                    for z in page.get("HostedZones") or []:
                        if z.get("Name") == want and not (z.get("Config") or {}).get(
                            "PrivateZone"
                        ):
                            zid = z["Id"].split("/")[-1]
                            break
                    if zid:
                        break
                self._clients["_zone_id"] = zid
        zid = self._clients["_zone_id"]
        if zid is None:
            raise _NotLive(f"route53 zone {self.zone} not live")
        return zid

    def _ordered_domains(self) -> list[str]:
        # domain.tf.j2 emits app_<loop.index> over all_domains. Mirror the
        # provider's ordering: each public service's domain, in service
        # declaration order, deduped. (Matches ECSProvider's all_domains
        # construction; pinned by unit test.)
        domains: list[str] = []
        for name, svc in self.services.items():
            d = getattr(svc, "domain", None)
            if d and d not in domains:
                domains.append(d)
        return domains

    def _route53_record(self, local: str) -> Optional[str]:
        m = re.fullmatch(r"app_(\d+)", local)
        if not m:
            return None
        idx = int(m.group(1)) - 1  # app_1 → index 0
        domains = self._ordered_domains()
        if idx < 0 or idx >= len(domains):
            raise _NotLive(f"no domain for {local}")
        zone_id = self._zone_id()
        # aws_route53_record import id: "ZONEID_NAME_TYPE".
        return f"{zone_id}_{domains[idx]}_A"


def _err_code(exc: BaseException) -> Optional[str]:
    """Extract a boto3 ClientError code, tolerant of mock-shaped errors."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None
