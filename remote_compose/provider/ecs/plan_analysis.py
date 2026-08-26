"""Structured reads of ``terraform show -json <planfile>`` output.

Pure functions over the parsed plan document — no I/O, no AWS, no terraform
invocation — so the interesting cases are unit-testable from a fixture
instead of from a live stack.

Why JSON and not stdout: the human plan output ("-/+ must be replaced",
"# forces replacement") is a rendering, not an interface. It changes between
terraform versions and wraps at terminal width. ``resource_changes[]`` is a
documented, versioned structure (``format_version``), so detection built on
it doesn't rot.

Beads:
  rc-avcr — ignore_task_definition_changes fails open on a FORCED task
            definition replacement, silently dropping out-of-band secrets.
  rc-5a4g — binpack placement is rejected by ECS while the service's live
            availability_zone_rebalancing is ENABLED.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

TASK_DEFINITION_TYPE = "aws_ecs_task_definition"
SERVICE_TYPE = "aws_ecs_service"

# A replacement is delete+create in either order — plain replacement is
# ["delete", "create"]; create_before_destroy inverts it. Anything else
# (["update"], ["create"], ["no-op"], ["delete"]) is not a replacement.
_REPLACE_ACTIONS = ({"delete", "create"},)


@dataclass
class TaskDefReplacement:
    """One ``aws_ecs_task_definition`` terraform intends to replace."""

    address: str
    family: str
    # Names of the secrets/env vars present on the LIVE revision that the
    # rendered replacement does not carry. This is the damage: the live
    # values were wired on out-of-band, and the new revision is born without
    # them.
    dropped_secrets: list[str] = field(default_factory=list)
    dropped_env: list[str] = field(default_factory=list)
    # Attribute paths terraform names as forcing the replacement, e.g.
    # ["requires_compatibilities"] for a FARGATE -> EC2 launch type change.
    forced_by: list[str] = field(default_factory=list)
    # True when the rendered side of the diff isn't fully known at plan time
    # (container_definitions computed from a not-yet-created resource). The
    # dropped_* lists are then a floor, not an exact count.
    after_unknown: bool = False

    @property
    def is_lossy(self) -> bool:
        return bool(self.dropped_secrets or self.dropped_env or self.after_unknown)


def _container_definitions(attrs: Any) -> list[dict]:
    """Parse a resource's ``container_definitions`` into a list of dicts.

    Terraform carries this attribute as a JSON *string*. Anything
    unparseable (null, already-a-list, malformed) yields [] rather than
    raising: a detector that crashes on an unexpected plan shape is worse
    than one that reports nothing.
    """
    if not isinstance(attrs, dict):
        return []
    raw = attrs.get("container_definitions")
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [c for c in parsed if isinstance(c, dict)]


def _named_keys(containers: list[dict], key: str) -> set[str]:
    """Collect ``name`` values from every container's ``key`` list.

    Qualified by container name, because two containers in one task
    definition legitimately carry the same env var name and losing it from
    one but not the other still matters.
    """
    out: set[str] = set()
    for c in containers:
        cname = str(c.get("name") or "?")
        entries = c.get(key)
        if not isinstance(entries, list):
            continue
        for e in entries:
            if isinstance(e, dict) and e.get("name"):
                out.add(f"{cname}.{e['name']}")
    return out


def _is_replacement(actions: Any) -> bool:
    if not isinstance(actions, list):
        return False
    return set(actions) in _REPLACE_ACTIONS


def _forced_by(change: dict) -> list[str]:
    """Flatten terraform's ``replace_paths`` into dotted attribute names."""
    paths = change.get("replace_paths")
    if not isinstance(paths, list):
        return []
    out: list[str] = []
    for path in paths:
        if isinstance(path, list):
            out.append(".".join(str(seg) for seg in path))
        elif path is not None:
            out.append(str(path))
    return sorted(set(out))


def detect_task_definition_replacements(
    plan_json: dict,
) -> list[TaskDefReplacement]:
    """Find every task definition the plan replaces, and what it would drop.

    Only REPLACEMENTS are reported. An in-place update of a task definition
    is exactly what ``lifecycle { ignore_changes = [container_definitions] }``
    already suppresses; a replacement is the case it cannot suppress, because
    ignore_changes governs diff-driven updates and a ForceNew attribute
    change is not one.

    Returns entries in plan order, including non-lossy ones — a caller that
    only wants the dangerous subset filters on ``is_lossy``, and a caller
    reporting "N task definitions will be replaced" needs the full count.
    """
    changes = plan_json.get("resource_changes")
    if not isinstance(changes, list):
        return []

    out: list[TaskDefReplacement] = []
    for rc in changes:
        if not isinstance(rc, dict) or rc.get("type") != TASK_DEFINITION_TYPE:
            continue
        change = rc.get("change")
        if not isinstance(change, dict) or not _is_replacement(change.get("actions")):
            continue

        before = change.get("before")
        after = change.get("after")
        before_containers = _container_definitions(before)
        after_containers = _container_definitions(after)

        after_unknown = bool(
            isinstance(change.get("after_unknown"), dict)
            and change["after_unknown"].get("container_definitions")
        )

        before_secrets = _named_keys(before_containers, "secrets")
        after_secrets = _named_keys(after_containers, "secrets")
        before_env = _named_keys(before_containers, "environment")
        after_env = _named_keys(after_containers, "environment")

        family = ""
        if isinstance(before, dict):
            family = str(before.get("family") or "")
        if not family and isinstance(after, dict):
            family = str(after.get("family") or "")

        out.append(
            TaskDefReplacement(
                address=str(rc.get("address") or ""),
                family=family or str(rc.get("name") or ""),
                dropped_secrets=sorted(before_secrets - after_secrets),
                dropped_env=sorted(before_env - after_env),
                forced_by=_forced_by(change),
                after_unknown=after_unknown,
            )
        )
    return out


def render_replacement_warning(
    replacements: list[TaskDefReplacement],
) -> str:
    """Prose warning for a plan that replaces ignore-managed task definitions.

    Self-contained by the compose_warnings.py convention: names the services,
    the count of values being dropped, the attribute that forced the
    replacement, and what to do — the reader should not have to look anything
    up to act on it.

    Returns "" when nothing lossy was found, so callers can warn
    unconditionally on the result.
    """
    lossy = [r for r in replacements if r.is_lossy]
    if not lossy:
        return ""

    lines = [
        "provider_config.ecs.ignore_task_definition_changes is on, which means "
        "terraform is NOT the source of truth for these task definitions' "
        "container definitions — but this plan REPLACES them, and "
        "ignore_changes cannot suppress a replacement. lifecycle "
        "ignore_changes suppresses diff-driven updates; a ForceNew attribute "
        "change is not one. The replacement revisions are rendered from "
        "rc.yml alone, so every value wired on out-of-band (a "
        "reconcile_task_secrets.py-style script, a manual "
        "register-task-definition) is dropped the moment the service is "
        "pointed at them:",
        "",
    ]
    for r in lossy:
        forced = (
            f" (forced by {', '.join(r.forced_by)})"
            if r.forced_by
            else " (forced by a ForceNew attribute change)"
        )
        counts = []
        if r.dropped_secrets:
            counts.append(f"{len(r.dropped_secrets)} secret(s)")
        if r.dropped_env:
            counts.append(f"{len(r.dropped_env)} env var(s)")
        summary = " and ".join(counts) if counts else "out-of-band values"
        lines.append(f"    {r.family or r.address}{forced}")
        lines.append(f"      drops {summary} present on the live revision")
        if r.dropped_secrets:
            shown = r.dropped_secrets[:8]
            more = len(r.dropped_secrets) - len(shown)
            tail = f", +{more} more" if more > 0 else ""
            lines.append(f"      secrets: {', '.join(shown)}{tail}")
        if r.after_unknown:
            lines.append(
                "      (the rendered side is not fully known at plan time — "
                "treat these counts as a floor)"
            )
    lines += [
        "",
        "    Services pointed at a secretless revision crashloop until the "
        "out-of-band reconcile runs again. In CI that window is short because "
        "build and reconcile are adjacent steps; running this apply by hand "
        "without immediately re-running the reconcile leaves the service "
        "down. Re-run your task-definition reconcile immediately after this "
        "apply, or split the change so the forcing attribute lands on its "
        "own.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rc-5a4g: binpack vs. Availability Zone rebalancing
# ---------------------------------------------------------------------------


ECS_SERVICE_TYPE = "aws_ecs_service"


@dataclass
class TaskGroupRegroup:
    """A plan that MERGES existing ECS services into task groups (rc-93ol)."""

    #: Services terraform will destroy — their containers reappear inside a
    #: survivor's task, but the service (and its Cloud Map record) goes away.
    destroyed: list[str] = field(default_factory=list)
    #: Services that remain afterwards, whether created fresh or updated in
    #: place. These are the groups.
    surviving: list[str] = field(default_factory=list)
    #: The subset of ``surviving`` terraform creates from scratch.
    created: list[str] = field(default_factory=list)
    #: The subset of ``surviving`` terraform updates in place — the naming
    #: lever paid off for these: same terraform address, same ECS service
    #: name, same task-def family, same Cloud Map record, same ALB wiring.
    updated: list[str] = field(default_factory=list)


def _service_name(rc: dict) -> str:
    name = rc.get("name")
    if isinstance(name, str) and name:
        return name
    address = str(rc.get("address") or "")
    return address.rsplit(".", 1)[-1] if address else ""


def detect_task_group_regroup(plan_json: dict) -> "TaskGroupRegroup | None":
    """Spot a plan that collapses several ECS services into task groups.

    Regrouping a live estate is destructive in terraform: the merged members'
    ``aws_ecs_service`` resources are DESTROYED and their containers come back
    inside a survivor's task. On a real estate that means each tenant's
    postgres stops and restarts — which must not read like a routine deploy.

    The signature is >= 2 ``aws_ecs_service`` deletions alongside at least one
    ``aws_ecs_service`` that survives. Both halves matter:

      * a single deletion beside untouched services is a service REMOVAL, not
        a merge, and calling it a regroup would be a false alarm;
      * deletions with NO survivor is a teardown (``rc destroy``), where
        telling the operator to expect a merge would be actively wrong.

    Returns None when the plan is anything else, so callers can render
    unconditionally on the result.
    """
    changes = plan_json.get("resource_changes") if isinstance(plan_json, dict) else None
    if not isinstance(changes, list):
        return None

    destroyed: list[str] = []
    created: list[str] = []
    updated: list[str] = []
    for rc in changes:
        if not isinstance(rc, dict) or rc.get("type") != ECS_SERVICE_TYPE:
            continue
        change = rc.get("change")
        if not isinstance(change, dict):
            continue
        actions = change.get("actions")
        if not isinstance(actions, list):
            continue
        name = _service_name(rc)
        if not name:
            continue
        if "delete" in actions and "create" not in actions:
            destroyed.append(name)
        elif "create" in actions and "delete" in actions:
            # replaced: the old service is torn down either way
            destroyed.append(name)
        elif "create" in actions:
            created.append(name)
        elif "update" in actions:
            updated.append(name)

    surviving = sorted(set(created) | set(updated))
    if len(destroyed) < 2 or not surviving:
        return None
    return TaskGroupRegroup(
        destroyed=sorted(set(destroyed)),
        surviving=surviving,
        created=sorted(set(created)),
        updated=sorted(set(updated)),
    )


def render_regroup_warning(regroup: "TaskGroupRegroup | None") -> str:
    """Prose for a regroup, in the self-contained compose_warnings style.

    Deliberately says what has NOT been verified. No grouped stack has been
    applied against real AWS yet (the same caveat rc-ero carries for the
    declared network), and a runbook that hides that is worse than none.
    """
    if regroup is None:
        return ""

    lines = [
        "WARN: this is NOT a routine deploy — it MERGES ECS services into task "
        "groups.",
        "",
        f"  Destroyed ({len(regroup.destroyed)}): " f"{', '.join(regroup.destroyed)}",
        "    These services stop. Their containers come back inside a "
        "survivor's task,",
        "    but each one's own Cloud Map A record goes away with the service "
        "-- anything",
        "    still resolving those names breaks. `rc plan` lists them " "separately.",
        "",
        f"  Surviving ({len(regroup.surviving)}): " f"{', '.join(regroup.surviving)}",
    ]
    if regroup.updated:
        lines.append(
            f"    {', '.join(regroup.updated)} update IN PLACE -- same ECS "
            "service, same task-def"
        )
        lines.append(
            "    family, same DNS record, same ALB target group. That is the "
            "naming lever:"
        )
        lines.append(
            "    a group named after one of its members keeps that member's "
            "identity."
        )
    if regroup.created:
        lines.append(
            f"    {', '.join(regroup.created)} are created fresh, so every "
            "merged member's"
        )
        lines.append(
            "    name retires. Naming the group after a member would have kept " "one."
        )
    lines += [
        "",
        "  BEFORE YOU APPLY:",
        "    1. Take a database BACKUP. A destroyed service's task stops; for a "
        "stateful",
        "       one that is a real stop-then-start against live data.",
        "    2. Check EFS. Volumes survive (the file system and access points "
        "are separate",
        "       resources), but the new task runs under the GROUP's task role "
        "-- confirm it",
        "       still carries the elasticfilesystem grants the old per-service "
        "role had.",
        "       UNVERIFIED against live AWS: no grouped stack has been applied " "yet.",
        "    3. Expect a gap. Each destroyed service is down from its stop "
        "until the",
        "       survivor's new task passes its health check -- not a "
        "zero-downtime roll.",
    ]
    return "\n".join(lines)


@dataclass
class BinpackRebalancingConflict:
    """A service the plan gives binpack while leaving AZ rebalancing on."""

    address: str
    service: str

    @property
    def label(self) -> str:
        return self.service or self.address


def _has_binpack(attrs: Any) -> bool:
    if not isinstance(attrs, dict):
        return False
    strategies = attrs.get("ordered_placement_strategy")
    if not isinstance(strategies, list):
        return False
    return any(
        isinstance(s, dict) and str(s.get("type", "")).lower() == "binpack"
        for s in strategies
    )


def detect_binpack_az_rebalancing_conflicts(
    plan_json: dict,
) -> list[BinpackRebalancingConflict]:
    """Services this plan would leave in a self-contradicting state.

    ECS refuses a service that combines a binpack placement strategy with
    ``availabilityZoneRebalancing = ENABLED``, and the default is
    history-dependent rather than config-dependent: CreateService with no
    value yields ENABLED, while UpdateService with no value KEEPS whatever
    the live service already has. So a service first created on Fargate
    carries ENABLED, and an apply that adds binpack without also setting
    DISABLED produces a 400 at UpdateService -- reachable only against live
    service state, which is why plan and preflight both pass clean.

    rc renders DISABLED alongside every binpack it emits, so this should
    never fire on rc-rendered terraform. It exists to catch the case coming
    back: drift, an adopted service, or a future template change that emits
    binpack without owning the field again.
    """
    changes = plan_json.get("resource_changes")
    if not isinstance(changes, list):
        return []

    out: list[BinpackRebalancingConflict] = []
    for rc in changes:
        if not isinstance(rc, dict) or rc.get("type") != SERVICE_TYPE:
            continue
        change = rc.get("change")
        if not isinstance(change, dict):
            continue
        actions = change.get("actions")
        if not isinstance(actions, list) or set(actions) <= {"no-op", "read"}:
            continue
        after = change.get("after")
        before = change.get("before")
        if not _has_binpack(after):
            continue
        planned = (after or {}).get("availability_zone_rebalancing")
        if str(planned or "").upper() == "DISABLED":
            continue  # the plan fixes it
        live = (before or {}).get("availability_zone_rebalancing")
        # Unset on a CREATE is also a conflict: ECS defaults new services to
        # ENABLED, so binpack + unspecified is rejected there too.
        creating = "create" in actions and "delete" not in actions
        if str(live or "").upper() == "ENABLED" or (creating and not planned):
            out.append(
                BinpackRebalancingConflict(
                    address=str(rc.get("address") or ""),
                    service=str((after or {}).get("name") or rc.get("name") or ""),
                )
            )
    return out


def render_binpack_conflict_warning(
    conflicts: list[BinpackRebalancingConflict],
) -> str:
    """Prose warning for services that would be rejected at UpdateService."""
    if not conflicts:
        return ""
    names = ", ".join(c.label for c in conflicts)
    return (
        f"ECS will REJECT this apply for {names}: the plan gives these "
        f"service(s) a binpack placement strategy while their Availability "
        f"Zone rebalancing stays ENABLED, and ECS does not accept that "
        f"combination (UpdateService returns 400).\n"
        f"    This is invisible until apply because it depends on LIVE "
        f"service state, not on config: ECS defaults a newly created service "
        f"to ENABLED, but on update it keeps whatever the service already "
        f"has. A service first created on FARGATE therefore carries ENABLED "
        f"into its move to EC2, while one that was already DISABLED migrates "
        f"cleanly -- same config, opposite outcomes.\n"
        f'    rc normally renders availability_zone_rebalancing = "DISABLED" '
        f"next to every binpack strategy it emits, so seeing this means the "
        f"service's placement strategy is coming from somewhere rc does not "
        f"own (an adopted service, or drift). Set it to DISABLED on those "
        f"services before applying."
    )
