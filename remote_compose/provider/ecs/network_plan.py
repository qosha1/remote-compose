"""Turn a validated ``network:`` / ``repositories:`` block into terraform view models.

Kept out of ``provider.py`` (already ~3.5k lines) and free of any AWS SDK or
terraform invocation so the whole layer is unit-testable on plain dicts: feed
it a :class:`NetworkV2`, assert on the emitted resource names, CIDR
expressions, and HCL references.

Two design commitments worth stating up front, because they are what make the
declared network *safe* rather than merely configurable:

**Modern per-rule resources.** Declared groups emit
``aws_vpc_security_group_{ingress,egress}_rule`` — one terraform resource per
rule — instead of inline ``ingress`` / ``egress`` blocks. The AWS provider
strips the allow-all egress rule AWS attaches to every new security group, so
an ``aws_security_group`` with no rules genuinely reaches nothing. Inline
blocks cannot be mixed with rule resources, which is why declared groups never
use them, and why the built-in ``alb`` / ``tasks`` groups (which do) are left
completely untouched.

**Both halves of a two-sided rule.** Declaring ``to: endpoint:ecr`` on a group
grants that group egress to the endpoint *and* grants the endpoint's own
security group the matching ingress. Writing one half and silently getting no
connectivity is the classic VPC-endpoint failure; here it is not expressible.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ...config._network_types import (
    DECLARED_SUBNET_CIDR_BASE,
    NetworkRuleV2,
    NetworkV2,
    RepositoryV2,
    ResourceRef,
)
from ..base import ProviderConfigError

# Interface endpoints terminate TLS on 443 and nothing else. A rule aimed at
# one with no explicit ports means 443/tcp on both sides — the only thing it
# could usefully mean.
ENDPOINT_DEFAULT_PORT = 443

# cidrsubnet(var.vpc_cidr, 8, n) — /16 VPC carved into /24s, matching the
# built-in public (n=0,1) and private (n=10,11) subnets in network.tf.j2.
_CIDR_NEWBITS = 8


def tf_ident(name: str) -> str:
    """Sanitize a declared name into a terraform identifier fragment."""
    ident = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if ident and ident[0].isdigit():
        ident = f"_{ident}"
    return ident or "unnamed"


def _rule_hash(*parts: Any) -> str:
    """Short stable digest so a rule's resource name survives reordering.

    Index-based names (``..._ingress_0``) shift every downstream rule when one
    is inserted mid-list, churning terraform state for no reason. Content
    hashing also makes exact-duplicate rules collide, which is how they get
    deduped in :func:`_dedupe`.
    """
    blob = "\x00".join(str(p) for p in parts)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# View models (consumed by network_declared.tf.j2 / outputs.tf.j2)
# ---------------------------------------------------------------------------


@dataclass
class RuleView:
    """One ``aws_vpc_security_group_{ingress,egress}_rule`` resource."""

    tf_name: str
    direction: str
    sg_ref: str  # HCL expression for the security_group_id argument
    ip_protocol: str
    from_port: Optional[int]
    to_port: Optional[int]
    source_attr: str  # cidr_ipv4 | referenced_security_group_id | prefix_list_id
    source_value: str  # already-quoted literal or bare HCL expression
    description: str

    @property
    def align_width(self) -> int:
        """Column width that makes this rule block ``terraform fmt``-clean.

        fmt aligns every ``=`` in a block to the longest argument name, and the
        longest here varies: ``referenced_security_group_id`` is wider than
        ``security_group_id``, ``cidr_ipv4`` is narrower. The generated .tf is
        what a human reads when reviewing a plan, so emit it already aligned
        rather than leaving fmt drift behind.
        """
        names = ["security_group_id", "ip_protocol", "description", self.source_attr]
        if self.from_port is not None:
            names += ["from_port", "to_port"]
        return max(len(n) for n in names)


@dataclass
class SecurityGroupView:
    name: str
    tf_name: str
    description: str
    rules: list[RuleView] = field(default_factory=list)
    # True for the group rc synthesizes for an interface endpoint. Those are
    # emitted alongside their endpoint rather than in the declared-group loop.
    is_endpoint_sg: bool = False


@dataclass
class SubnetGroupView:
    name: str
    tf_name: str
    count: int
    public: bool
    egress: str
    cidr_exprs: list[str]

    @property
    def needs_nat(self) -> bool:
        return self.egress == "nat"

    @property
    def cidr_block_expr(self) -> str:
        """HCL for the ``cidr_block`` argument of a counted aws_subnet.

        One CIDR needs no indexing; several are wrapped in ``element()`` so
        the same expression works for any count.
        """
        if len(self.cidr_exprs) == 1:
            return self.cidr_exprs[0]
        return f"element([{', '.join(self.cidr_exprs)}], count.index)"


@dataclass
class EndpointServiceView:
    """One ``aws_vpc_endpoint`` resource (an endpoint group may hold several)."""

    tf_name: str
    service_suffix: str  # 'ecr.api' -> com.amazonaws.<region>.ecr.api


@dataclass
class EndpointView:
    name: str
    tf_name: str
    type: str
    private_dns: bool
    services: list[EndpointServiceView]
    subnet_group_tf_names: list[str]
    sg_tf_name: Optional[str]  # Interface only

    @property
    def subnet_ids_expr(self) -> str:
        """HCL for an interface endpoint's ``subnet_ids`` argument."""
        parts = [f"aws_subnet.{tf}[*].id" for tf in self.subnet_group_tf_names]
        if len(parts) == 1:
            return parts[0]
        return f"concat({', '.join(parts)})"

    @property
    def route_table_ids_expr(self) -> str:
        """HCL for a gateway endpoint's ``route_table_ids`` argument."""
        parts = [f"aws_route_table.{tf}.id" for tf in self.subnet_group_tf_names]
        return f"[{', '.join(parts)}]"


@dataclass
class RepositoryView:
    name: str
    tf_name: str
    repo_suffix: str
    mutable: bool
    scan_on_push: bool
    expire_untagged_days: Optional[int]
    force_delete: bool
    mirror: Optional[str]


@dataclass
class NetworkPlan:
    """Everything the declared-network templates and outputs need."""

    security_groups: list[SecurityGroupView] = field(default_factory=list)
    subnet_groups: list[SubnetGroupView] = field(default_factory=list)
    endpoints: list[EndpointView] = field(default_factory=list)
    repositories: list[RepositoryView] = field(default_factory=list)
    needs_nat: bool = False

    @property
    def is_empty(self) -> bool:
        return not (
            self.security_groups
            or self.subnet_groups
            or self.endpoints
            or self.repositories
        )

    @property
    def has_network(self) -> bool:
        """True when anything belongs in network_declared.tf.

        Deliberately excludes repositories: they render in their own file, and
        a repositories-only config must not emit a bare network header.
        """
        return bool(self.security_groups or self.subnet_groups or self.endpoints)

    @property
    def declared_security_groups(self) -> list[SecurityGroupView]:
        """Only the groups the user wrote — what `security_groups` exports."""
        return [sg for sg in self.security_groups if not sg.is_endpoint_sg]

    @property
    def endpoint_security_groups(self) -> list[SecurityGroupView]:
        """Only the groups rc synthesized for interface endpoints."""
        return [sg for sg in self.security_groups if sg.is_endpoint_sg]

    @property
    def endpoint_output_key_width(self) -> int:
        """Quoted-key column width for the ``vpc_endpoints`` output map.

        Keys are composite (``"<name>.<service suffix>"``), so unlike the other
        output maps the width cannot be derived from a single attribute in the
        template. Computed here to keep the emitted block fmt-clean.
        """
        return max(
            (
                len(f'"{ep.name}.{svc.service_suffix}"')
                for ep in self.endpoints
                for svc in ep.services
            ),
            default=0,
        )

    # -- lookups used by provider.py when wiring services --------------------

    def sg_tf_name(self, declared_name: str) -> str:
        for sg in self.security_groups:
            if sg.name == declared_name:
                return sg.tf_name
        raise KeyError(declared_name)

    def subnet_group(self, declared_name: str) -> SubnetGroupView:
        for sn in self.subnet_groups:
            if sn.name == declared_name:
                return sn
        raise KeyError(declared_name)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def build_network_plan(
    network: NetworkV2,
    repositories: dict[str, RepositoryV2],
    *,
    existing_vpc: bool = False,
    service_sg_refs: Optional[dict[str, list[str]]] = None,
) -> NetworkPlan:
    """Resolve a validated network block into terraform view models.

    ``service_sg_refs`` maps a service name to the HCL expressions for its
    effective security group ids, so a ``from: service:<name>`` rule can point
    at whatever that service actually sits on — its declared groups if it
    overrode them, the shared ``tasks`` group otherwise.
    """
    service_sg_refs = service_sg_refs or {}
    plan = NetworkPlan()

    subnet_views = _plan_subnets(network, existing_vpc=existing_vpc)
    plan.subnet_groups = subnet_views
    plan.needs_nat = any(sn.needs_nat for sn in subnet_views)

    endpoint_views = _plan_endpoints(network)
    plan.endpoints = endpoint_views

    plan.security_groups = _plan_security_groups(
        network,
        endpoint_views=endpoint_views,
        service_sg_refs=service_sg_refs,
    )
    plan.repositories = _plan_repositories(repositories)
    return plan


def _plan_subnets(network: NetworkV2, *, existing_vpc: bool) -> list[SubnetGroupView]:
    """Allocate CIDRs and emit one view per declared subnet group.

    Automatic allocation walks the declared groups in sorted-name order from
    :data:`DECLARED_SUBNET_CIDR_BASE`, so a group's block depends only on the
    set of names — adding a group later never renumbers an existing one unless
    its name sorts earlier, and even then the explicit ``cidr_offset`` escape
    hatch pins it.
    """
    views: list[SubnetGroupView] = []
    next_index = DECLARED_SUBNET_CIDR_BASE

    for name in sorted(network.subnets):
        group = network.subnets[name]
        if group.cidrs:
            cidr_exprs = [f'"{c}"' for c in group.cidrs]
        else:
            if existing_vpc:
                raise ProviderConfigError(
                    f"network.subnets.{name}: deploying into an existing VPC "
                    f"(provider_config.ecs.vpc_id), so rc cannot carve a CIDR "
                    f"block out of a range it does not own. Give explicit "
                    f"'cidrs: [...]' — one per subnet — inside the existing "
                    f"VPC's range."
                )
            base = group.cidr_offset if group.cidr_offset is not None else next_index
            if group.cidr_offset is None:
                next_index = base + group.count
            cidr_exprs = [
                f"cidrsubnet(var.vpc_cidr, {_CIDR_NEWBITS}, {base + i})"
                for i in range(group.count)
            ]
        views.append(
            SubnetGroupView(
                name=name,
                tf_name=f"rc_{tf_ident(name)}",
                count=group.count,
                public=group.public,
                egress=group.egress,
                cidr_exprs=cidr_exprs,
            )
        )

    _reject_overlapping_auto_cidrs(network, views)
    return views


def _reject_overlapping_auto_cidrs(
    network: NetworkV2, views: list[SubnetGroupView]
) -> None:
    """Catch an explicit cidr_offset colliding with an auto-allocated block.

    Per-instance validation already rejects an offset that overlaps rc's
    built-in 0-1 / 10-11 indices, but it cannot see sibling groups. Two groups
    landing on the same /24 produces a terraform apply error deep in AWS
    ("CIDR conflicts with another subnet"); saying so here is cheaper.
    """
    seen: dict[str, str] = {}
    for view in views:
        for expr in view.cidr_exprs:
            prior = seen.get(expr)
            if prior is not None:
                raise ProviderConfigError(
                    f"network.subnets.{view.name} and network.subnets.{prior} "
                    f"both resolve to {expr} — give one of them a distinct "
                    f"'cidr_offset' or explicit 'cidrs'"
                )
            seen[expr] = view.name


def _plan_endpoints(network: NetworkV2) -> list[EndpointView]:
    views: list[EndpointView] = []
    for name in sorted(network.endpoints):
        ep = network.endpoints[name]
        ep_tf = f"rc_{tf_ident(name)}"
        kind = ep.resolved_type
        views.append(
            EndpointView(
                name=name,
                tf_name=ep_tf,
                type=kind,
                private_dns=ep.private_dns,
                services=[
                    EndpointServiceView(
                        tf_name=f"{ep_tf}_{tf_ident(svc)}",
                        service_suffix=svc,
                    )
                    for svc in ep.services
                ],
                subnet_group_tf_names=[f"rc_{tf_ident(s)}" for s in ep.subnets],
                sg_tf_name=f"{ep_tf}_vpce" if kind == "Interface" else None,
            )
        )
    return views


def _plan_security_groups(
    network: NetworkV2,
    *,
    endpoint_views: list[EndpointView],
    service_sg_refs: dict[str, list[str]],
) -> list[SecurityGroupView]:
    by_name = {ep.name: ep for ep in endpoint_views}

    views: list[SecurityGroupView] = []
    # Interface endpoints each get a group whose ingress is derived entirely
    # from other groups' egress rules — collected as we walk them below.
    endpoint_sgs: dict[str, SecurityGroupView] = {
        ep.name: SecurityGroupView(
            name=f"{ep.name}-vpce",
            tf_name=ep.sg_tf_name or "",
            description=(
                f"Interface endpoint {ep.name}: ingress derived from declared "
                f"'to: endpoint:{ep.name}' egress rules."
            ),
            is_endpoint_sg=True,
        )
        for ep in endpoint_views
        if ep.type == "Interface"
    }

    for name in sorted(network.security_groups):
        sg = network.security_groups[name]
        tf_name = f"rc_{tf_ident(name)}"
        view = SecurityGroupView(
            name=name,
            tf_name=tf_name,
            description=sg.description
            or f"rc declared security group {name} (default-deny).",
        )
        self_ref = f"aws_security_group.{tf_name}.id"

        for rule in sg.ingress:
            view.rules.extend(
                _expand_rule(
                    rule,
                    sg_ref=self_ref,
                    sg_tf_name=tf_name,
                    self_ref=self_ref,
                    endpoints=by_name,
                    service_sg_refs=service_sg_refs,
                )
            )
        for rule in sg.egress:
            view.rules.extend(
                _expand_rule(
                    rule,
                    sg_ref=self_ref,
                    sg_tf_name=tf_name,
                    self_ref=self_ref,
                    endpoints=by_name,
                    service_sg_refs=service_sg_refs,
                )
            )
            # The reverse half: let the endpoint admit this group.
            if rule.ref.kind == "endpoint":
                ep = by_name.get(rule.ref.value)
                if ep is not None and ep.type == "Interface":
                    endpoint_sgs[ep.name].rules.extend(
                        _endpoint_ingress_for(rule, source_sg_tf_name=tf_name, ep=ep)
                    )

        view.rules = _dedupe(view.rules)
        views.append(view)

    for ep_sg in endpoint_sgs.values():
        ep_sg.rules = _dedupe(ep_sg.rules)
        views.append(ep_sg)

    return views


def _expand_rule(
    rule: NetworkRuleV2,
    *,
    sg_ref: str,
    sg_tf_name: str,
    self_ref: str,
    endpoints: dict[str, EndpointView],
    service_sg_refs: dict[str, list[str]],
) -> list[RuleView]:
    """Fan one declared rule out to concrete per-source, per-port resources.

    AWS's rule resources take exactly one source and one port range each, so a
    rule naming two ports, or a service that sits on three security groups,
    becomes several resources.
    """
    sources = _resolve_sources(
        rule.ref,
        self_ref=self_ref,
        endpoints=endpoints,
        service_sg_refs=service_sg_refs,
    )
    port_specs = _resolve_ports(rule, endpoints=endpoints)

    out: list[RuleView] = []
    for attr, value, source_label in sources:
        for from_port, to_port in port_specs:
            digest = _rule_hash(
                sg_tf_name,
                rule.direction,
                attr,
                value,
                rule.protocol,
                from_port,
                to_port,
            )
            out.append(
                RuleView(
                    tf_name=f"{sg_tf_name}_{rule.direction[:2]}_{digest}",
                    direction=rule.direction,
                    sg_ref=sg_ref,
                    ip_protocol=rule.protocol,
                    from_port=from_port,
                    to_port=to_port,
                    source_attr=attr,
                    source_value=value,
                    description=rule.description
                    or _default_description(rule, source_label),
                )
            )
    return out


def _default_description(rule: NetworkRuleV2, source_label: str) -> str:
    verb = "from" if rule.direction == "ingress" else "to"
    return f"rc: {rule.direction} {verb} {source_label}"


def _resolve_sources(
    ref: ResourceRef,
    *,
    self_ref: str,
    endpoints: dict[str, EndpointView],
    service_sg_refs: dict[str, list[str]],
) -> list[tuple[str, str, str]]:
    """Map a reference to (argument_name, HCL value, human label) tuples."""
    if ref.kind == "cidr":
        return [("cidr_ipv4", f'"{ref.value}"', ref.value)]
    if ref.kind == "self":
        return [("referenced_security_group_id", self_ref, "self")]
    if ref.kind == "alb":
        return [
            ("referenced_security_group_id", "aws_security_group.alb.id", "the ALB")
        ]
    if ref.kind == "sg":
        return [
            (
                "referenced_security_group_id",
                f"aws_security_group.rc_{tf_ident(ref.value)}.id",
                f"sg {ref.value}",
            )
        ]
    if ref.kind == "service":
        refs = service_sg_refs.get(ref.value)
        if not refs:
            # No declared override: the service sits on the shared group.
            refs = ["aws_security_group.tasks.id"]
        return [
            ("referenced_security_group_id", r, f"service {ref.value}") for r in refs
        ]
    if ref.kind == "endpoint":
        ep = endpoints.get(ref.value)
        if ep is None:  # unreachable: validate_network_refs ran first
            raise ProviderConfigError(f"unknown VPC endpoint reference {ref.raw!r}")
        if ep.type == "Gateway":
            # A gateway endpoint has no ENI and therefore no security group;
            # egress to it is expressed against its managed prefix list.
            return [
                (
                    "prefix_list_id",
                    f"aws_vpc_endpoint.{ep.services[0].tf_name}.prefix_list_id",
                    f"endpoint {ep.name} (gateway)",
                )
            ]
        return [
            (
                "referenced_security_group_id",
                f"aws_security_group.{ep.sg_tf_name}.id",
                f"endpoint {ep.name}",
            )
        ]
    raise ProviderConfigError(f"unhandled reference kind {ref.kind!r}")


def _resolve_ports(
    rule: NetworkRuleV2, *, endpoints: dict[str, EndpointView]
) -> list[tuple[Optional[int], Optional[int]]]:
    if rule.ports:
        return [(p.from_port, p.to_port) for p in rule.ports]
    if rule.protocol == "-1":
        # ip_protocol = "-1" means every protocol; AWS rejects a port range.
        return [(None, None)]
    if rule.ref.kind == "endpoint":
        # An interface endpoint serves 443 and nothing else, and a gateway
        # endpoint's prefix list is only useful for HTTPS to the service.
        # Defaulting to 443 keeps `to: endpoint:ecr` a one-liner without it
        # secretly meaning "all 65536 ports".
        return [(ENDPOINT_DEFAULT_PORT, ENDPOINT_DEFAULT_PORT)]
    return [(0, 65535)]


def _endpoint_ingress_for(
    rule: NetworkRuleV2, *, source_sg_tf_name: str, ep: EndpointView
) -> list[RuleView]:
    """Derive the endpoint side of a ``to: endpoint:<name>`` egress rule."""
    ep_sg_ref = f"aws_security_group.{ep.sg_tf_name}.id"
    source_ref = f"aws_security_group.{source_sg_tf_name}.id"
    out: list[RuleView] = []
    for from_port, to_port in _resolve_ports(rule, endpoints={ep.name: ep}):
        digest = _rule_hash(
            ep.sg_tf_name, "in", source_ref, rule.protocol, from_port, to_port
        )
        out.append(
            RuleView(
                tf_name=f"{ep.sg_tf_name}_in_{digest}",
                direction="ingress",
                sg_ref=ep_sg_ref,
                ip_protocol=rule.protocol,
                from_port=from_port,
                to_port=to_port,
                source_attr="referenced_security_group_id",
                source_value=source_ref,
                description=(
                    f"rc: derived from '{source_sg_tf_name}' egress to "
                    f"endpoint:{ep.name}"
                ),
            )
        )
    return out


def _dedupe(rules: list[RuleView]) -> list[RuleView]:
    """Drop exact duplicates, which would be a terraform name collision.

    Two rules hash identically only when every field that reaches AWS matches,
    so collapsing them changes nothing about the resulting access.
    """
    seen: set[str] = set()
    out: list[RuleView] = []
    for rule in rules:
        if rule.tf_name in seen:
            continue
        seen.add(rule.tf_name)
        out.append(rule)
    return out


# What a Fargate task needs to reach before it can run at all, when its
# subnet has no default route. Missing any one of these produces an opaque
# CannotPullContainerError / log-driver failure several minutes into a deploy,
# so it is worth refusing to emit.
_FARGATE_REQUIRED_ENDPOINTS = {
    "ecr.api": "authenticate to ECR",
    "ecr.dkr": "pull the image manifest",
    "s3": "download image layers (ECR stores them in S3)",
    "logs": "ship container logs (the awslogs driver)",
}
_SECRETS_ENDPOINT = "secretsmanager"


def check_endpoint_reachability(
    network: NetworkV2,
    *,
    placements: list[dict[str, Any]],
) -> None:
    """Refuse to place a task where it provably cannot start.

    A subnet group with ``egress: endpoints`` has no default route, so every
    byte a task sends must land on a VPC endpoint that (a) has an ENI in that
    same subnet group and (b) admits the task's security group. Getting either
    half wrong yields a task that pulls nothing, logs nothing, and reports
    ``CannotPullContainerError`` minutes into a rollout.

    ``placements`` entries: ``{"service", "subnet_group", "security_groups",
    "needs_secrets"}``.
    """
    for placement in placements:
        group_name = placement.get("subnet_group")
        if not group_name:
            continue
        group = network.subnets.get(group_name)
        if group is None or group.egress != "endpoints":
            continue

        svc = placement["service"]
        sg_names = list(placement.get("security_groups") or [])
        if not sg_names:
            raise ProviderConfigError(
                f"service {svc!r} is placed in network.subnets.{group_name} "
                f"(egress: endpoints, so no default route), but it has no "
                f"'security_groups:' of its own — it would sit on the shared "
                f"${{project}}-tasks group, which no VPC endpoint admits. "
                f"Declare a group in network.security_groups with the egress "
                f"rules this service needs and name it on the service."
            )

        reachable = _reachable_endpoint_services(
            network, sg_names=sg_names, subnet_group=group_name
        )
        required = dict(_FARGATE_REQUIRED_ENDPOINTS)
        if placement.get("needs_secrets"):
            required[_SECRETS_ENDPOINT] = "read its Secrets Manager secrets"

        missing = {s: why for s, why in required.items() if s not in reachable}
        if missing:
            lines = "\n".join(
                f"    - {s}  ({why})" for s, why in sorted(missing.items())
            )
            raise ProviderConfigError(
                f"service {svc!r} is placed in network.subnets.{group_name} "
                f"(egress: endpoints, so no default route), but cannot reach "
                f"the AWS services a Fargate task needs to start:\n{lines}\n"
                f"  Each one needs a network.endpoints entry that is attached "
                f"to subnet group {group_name!r} AND is named in an "
                f"'to: endpoint:<name>' egress rule on {sg_names}.\n"
                f"  Reachable today: {sorted(reachable) or 'nothing'}."
            )


def _reachable_endpoint_services(
    network: NetworkV2, *, sg_names: list[str], subnet_group: str
) -> set[str]:
    """AWS endpoint service suffixes a task on ``sg_names`` can actually reach.

    Both conditions must hold: an egress rule naming the endpoint, and the
    endpoint having presence in the task's own subnet group. An endpoint
    attached only to some other subnet group has no ENI (or, for a gateway
    endpoint, no route-table entry) where this task lives.
    """
    reachable: set[str] = set()
    for sg_name in sg_names:
        sg = network.security_groups.get(sg_name)
        if sg is None:
            continue
        for rule in sg.egress:
            if rule.ref.kind != "endpoint":
                continue
            ep = network.endpoints.get(rule.ref.value)
            if ep is None or subnet_group not in ep.subnets:
                continue
            reachable.update(ep.services)
    return reachable


def _plan_repositories(repositories: dict[str, RepositoryV2]) -> list[RepositoryView]:
    return [
        RepositoryView(
            name=name,
            tf_name=f"rc_repo_{tf_ident(name)}",
            repo_suffix=name,
            mutable=repositories[name].mutable,
            scan_on_push=repositories[name].scan_on_push,
            expire_untagged_days=repositories[name].expire_untagged_days,
            force_delete=repositories[name].force_delete,
            mirror=repositories[name].mirror,
        )
        for name in sorted(repositories)
    ]
