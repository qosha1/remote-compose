"""rc.yml v2 ``task_groups:`` — N compose services in ONE ECS task (rc-l6l8).

Every compose service becomes its own awsvpc task today, and every awsvpc task
burns one branch ENI. On the estate this was measured against, the ENI dimension
needed twice the instances memory did (4 x m6i.large where memory wanted 2), and
a real ``AssociationLimitExceeded`` placement failure proved the ceiling was
live. Grouping co-locates containers behind one ENI without touching the
security boundary — the alternative, ``network_mode = bridge``, collapses every
task onto the host ENI under one shared SG.

The shape (decided in rc-4seu)::

    task_groups:
      nginx:                                   # group name == ECS service name
        services: [nginx, django, frontend]    #            == Cloud Map A record
        ingress: nginx                         # optional
        memory: 3072                           # optional, default = sum
      postgres:
        services: [postgres, redis]

Three properties make this safe to add to an existing stack:

1. **Additive and optional.** No ``task_groups`` block means every service is an
   implicit group of one named after itself, which renders byte-identical to
   what rc emitted before groups existed. ``rc init --from-compose`` therefore
   needs no change at all.
2. **Group properties DERIVE from members.** ``replicas``, ``stateful``,
   ``auto_roll``, ``deployment``, ``launch_type``, placement and ``iam_role``
   are all read off the members, and disagreement is an ERROR rather than a
   coercion. Silently making the app group stateful would turn a rolling deploy
   into a stop-then-start outage from an rc.yml that reads innocent.
3. **Only two knobs live on the group**, because only these two have no
   member-level meaning: ``memory`` (the task-level reservation, default the sum
   of its members) and ``ingress`` (which container the ALB target group points
   at).

Validation splits in two, for the same reason ``validate_network_refs`` does:
``parse()`` sees only rc.yml, but ``build_deploy_context`` resolves the deploy
set as ``compose_names | rc_names``, so a group may legitimately name a service
that exists only in docker-compose.yml. Structure is checked at parse time;
every semantic reject lives in :func:`validate_task_groups`, which the ECS
provider calls once the merged specs are known.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ._errors import ConfigError

#: Keys accepted inside one ``task_groups.<name>`` body.
VALID_TASK_GROUP_KEYS = {"services", "ingress", "memory"}

#: Fields every member of a group must agree on, because each is a property of
#: the ECS *task* or *service* rather than of a container. ``iam_role`` is the
#: one most easily missed: ``services.tf.j2`` renders a single
#: ``task_role_arn`` per task definition, so grouping collapses per-service IAM
#: onto one role — the same class of objection that ruled out
#: ``network_mode = bridge`` for security groups.
#:
#: Maps the ServiceSpec attribute onto the rc.yml key an operator would edit.
UNIFORM_MEMBER_FIELDS = {
    "replicas": "replicas",
    "auto_roll": "auto_roll",
    "iam_role": "iam_role",
    "subnet_group": "subnets",
    "ephemeral_storage": "ephemeral_storage",
}

# DERIVED task/service fields are deliberately NOT in the map above, even
# though a group must be uniform in them too. They cannot be compared here
# because the rc.yml value is not the rendered value:
#
#   launch_type  -- None until the provider applies default_launch_type, so two
#                   members that both resolve to EC2 (one explicit, one by
#                   default) would be reported as a conflict they do not have.
#   stateful     -- ``_is_stateful_service`` also fires on an EFS volume and on
#                   a singleton-scheduler name, so comparing the raw flag lets
#                   [django, postgres-with-volumes] pass while rendering
#                   whichever rollout policy the FIRST member implies. At
#                   min_healthy=100/max=200 that is two postgres containers on
#                   one EFS access point during a roll.
#   deployment   -- ``_deployment_percents`` folds `stateful` in, so it
#                   inherits the same problem.
#
# The ECS provider re-checks these three against the COMPUTED per-service views
# just before it folds them into a group. See
# ``_validate_group_render_uniformity``.
DERIVED_UNIFORM_FIELDS = ("launch_type", "stateful", "deployment")


@dataclass
class TaskGroupV2:
    """One declared ``task_groups:`` entry, structurally validated."""

    name: str
    services: list[str]
    #: Which member the ALB target group points at. Required only when more
    #: than one member is public — a task has one ENI, but a target group
    #: names one container_name/container_port pair.
    ingress: Optional[str] = None
    #: Task-level memory. ``None`` means the sum of member memory, which is
    #: what the task actually needs; set it to harvest the slack.
    memory: Optional[int] = None

    def validate(self) -> None:
        if not self.name:
            raise ConfigError("task_groups: group names cannot be empty")
        if not isinstance(self.services, list) or not self.services:
            raise ConfigError(
                f"task_groups.{self.name}: services must be a non-empty list "
                f"of service names"
            )
        seen: set[str] = set()
        for member in self.services:
            if not isinstance(member, str) or not member:
                raise ConfigError(
                    f"task_groups.{self.name}.services must contain service "
                    f"names (strings), got {member!r}"
                )
            if member in seen:
                raise ConfigError(f"task_groups.{self.name} lists {member!r} twice")
            seen.add(member)
        if self.ingress is not None and self.ingress not in seen:
            raise ConfigError(
                f"task_groups.{self.name}: ingress {self.ingress!r} is not a "
                f"member of this group (members: {sorted(seen)})"
            )
        if self.memory is not None and (
            not isinstance(self.memory, int)
            or isinstance(self.memory, bool)
            or self.memory <= 0
        ):
            raise ConfigError(
                f"task_groups.{self.name}: memory must be a positive integer "
                f"(MiB), got {self.memory!r}"
            )


@dataclass
class ResolvedTaskGroup:
    """A group with its members resolved against the merged service set.

    This — not :class:`TaskGroupV2` — is what the provider and the templates
    consume. Every service is in exactly one, including the ones nobody
    grouped, so downstream code never branches on "is this grouped?".
    """

    name: str
    members: list[str]
    memory: int
    ingress: Optional[str] = None
    #: True when nobody declared this group: a single service standing alone.
    #: The byte-identical rendering path depends on these being
    #: indistinguishable from pre-grouping output, not on a template branch.
    is_implicit: bool = False
    #: Every hostname this group retires. A group registers ONE Cloud Map
    #: record at its own name (ECS allows one service registry per service),
    #: so members whose name is not the group name lose theirs. Empty for a
    #: group of one, and for a group named after one of its members it
    #: excludes that member. ``rc plan`` warns with this list (rc-2zzd).
    retired_hostnames: list[str] = field(default_factory=list)


def _members_of(groups: dict[str, TaskGroupV2]) -> dict[str, str]:
    """member service name -> the group that claims it."""
    owner: dict[str, str] = {}
    for gname in sorted(groups):
        for member in groups[gname].services:
            owner[member] = gname
    return owner


def validate_task_group_membership(groups: dict[str, TaskGroupV2]) -> None:
    """Cross-group structural checks. Safe on the rc.yml-only set.

    Membership overlap needs no knowledge of which services exist, so unlike
    everything in :func:`validate_task_groups` it can run at parse time.
    """
    claimed: dict[str, str] = {}
    for gname in sorted(groups):
        for member in groups[gname].services:
            first = claimed.get(member)
            if first is not None:
                raise ConfigError(
                    f"task_groups: {member!r} is in two task groups "
                    f"({first!r} and {gname!r}) — a service runs in exactly "
                    f"one task"
                )
            claimed[member] = gname


def resolve_task_groups(
    groups: dict[str, TaskGroupV2],
    specs: dict[str, Any],
) -> dict[str, ResolvedTaskGroup]:
    """Expand declared groups + ungrouped services into the full group set.

    ``specs`` is the MERGED service set (compose union rc.yml), keyed by
    service name; each value only needs the attributes read below, so this
    stays free of a provider import.

    Ordering is the byte-identical guard: implicit groups are named after their
    service, so with no ``task_groups`` block the result iterates in exactly
    the ``sorted(ctx.services)`` order ``services.tf.j2`` has always used.
    """
    owner = _members_of(groups)
    resolved: dict[str, ResolvedTaskGroup] = {}

    for gname in sorted(groups):
        group = groups[gname]
        members = [m for m in group.services if m in specs]
        resolved[gname] = ResolvedTaskGroup(
            name=gname,
            members=members,
            memory=(
                group.memory
                if group.memory is not None
                else sum(int(getattr(specs[m], "memory", 0) or 0) for m in members)
            ),
            ingress=group.ingress or _sole_public_member(members, specs),
            is_implicit=False,
            retired_hostnames=[m for m in members if m != gname],
        )

    for name in sorted(specs):
        if name in owner:
            continue
        resolved[name] = ResolvedTaskGroup(
            name=name,
            members=[name],
            memory=int(getattr(specs[name], "memory", 0) or 0),
            ingress=name if getattr(specs[name], "public", False) else None,
            is_implicit=True,
            retired_hostnames=[],
        )

    return {k: resolved[k] for k in sorted(resolved)}


def _sole_public_member(members: list[str], specs: dict[str, Any]) -> Optional[str]:
    """The one public member, or None when there is no unambiguous answer.

    Ambiguity is not resolved here — :func:`validate_task_groups` rejects it
    with a message naming the candidates, which is more useful than an
    arbitrary pick.
    """
    public = [m for m in members if getattr(specs[m], "public", False)]
    return public[0] if len(public) == 1 else None


def _ports_of(spec: Any) -> list[int]:
    """Every containerPort a service claims.

    ``extra_ports`` counts: awsvpc forces ``hostPort == containerPort``, so two
    containers in one task cannot share a port whether it is the primary or not.
    """
    ports: list[int] = []
    primary = getattr(spec, "port", None)
    if primary:
        ports.append(int(primary))
    for extra in getattr(spec, "extra_ports", None) or []:
        ports.append(int(extra))
    return ports


def _volume_names_of(spec: Any) -> list[str]:
    out: list[str] = []
    for entry in getattr(spec, "volumes", None) or []:
        if isinstance(entry, dict) and entry.get("name"):
            out.append(str(entry["name"]))
    return out


def validate_task_groups(
    groups: dict[str, TaskGroupV2],
    specs: dict[str, Any],
) -> None:
    """Every semantic reject for grouping, against the MERGED service set.

    Called by the ECS provider at emit time rather than by ``parse()``, because
    a group may name a service that only docker-compose.yml declares.

    Also validates the implicit groups: a group of one is still a task, so the
    all-non-essential check applies to a lone service too.
    """
    known = set(specs)

    for gname in sorted(groups):
        group = groups[gname]
        for member in group.services:
            if member not in known:
                raise ConfigError(
                    f"task_groups.{gname} names service {member!r}, which is "
                    f"not in the deploy set. Either it is in neither rc.yml "
                    f"services: nor docker-compose.yml, or compose.include / "
                    f"compose.exclude filtered it out. "
                    f"Deploying: {sorted(known) or 'nothing'}"
                )
        # A group name that equals one of its OWN members is the recommended
        # form — it keeps that member's terraform address, ECS service name,
        # task-def family, Cloud Map record and ALB wiring, which turns a
        # brownfield regroup into an in-place update for the survivor. A group
        # name that matches some OTHER service is a collision: two things would
        # claim one ECS service name.
        if gname in known and gname not in set(group.services):
            raise ConfigError(
                f"task_groups.{gname} collides with service {gname!r}, which is "
                f"not one of its members. Either add it to the group (naming a "
                f"group after a member is the recommended form — it keeps that "
                f"member's ECS service name and DNS record) or rename the group."
            )

    for rgroup in resolve_task_groups(groups, specs).values():
        _validate_one_group(rgroup, specs)


def _validate_one_group(group: ResolvedTaskGroup, specs: dict[str, Any]) -> None:
    members = group.members
    if not members:
        return
    label = "service" if group.is_implicit else f"task_groups.{group.name}"

    # -- at least one essential container. AWS, verbatim: "All tasks must have
    #    at least one essential container." Applies to a group of one too.
    if not any(getattr(specs[m], "essential", True) for m in members):
        raise ConfigError(
            f"{label}: every container is essential: false, but a task must "
            f"have at least one essential container (members: {members})"
        )

    if group.is_implicit:
        # Nothing below can disagree with itself, and a lone service keeps
        # whatever ports and volumes it already had.
        return

    anchor = members[0]

    # -- task/service-level fields every member must agree on.
    for attr, yml_key in UNIFORM_MEMBER_FIELDS.items():
        base = getattr(specs[anchor], attr, None)
        for member in members[1:]:
            other = getattr(specs[member], attr, None)
            if other != base:
                raise ConfigError(
                    f"task_groups.{group.name}: {anchor!r} and {member!r} "
                    f"disagree on {yml_key} ({base!r} vs {other!r}). One task "
                    f"has one {yml_key}, so every member of a group must "
                    f"declare the same value — or the group has to be split."
                )

    # -- security_groups is a list; compare as an ordered-insensitive set so a
    #    reordering isn't reported as a conflict.
    base_sgs = sorted(getattr(specs[anchor], "security_groups", None) or [])
    for member in members[1:]:
        other_sgs = sorted(getattr(specs[member], "security_groups", None) or [])
        if other_sgs != base_sgs:
            raise ConfigError(
                f"task_groups.{group.name}: {anchor!r} and {member!r} disagree "
                f"on security_groups ({base_sgs or 'default'} vs "
                f"{other_sgs or 'default'}). A task has ONE ENI, so it carries "
                f"one set of security groups."
            )

    # -- ports are unique within the task (awsvpc: hostPort == containerPort).
    claimed_ports: dict[int, str] = {}
    for member in members:
        for port in _ports_of(specs[member]):
            first = claimed_ports.get(port)
            if first is not None:
                raise ConfigError(
                    f"task_groups.{group.name}: {first!r} and {member!r} both "
                    f"claim port {port}. Containers in one awsvpc task share an "
                    f"ENI, so hostPort == containerPort and ports must be "
                    f"unique within the task."
                )
            claimed_ports[port] = member

    # -- one volume NAME per task. rc mints an access point per SERVICE per
    #    volume (`<service>__<volume>`), and the task-level `volume` block
    #    carries the access point id — so two members mounting the same name
    #    would need two `volume` blocks with one name, which ECS rejects.
    claimed_volumes: dict[str, str] = {}
    for member in members:
        for vol in _volume_names_of(specs[member]):
            first = claimed_volumes.get(vol)
            if first is not None:
                raise ConfigError(
                    f"task_groups.{group.name}: {first!r} and {member!r} both "
                    f"mount volume {vol!r}. rc gives each service its own EFS "
                    f"access point per volume, so one task cannot carry both "
                    f"under a single volume name. Rename one, or split the group."
                )
            claimed_volumes[vol] = member

    # -- exactly one ALB ingress container.
    public = [m for m in members if getattr(specs[m], "public", False)]
    if len(public) > 1 and group.ingress is None:
        raise ConfigError(
            f"task_groups.{group.name}: {public} are all public, so rc cannot "
            f"tell which container the ALB target group should point at. Set "
            f"task_groups.{group.name}.ingress to one of them."
        )
    if group.ingress is not None and not getattr(specs[group.ingress], "port", None):
        raise ConfigError(
            f"task_groups.{group.name}: ingress {group.ingress!r} declares no "
            f"port, so there is nothing for the ALB target group to forward to."
        )
    # A group gets ONE load_balancer block, pointed at the ingress container.
    # A domain on any other member would be read from nowhere: no target group,
    # no listener rule, no cert SAN, no R53 record -- and parse()'s
    # duplicate-hostname check does not catch it either, because the hostname is
    # unique. It would just stop resolving, which is exactly the silent
    # half-broken outcome this whole feature is trying not to produce.
    stranded = [
        m for m in members if m != group.ingress and getattr(specs[m], "domain", None)
    ]
    if stranded:
        for m in stranded:
            domain = getattr(specs[m], "domain", None)
            raise ConfigError(
                f"task_groups.{group.name}: {m!r} declares domain {domain!r} but "
                f"is not this group's ingress "
                f"({group.ingress!r}). A task gets ONE load balancer target, so "
                f"that hostname would be routed nowhere. Move {m!r} out of the "
                f"group, make it the ingress, or drop its domain and route the "
                f"hostname at {group.ingress!r} instead."
            )


def group_for_service(rc_yml_raw: Any, service: str) -> str:
    """The ECS service name that runs ``service``, from a RAW rc.yml mapping.

    The dict-level twin of the provider's ``_ecs_service_name`` indirection,
    for CLI paths that talk to AWS without ever building a DeployContext
    (``rc db``). Returns ``service`` unchanged when it is ungrouped, when it is
    a group's own name, or when rc.yml declares no groups at all.

    Tolerant of a malformed block on purpose: this runs on the way to a psql
    prompt, and a confusing traceback there is worse than falling back to the
    name the user typed.
    """
    if not isinstance(rc_yml_raw, dict):
        return service
    groups = rc_yml_raw.get("task_groups")
    if not isinstance(groups, dict):
        return service
    for gname, body in sorted(groups.items()):
        members = (body or {}).get("services") if isinstance(body, dict) else None
        if isinstance(members, list) and service in members:
            return str(gname)
    return service


def container_named(containers: Any, name: str) -> Any:
    """The container definition called ``name``, else the first one.

    ``containerDefinitions[0]`` was a safe shorthand while every task held
    exactly one container. In a group it is whichever member sorts first, so a
    caller reading POSTGRES_* off it would silently get nginx's environment and
    fall back to 5432/postgres/postgres. The fallback is kept for a task whose
    containers rc did not name (an adopted/hand-written task definition).
    """
    if not isinstance(containers, list) or not containers:
        return {}
    for c in containers:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    return containers[0]
