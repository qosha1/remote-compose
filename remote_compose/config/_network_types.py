"""rc.yml v2 ``network:`` / ``repositories:`` — standalone infrastructure
primitives that exist in their own right rather than being derived from a
service.

Motivation
----------
Before this module, every AWS network resource rc emitted was a hardcoded
singleton in a Jinja template: exactly two security groups (``alb`` and
``tasks``), two public subnets, two *unrouted* private subnets, and no VPC
endpoints. Every service landed on the same ``tasks`` SG, which carries
implicit ALB ingress, ``self = true``, and blanket ``egress -> 0.0.0.0/0``.

That is fine for "a web app and its workers" and useless for anything that
needs an isolated blast radius — in particular a consumer that launches its
own ECS tasks out-of-band (a backend calling ``run_task``) and has no rc
service to hang an SG off. Such a consumer had nothing to attach but the
shared SG.

The fix is not to model those consumers as services. It is to let the
network layer be *declared* instead of *derived*, and to hand back the
resulting resource ids. A declared resource may be referenced by an rc
service, by another declared resource, or by nothing at all — in which case
it simply exists and is exported.

Default-deny
------------
Everything here is deny-by-default and additive-only:

* A declared security group with no ``ingress`` admits nothing. With no
  ``egress`` it reaches nothing — the AWS provider strips the implicit
  allow-all egress rule AWS attaches at creation, so "no rule" really is
  "no access".
* No ALB ingress rule is ever injected into a declared SG. A service that
  needs it says so with ``from: alb``.
* A private subnet group gets no ``0.0.0.0/0`` route unless its ``egress``
  mode asks for one.

None of this touches the built-in ``alb`` / ``tasks`` groups or the default
subnets: a config with no ``network:`` block emits byte-identical terraform
to before.

Reference grammar
-----------------
Rule sources/destinations are ``<kind>:<value>`` strings, plus two bare
keywords::

    sg:<name>        a security group declared in network.security_groups
    service:<name>   the effective security group(s) of an rc service
    endpoint:<name>  a VPC endpoint declared in network.endpoints
                     (interface -> its SG; gateway -> its prefix list)
    cidr:<cidr>      a literal IPv4 CIDR block
    alb              the rc-managed load balancer's security group
    self             the declaring security group itself

Parsing and per-instance validation live here; cross-resource reference
resolution lives in :func:`validate_network_refs`, which needs the whole
config in hand. Turning a validated :class:`NetworkV2` into terraform is the
provider's job (``provider/ecs/network_plan.py``) — this module stays
provider-agnostic and dependency-light.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ._errors import ConfigError

# Egress modes for a subnet group. Public subnets are always "igw"; the
# other three are the private-subnet menu:
#   endpoints — no default route at all. Reachability comes exclusively from
#               VPC endpoints (interface ENIs + gateway route entries). This
#               is the NAT-free path: tasks pull from ECR, ship logs, and
#               call STS without a single byte of internet routing.
#   nat       — a project-shared NAT gateway in the first public subnet.
#               Real egress, real hourly + per-GB cost.
#   none      — local routes only. Fully isolated.
VALID_SUBNET_EGRESS = {"igw", "endpoints", "nat", "none"}

VALID_ENDPOINT_TYPES = {"Interface", "Gateway"}

# AWS ships exactly two gateway-type endpoints; everything else is an
# interface endpoint backed by an ENI (and therefore by a security group).
GATEWAY_ENDPOINT_SERVICES = {"s3", "dynamodb"}

VALID_REF_KINDS = {"sg", "service", "endpoint", "cidr"}
BARE_REFS = {"alb", "self"}

_PROTOCOL_ALIASES = {"all": "-1", "any": "-1", "-1": "-1"}
# No icmpv6: rc rejects IPv6 CIDRs outright (see ResourceRef.parse), so an
# icmpv6 rule could only ever pair with an IPv4 source, which AWS rejects.
# Accepting it here would emit `ip_protocol = "icmpv6"` next to `cidr_ipv4`
# and fail at apply.
VALID_PROTOCOLS = {"tcp", "udp", "icmp", "-1"}

# Declared resource names become terraform identifiers, AWS Name tags, and
# environment-variable suffixes in `rc outputs --env`. Keep them boring.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

# Free-text that rc interpolates into generated HCL. Restricted to what AWS
# actually accepts for a security-group / rule description, which conveniently
# excludes the quote and backslash that would break the emitted string, and
# the newline that would let a value escape its line. `$` IS permitted by AWS,
# so the emitter still has to escape `${` -- see _hcl_safe below.
_DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9 ._\-:/()#,@\[\]+=&;{}!$*]{1,255}$")

# An OCI image reference. Constrained here rather than escaped downstream:
# `mirror` is rendered into a comment in the generated terraform, and a value
# containing a newline would close that comment and let the rest of the string
# be parsed as HCL -- i.e. arbitrary resources, from a file that travels in the
# repo being deployed.
_IMAGE_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,199}"  # registry/namespace/name
    r"(:[A-Za-z0-9][A-Za-z0-9._\-]{0,127})?"  # :tag
    r"(@sha256:[a-f0-9]{64})?$"  # @digest
)


def _validate_description(value: Any, where: str) -> None:
    """Reject free text that cannot be safely rendered into HCL.

    Validating beats escaping here: every rejected character is one AWS would
    refuse anyway, so nothing legitimate is lost, and the failure lands at
    parse time with a pointer to the offending field instead of as an
    unparseable .tf file.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise ConfigError(f"{where}: description must be a string")
    if not _DESCRIPTION_RE.match(value):
        raise ConfigError(
            f"{where}: description {value!r} contains characters AWS does not "
            f"accept for a security-group description (allowed: letters, "
            f"digits, spaces and ._-:/()#,@[]+=&;{{}}!$* — no quotes, "
            f"backslashes or newlines), or exceeds 255 characters"
        )


# CIDR indices 0..1 (public) and 10..11 (private) are taken by the built-in
# subnets in network.tf.j2. Declared groups allocate from here up so a new
# `network.subnets` entry can never silently overlap them.
DECLARED_SUBNET_CIDR_BASE = 20
_MAX_CIDR_INDEX = 255


def _validate_name(kind: str, name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ConfigError(
            f"{kind} name {name!r} is invalid: use lowercase letters, digits "
            f"and hyphens (max 32 chars, must start and end alphanumeric)"
        )


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceRef:
    """A parsed ``<kind>:<value>`` rule source or destination."""

    kind: str
    value: str
    raw: str

    @classmethod
    def parse(cls, raw: Any, *, where: str) -> "ResourceRef":
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError(
                f"{where}: reference must be a non-empty string, got {raw!r}"
            )
        raw = raw.strip()
        if raw in BARE_REFS:
            return cls(kind=raw, value="", raw=raw)
        if ":" not in raw:
            raise ConfigError(
                f"{where}: reference {raw!r} is missing a kind prefix — expected "
                f"one of {sorted(k + ':' for k in VALID_REF_KINDS)} or a bare "
                f"{sorted(BARE_REFS)}"
            )
        kind, _, value = raw.partition(":")
        kind = kind.strip()
        value = value.strip()
        if kind not in VALID_REF_KINDS:
            raise ConfigError(
                f"{where}: unknown reference kind {kind!r} in {raw!r} "
                f"(supported: {sorted(VALID_REF_KINDS)} or a bare "
                f"{sorted(BARE_REFS)})"
            )
        if not value:
            raise ConfigError(f"{where}: reference {raw!r} has an empty value")
        if kind == "cidr":
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError as exc:
                raise ConfigError(
                    f"{where}: {raw!r} is not a valid CIDR block — {exc}"
                ) from exc
            if net.version != 4:
                raise ConfigError(
                    f"{where}: {raw!r} is IPv6; only IPv4 CIDRs are supported"
                )
            value = str(net)
        else:
            _validate_name(f"{where}: reference target", value)
        return cls(kind=kind, value=value, raw=raw)


@dataclass(frozen=True)
class PortRange:
    from_port: int
    to_port: int

    @classmethod
    def parse(cls, raw: Any, *, where: str) -> "PortRange":
        if isinstance(raw, bool):  # bool is an int subclass; reject explicitly
            raise ConfigError(f"{where}: port {raw!r} must be a number or 'a-b'")
        if isinstance(raw, int):
            lo = hi = raw
        elif isinstance(raw, str) and "-" in raw.strip():
            lo_s, _, hi_s = raw.strip().partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ConfigError(
                    f"{where}: port range {raw!r} must be 'from-to', e.g. '8000-8100'"
                ) from exc
        elif isinstance(raw, str):
            try:
                lo = hi = int(raw.strip())
            except ValueError as exc:
                raise ConfigError(
                    f"{where}: port {raw!r} must be a number or a 'from-to' range"
                ) from exc
        else:
            raise ConfigError(
                f"{where}: port {raw!r} must be a number or a 'from-to' range"
            )
        for p in (lo, hi):
            if not 0 <= p <= 65535:
                raise ConfigError(f"{where}: port {p} is outside 0-65535")
        if lo > hi:
            raise ConfigError(f"{where}: port range {raw!r} is inverted ({lo} > {hi})")
        return cls(from_port=lo, to_port=hi)


@dataclass
class NetworkRuleV2:
    """One ingress or egress rule on a declared security group.

    ``ports`` empty means "every port for this protocol". Combined with the
    default ``protocol: tcp`` that is all 65536 TCP ports — deliberately
    noisy to write, because you should be naming ports.
    """

    direction: str
    ref: ResourceRef
    ports: list[PortRange] = field(default_factory=list)
    protocol: str = "tcp"
    description: Optional[str] = None

    @classmethod
    def parse(cls, raw: Any, *, direction: str, where: str) -> "NetworkRuleV2":
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{where}: each {direction} rule must be a mapping, got "
                f"{type(raw).__name__}"
            )
        key = "from" if direction == "ingress" else "to"
        wrong_key = "to" if direction == "ingress" else "from"
        if wrong_key in raw:
            raise ConfigError(
                f"{where}: {direction} rules use {key!r}, not {wrong_key!r}"
            )
        if key not in raw:
            raise ConfigError(f"{where}: {direction} rule is missing {key!r}")
        unknown = set(raw) - {key, "ports", "protocol", "description"}
        if unknown:
            raise ConfigError(
                f"{where}: unknown {direction} rule key(s) {sorted(unknown)} "
                f"(supported: {key}, ports, protocol, description)"
            )

        ref = ResourceRef.parse(raw[key], where=where)

        protocol = str(raw.get("protocol", "tcp")).strip().lower()
        protocol = _PROTOCOL_ALIASES.get(protocol, protocol)
        if protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"{where}: protocol {raw.get('protocol')!r} must be one of "
                f"{sorted(VALID_PROTOCOLS)} (or 'all')"
            )

        ports_raw = raw.get("ports")
        if ports_raw is None:
            ports: list[PortRange] = []
        elif isinstance(ports_raw, list):
            ports = [PortRange.parse(p, where=where) for p in ports_raw]
        else:
            ports = [PortRange.parse(ports_raw, where=where)]
        if ports and protocol == "-1":
            raise ConfigError(
                f"{where}: protocol 'all' cannot carry a port list — "
                f"'-1' means every protocol, and only tcp/udp have ports"
            )

        desc = raw.get("description")
        _validate_description(desc, where)

        return cls(
            direction=direction,
            ref=ref,
            ports=ports,
            protocol=protocol,
            description=desc,
        )

    def validate(self, *, where: str) -> None:
        if self.direction == "ingress" and self.ref.kind == "endpoint":
            raise ConfigError(
                f"{where}: 'endpoint:{self.ref.value}' is not a valid ingress "
                f"source — VPC endpoints are destinations you egress TO, not "
                f"originators of traffic"
            )


# ---------------------------------------------------------------------------
# Declared resources
# ---------------------------------------------------------------------------


@dataclass
class SecurityGroupV2:
    """A standalone security group, attachable to rc services or to nothing.

    Not owned by any service. Its id is exported so an out-of-band consumer
    (a backend calling ``run_task``, a Lambda, a hand-run instance) can
    attach to it without rc managing that consumer.
    """

    name: str
    description: Optional[str] = None
    ingress: list[NetworkRuleV2] = field(default_factory=list)
    egress: list[NetworkRuleV2] = field(default_factory=list)

    def validate(self) -> None:
        _validate_name("network.security_groups", self.name)
        where = f"network.security_groups.{self.name}"
        _validate_description(self.description, where)
        for rule in self.ingress:
            rule.validate(where=f"{where}.ingress")
        for rule in self.egress:
            rule.validate(where=f"{where}.egress")


@dataclass
class SubnetGroupV2:
    """A named set of subnets (one per AZ) with its own route table.

    The route table is what makes the group meaningful: ``public`` gets an
    IGW default route, ``egress: nat`` gets a NAT default route, and
    ``egress: endpoints`` / ``none`` get no default route at all.
    """

    name: str
    public: bool = False
    egress: str = "none"
    count: int = 2
    # Explicit CIDRs bypass automatic cidrsubnet() allocation. Required when
    # deploying into an existing VPC, where rc does not know the real CIDR
    # and cannot safely carve a block out of it.
    cidrs: list[str] = field(default_factory=list)
    cidr_offset: Optional[int] = None

    def validate(self) -> None:
        _validate_name("network.subnets", self.name)
        where = f"network.subnets.{self.name}"
        if self.egress not in VALID_SUBNET_EGRESS:
            raise ConfigError(
                f"{where}: egress must be one of {sorted(VALID_SUBNET_EGRESS)}, "
                f"got {self.egress!r}"
            )
        if self.public and self.egress != "igw":
            raise ConfigError(
                f"{where}: public subnets always egress via the internet "
                f"gateway — drop 'egress: {self.egress}' or set public: false"
            )
        if not self.public and self.egress == "igw":
            raise ConfigError(
                f"{where}: egress 'igw' requires public: true (a private "
                f"subnet has no internet gateway route by definition)"
            )
        if not isinstance(self.count, int) or isinstance(self.count, bool):
            raise ConfigError(f"{where}: count must be an integer")
        if not 1 <= self.count <= 6:
            raise ConfigError(
                f"{where}: count must be between 1 and 6 (one subnet per AZ), "
                f"got {self.count}"
            )
        if self.cidrs:
            if len(self.cidrs) != self.count:
                raise ConfigError(
                    f"{where}: {len(self.cidrs)} explicit cidrs for count "
                    f"{self.count} — give one CIDR per subnet"
                )
            for c in self.cidrs:
                try:
                    ipaddress.ip_network(c, strict=True)
                except ValueError as exc:
                    raise ConfigError(
                        f"{where}: cidr {c!r} is invalid — {exc}"
                    ) from exc
            if self.cidr_offset is not None:
                raise ConfigError(
                    f"{where}: cidrs and cidr_offset are mutually exclusive"
                )
        if self.cidr_offset is not None:
            if isinstance(self.cidr_offset, bool) or not isinstance(
                self.cidr_offset, int
            ):
                raise ConfigError(f"{where}: cidr_offset must be an integer")
            if not 0 <= self.cidr_offset <= _MAX_CIDR_INDEX:
                raise ConfigError(
                    f"{where}: cidr_offset must be 0-{_MAX_CIDR_INDEX}, got "
                    f"{self.cidr_offset}"
                )
            overlap = set(range(self.cidr_offset, self.cidr_offset + self.count)) & {
                0,
                1,
                10,
                11,
            }
            if overlap:
                raise ConfigError(
                    f"{where}: cidr_offset {self.cidr_offset} (count "
                    f"{self.count}) overlaps index(es) {sorted(overlap)}, which "
                    f"belong to rc's built-in public (0-1) / private (10-11) "
                    f"subnets"
                )


@dataclass
class VpcEndpointV2:
    """One or more AWS service endpoints published under a single handle.

    ``endpoints: {ecr: {services: [ecr.api, ecr.dkr]}}`` emits two endpoint
    resources sharing one security group, referenceable as a unit via
    ``endpoint:ecr``. That grouping matters because pulling an image needs
    both ``ecr.api`` and ``ecr.dkr`` — splitting them across two handles is
    a foot-gun with no upside.

    Interface endpoints get an rc-managed SG whose ingress is derived: every
    declared SG with an ``to: endpoint:<name>`` egress rule is granted the
    matching ingress on the endpoint side automatically. Declaring one half
    of a two-sided rule and silently getting no connectivity is the single
    most common VPC-endpoint failure, so rc closes it.
    """

    name: str
    services: list[str] = field(default_factory=list)
    type: Optional[str] = None
    subnets: list[str] = field(default_factory=list)
    private_dns: bool = True

    @property
    def resolved_type(self) -> str:
        if self.type:
            return self.type
        if self.services and all(s in GATEWAY_ENDPOINT_SERVICES for s in self.services):
            return "Gateway"
        return "Interface"

    def validate(self) -> None:
        _validate_name("network.endpoints", self.name)
        where = f"network.endpoints.{self.name}"
        if not self.services:
            raise ConfigError(
                f"{where}: services is required (e.g. [ecr.api, ecr.dkr], "
                f"[logs], [s3])"
            )
        for s in self.services:
            if not isinstance(s, str) or not re.match(
                r"^[a-z0-9][a-z0-9.\-_]{0,62}$", s
            ):
                raise ConfigError(
                    f"{where}: service {s!r} is not a valid AWS endpoint "
                    f"service suffix (the part after "
                    f"'com.amazonaws.<region>.')"
                )
        if len(set(self.services)) != len(self.services):
            raise ConfigError(f"{where}: duplicate entries in services")
        if self.type is not None and self.type not in VALID_ENDPOINT_TYPES:
            raise ConfigError(
                f"{where}: type must be one of {sorted(VALID_ENDPOINT_TYPES)}, "
                f"got {self.type!r}"
            )
        gateways = [s for s in self.services if s in GATEWAY_ENDPOINT_SERVICES]
        if gateways and len(gateways) != len(self.services):
            raise ConfigError(
                f"{where}: cannot mix gateway service(s) {sorted(gateways)} with "
                f"interface services in one endpoint — gateway endpoints attach "
                f"to route tables and interface endpoints to subnets. Declare "
                f"them as separate entries."
            )
        if self.resolved_type == "Gateway" and not self.private_dns:
            # private_dns is an interface-only concept; silently ignoring a
            # false here would hide a misunderstanding.
            raise ConfigError(
                f"{where}: private_dns does not apply to gateway endpoints "
                f"(they work via route-table entries, not DNS)"
            )
        if not self.subnets:
            raise ConfigError(
                f"{where}: subnets is required — name the network.subnets "
                f"group(s) this endpoint serves "
                f"({'route tables to attach' if self.resolved_type == 'Gateway' else 'subnets to place ENIs in'})"
            )


@dataclass
class RepositoryV2:
    """A standalone ECR repository, not tied to any service's build.

    The use case is a mirror: pull ``postgres:16-alpine`` from Docker Hub
    once, push it here, and let private-subnet tasks pull it through the ECR
    endpoint with no internet route and no Docker Hub rate limit. ``mirror``
    records the upstream reference; rc creates the repository and reports
    both in ``rc outputs`` — pushing the image stays yours.
    """

    name: str
    mirror: Optional[str] = None
    mutable: bool = True
    scan_on_push: bool = True
    expire_untagged_days: Optional[int] = None
    force_delete: bool = False

    def validate(self) -> None:
        _validate_name("repositories", self.name)
        where = f"repositories.{self.name}"
        if self.mirror is not None:
            if not isinstance(self.mirror, str) or not _IMAGE_REF_RE.match(
                self.mirror.strip()
            ):
                raise ConfigError(
                    f"{where}: mirror {self.mirror!r} is not a valid image "
                    f"reference (expected something like "
                    f"'postgres:16-alpine' or "
                    f"'registry/name@sha256:<64 hex>')"
                )
        if self.expire_untagged_days is not None:
            if isinstance(self.expire_untagged_days, bool) or not isinstance(
                self.expire_untagged_days, int
            ):
                raise ConfigError(f"{where}: expire_untagged_days must be an integer")
            if self.expire_untagged_days < 1:
                raise ConfigError(
                    f"{where}: expire_untagged_days must be >= 1, got "
                    f"{self.expire_untagged_days}"
                )


@dataclass
class NetworkV2:
    """The whole ``network:`` block."""

    security_groups: dict[str, SecurityGroupV2] = field(default_factory=dict)
    subnets: dict[str, SubnetGroupV2] = field(default_factory=dict)
    endpoints: dict[str, VpcEndpointV2] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.security_groups or self.subnets or self.endpoints)

    def validate(self) -> None:
        for sg in self.security_groups.values():
            sg.validate()
        for sn in self.subnets.values():
            sn.validate()
        for ep in self.endpoints.values():
            ep.validate()


# ---------------------------------------------------------------------------
# Cross-resource validation
# ---------------------------------------------------------------------------


def validate_network_refs(
    network: NetworkV2,
    *,
    service_names: Optional[set[str]] = None,
    service_sg_overrides: Optional[dict[str, list[str]]] = None,
    service_subnet_placements: Optional[dict[str, Optional[str]]] = None,
    public_services: Optional[dict[str, Optional[int]]] = None,
    has_alb: Optional[bool] = None,
) -> None:
    """Resolve every reference in the network block against the full config.

    Split out from the per-instance ``validate()`` methods because a rule can
    only be checked once every declared name — and every *service* name — is
    known.

    Runs in two phases, because rc.yml alone does not know the full service
    set: a service may exist only in docker-compose.yml and never appear in
    rc.yml. ``parse()`` calls this with ``service_names=None`` /
    ``has_alb=None``, which skips the two checks that need the merged view;
    the ECS provider calls it again with both populated once compose has been
    read. Everything else — declared-name resolution, self-reference, ALB
    reachability for public services, orphan endpoints — is fully decidable
    from rc.yml and is checked in both passes.
    """
    service_sg_overrides = service_sg_overrides or {}
    service_subnet_placements = service_subnet_placements or {}
    public_services = public_services or {}

    sg_names = set(network.security_groups)
    subnet_names = set(network.subnets)
    endpoint_names = set(network.endpoints)

    def _check_ref(ref: ResourceRef, where: str) -> None:
        if ref.kind == "sg" and ref.value not in sg_names:
            raise ConfigError(
                f"{where}: 'sg:{ref.value}' does not name a declared security "
                f"group (known: {sorted(sg_names) or 'none'})"
            )
        if (
            ref.kind == "service"
            and service_names is not None
            and ref.value not in service_names
        ):
            raise ConfigError(
                f"{where}: 'service:{ref.value}' does not name a service "
                f"(known: {sorted(service_names) or 'none'})"
            )
        if ref.kind == "endpoint" and ref.value not in endpoint_names:
            raise ConfigError(
                f"{where}: 'endpoint:{ref.value}' does not name a declared VPC "
                f"endpoint (known: {sorted(endpoint_names) or 'none'})"
            )
        if ref.kind == "alb" and has_alb is False:
            raise ConfigError(
                f"{where}: 'alb' references the load balancer's security "
                f"group, but this stack has no public service and therefore "
                f"no ALB"
            )

    for sg_name, sg in network.security_groups.items():
        for direction, rules in (("ingress", sg.ingress), ("egress", sg.egress)):
            for i, rule in enumerate(rules):
                where = f"network.security_groups.{sg_name}.{direction}[{i}]"
                _check_ref(rule.ref, where)
                if rule.ref.kind == "sg" and rule.ref.value == sg_name:
                    raise ConfigError(
                        f"{where}: 'sg:{sg_name}' refers to the group being "
                        f"declared — use the bare 'self' keyword instead"
                    )

    for ep_name, ep in network.endpoints.items():
        for sn in ep.subnets:
            if sn not in subnet_names:
                raise ConfigError(
                    f"network.endpoints.{ep_name}.subnets: {sn!r} does not name "
                    f"a declared subnet group (known: "
                    f"{sorted(subnet_names) or 'none'})"
                )

    # Service-side references into the network block.
    for svc_name, sgs in service_sg_overrides.items():
        for sg in sgs:
            if sg not in sg_names:
                raise ConfigError(
                    f"service {svc_name!r}: security_groups entry {sg!r} does "
                    f"not name a declared network.security_groups group "
                    f"(known: {sorted(sg_names) or 'none'})"
                )
    for svc_name, subnet in service_subnet_placements.items():
        if subnet is not None and subnet not in subnet_names:
            raise ConfigError(
                f"service {svc_name!r}: subnets {subnet!r} does not name a "
                f"declared network.subnets group (known: "
                f"{sorted(subnet_names) or 'none'})"
            )

    _check_public_services_keep_alb_reachability(
        network,
        service_sg_overrides=service_sg_overrides,
        public_services=public_services,
    )
    _check_interface_endpoints_are_reachable(network)


def _check_public_services_keep_alb_reachability(
    network: NetworkV2,
    *,
    service_sg_overrides: dict[str, list[str]],
    public_services: dict[str, Optional[int]],
) -> None:
    """A public service that replaces the shared SG must re-admit the ALB.

    ``security_groups:`` replaces rather than appends, which is the whole
    point — but replacing it on a ``public: true`` service also drops the
    ALB ingress the shared ``tasks`` group carries. The target group would
    then never pass a health check, and the failure surfaces minutes later
    as an ECS deployment circuit-breaker rollback rather than as a config
    error. Catch it at parse time.

    ``from: alb`` satisfies this; so does an explicit CIDR that covers the
    VPC, since a user writing that has clearly thought about it.
    """
    for svc_name, port in public_services.items():
        names = service_sg_overrides.get(svc_name)
        if not names:
            continue  # keeps the shared tasks SG, which already admits the ALB
        admits_alb = False
        for sg_name in names:
            sg = network.security_groups.get(sg_name)
            if sg is None:
                continue  # already reported by the reference check above
            for rule in sg.ingress:
                if rule.ref.kind in ("alb", "cidr"):
                    admits_alb = True
                    break
            if admits_alb:
                break
        if not admits_alb:
            port_hint = port if port is not None else "<port>"
            raise ConfigError(
                f"service {svc_name!r}: public=true, but its security_groups "
                f"{names} replace the shared ${{project}}-tasks group and none "
                f"of them admit the load balancer — the target group could "
                f"never pass a health check. Add "
                f"'ingress: [{{from: alb, ports: [{port_hint}]}}]' to one of "
                f"them, or drop public: true."
            )


def _check_interface_endpoints_are_reachable(network: NetworkV2) -> None:
    """Reject interface endpoints nothing can reach.

    An interface endpoint with no inbound grant is a paid ENI per AZ that
    serves no traffic. Because rc derives the endpoint SG's ingress purely
    from other groups' ``to: endpoint:<name>`` rules, "nobody egresses to it"
    means "its SG is empty" means "it is unreachable". That is always a
    mistake, and an expensive silent one.
    """
    referenced: set[str] = set()
    for sg in network.security_groups.values():
        for rule in sg.egress:
            if rule.ref.kind == "endpoint":
                referenced.add(rule.ref.value)

    orphans = [
        name
        for name, ep in sorted(network.endpoints.items())
        if ep.resolved_type == "Interface" and name not in referenced
    ]
    if orphans:
        raise ConfigError(
            f"interface endpoint(s) {orphans} are unreachable: no declared "
            f"security group has an 'to: endpoint:<name>' egress rule for "
            f"them, so rc derives an empty ingress and the endpoint ENIs "
            f"would serve no traffic. Add the egress rule to whichever group "
            f"needs them, or remove the endpoint."
        )
