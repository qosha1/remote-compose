"""rc.yml v2 ``iam_roles:`` — declared, nameable ECS task roles.

Motivation
----------
rc emits exactly one task role, ``aws_iam_role.task``, and every service's
task definition points at it. ``provider_config.ecs.iam`` bolts managed
policies and inline statements onto *that* role, so a grant written for one
service (say S3 write on the media bucket) is silently held by every other
service in the stack — the worker, the scheduler, the nginx proxy. That is
the opposite of least privilege, and it gets worse with every service added.

A declared role is an opt-in override: name a role here, point a service at
it with ``iam_role:``, and that service's task definition carries that role
instead of the shared one. Everything else is untouched.

Why a top-level block instead of a per-service ``services.<name>.iam:``
----------------------------------------------------------------------
Both shapes were on the table; this one wins for three reasons.

1. **Reuse.** Grants cluster by *tier*, not by service: web + worker +
   scheduler usually need the identical S3/SQS set. Inline blocks force that
   list to be copy-pasted per service, and copies drift — one service gets a
   new bucket, the others silently do not.
2. **Identity.** A role is a thing with a name, an ARN, and a lifetime. Two
   services sharing ``iam_role: media-writer`` provably share one IAM role;
   two identical inline blocks are ambiguous (one role or two?) and the
   answer would be an emitter implementation detail.
3. **Consistency.** The declared-network layer already established
   "declare it at the top level, reference it by name" for security groups
   and subnet groups. A second, differently-shaped mechanism for the same
   kind of thing is a cost paid by every reader of every rc.yml.

The statement vocabulary is deliberately identical to
``provider_config.ecs.iam`` (``managed_policies`` / ``statements`` with
``sid`` / ``actions`` / ``resources`` / ``condition``) so there is one thing
to learn. That block keeps working and keeps meaning "grants on the shared
role" — a service that declares ``iam_role:`` opts out of it entirely, which
is the whole point.

Like a declared security group with no rules, a declared role with no grants
is valid and means exactly what it says: this service gets nothing beyond
what ECS itself requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ._errors import ConfigError

# Declared IAM role names share the network layer's name grammar on purpose:
# both become terraform identifiers, AWS resource names, and `rc outputs
# --env` suffixes, and a reader should not have to remember two rules.
from ._network_types import _validate_name

# AWS caps managed policies at 20 per role (10 by default, raisable to 20).
# Hitting it surfaces as a LimitExceeded halfway through an apply, with some
# attachments already made.
MAX_MANAGED_POLICIES = 20

# IAM requires a Sid to be alphanumeric — no hyphens, no underscores, no
# spaces. A bad one is accepted by `terraform validate` and rejected by IAM
# at apply time, which is the worst place to find out.
_SID_RE = re.compile(r"^[A-Za-z0-9]+$")

# arn:<partition>:iam::<account-or-aws>:policy/<path><name>. Deliberately
# loose on the tail (paths, service-role/, aws-service-role/ all appear) and
# strict on the head, which is where the realistic typos are: a role ARN or
# a bare policy name pasted where a policy ARN belongs.
_POLICY_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::(aws|\d{12}):policy/.+$")


@dataclass
class IamStatementV2:
    """One Allow statement in a declared role's inline policy.

    Allow-only, matching ``provider_config.ecs.iam.statements``. A declared
    role starts with zero permissions, so its allow-list *is* the boundary —
    there is nothing broad enough for a Deny to usefully carve out. Reach for
    a managed policy plus a boundary elsewhere if you genuinely need one.
    """

    actions: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    sid: Optional[str] = None
    condition: Optional[dict[str, Any]] = None

    @classmethod
    def parse(cls, raw: Any, *, where: str) -> "IamStatementV2":
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{where}: each statement must be a mapping, got "
                f"{type(raw).__name__}"
            )
        unknown = set(raw) - {"sid", "actions", "resources", "condition"}
        if unknown:
            raise ConfigError(
                f"{where}: unknown statement key(s) {sorted(unknown)} "
                f"(supported: actions, condition, resources, sid)"
            )
        return cls(
            actions=_str_list(raw.get("actions"), where=f"{where}.actions"),
            resources=_str_list(raw.get("resources"), where=f"{where}.resources"),
            sid=raw.get("sid"),
            condition=raw.get("condition"),
        )

    def validate(self, *, where: str) -> None:
        if not self.actions:
            raise ConfigError(
                f"{where}: 'actions' is required and must be non-empty — a "
                f"statement granting no action does nothing"
            )
        if not self.resources:
            raise ConfigError(
                f"{where}: 'resources' is required and must be non-empty (use "
                f"['*'] deliberately if the action is account-wide)"
            )
        if self.sid is not None:
            if not isinstance(self.sid, str) or not _SID_RE.match(self.sid):
                raise ConfigError(
                    f"{where}: sid {self.sid!r} must be alphanumeric — IAM "
                    f"rejects hyphens, underscores and spaces in a Sid"
                )
        if self.condition is not None and not isinstance(self.condition, dict):
            raise ConfigError(
                f"{where}: condition must be a mapping of IAM condition "
                f"operators, got {type(self.condition).__name__}"
            )


@dataclass
class IamRoleV2:
    """A declared ECS task role, attachable to any number of services.

    Not owned by a service: like a declared security group it may be
    referenced by one service, by several, or by none at all — in which case
    it simply exists and its ARN is exported for an out-of-band consumer
    (a Lambda, a hand-run ``run_task``) to assume or pass.
    """

    name: str
    description: Optional[str] = None
    managed_policies: list[str] = field(default_factory=list)
    statements: list[IamStatementV2] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        _validate_name("iam_roles", self.name)
        where = f"iam_roles.{self.name}"
        if self.description is not None and not isinstance(self.description, str):
            raise ConfigError(f"{where}: description must be a string")
        if len(self.managed_policies) > MAX_MANAGED_POLICIES:
            raise ConfigError(
                f"{where}: {len(self.managed_policies)} managed_policies exceeds "
                f"the AWS limit of {MAX_MANAGED_POLICIES} per role"
            )
        seen: set[str] = set()
        for arn in self.managed_policies:
            if not _POLICY_ARN_RE.match(arn):
                raise ConfigError(
                    f"{where}: managed_policies entry {arn!r} is not an IAM "
                    f"policy ARN (expected "
                    f"'arn:aws:iam::aws:policy/...' or "
                    f"'arn:aws:iam::<account>:policy/...')"
                )
            if arn in seen:
                raise ConfigError(f"{where}: managed_policies lists {arn!r} twice")
            seen.add(arn)
        sids: set[str] = set()
        for i, stmt in enumerate(self.statements):
            stmt.validate(where=f"{where}.statements[{i}]")
            if stmt.sid is not None:
                if stmt.sid in sids:
                    raise ConfigError(
                        f"{where}.statements[{i}]: duplicate sid {stmt.sid!r} — "
                        f"IAM rejects a policy document with repeated Sids"
                    )
                sids.add(stmt.sid)
        for k, v in self.tags.items():
            if not isinstance(k, str) or isinstance(v, (dict, list)):
                raise ConfigError(
                    f"{where}: tags must be a flat mapping of string keys to "
                    f"scalar values (offending key {k!r})"
                )

    def policy_document(self) -> Optional[dict[str, Any]]:
        """The inline policy for this role, or None when it has no statements.

        Sids are generated positionally when omitted, mirroring
        ``provider_config.ecs.iam``'s ``AppGrant<i>``, but namespaced per role
        so two roles' documents stay independently readable in the console.
        """
        if not self.statements:
            return None
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": stmt.sid or f"Grant{i}",
                    "Effect": "Allow",
                    "Action": list(stmt.actions),
                    "Resource": list(stmt.resources),
                    **({"Condition": stmt.condition} if stmt.condition else {}),
                }
                for i, stmt in enumerate(self.statements)
            ],
        }


def _str_list(raw: Any, *, where: str) -> list[str]:
    """Coerce a scalar-or-list field to a list of strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        raise ConfigError(
            f"{where} must be a string or a list of strings, got "
            f"{type(raw).__name__}"
        )
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{where}: {item!r} must be a non-empty string")
    return [str(item) for item in raw]


def validate_iam_role_refs(
    roles: dict[str, IamRoleV2],
    *,
    service_roles: dict[str, Optional[str]],
) -> None:
    """Resolve every ``services.<name>.iam_role`` against the declared set.

    Split out from ``IamRoleV2.validate`` for the same reason
    ``validate_network_refs`` is: a reference can only be checked once every
    declared name is known. Unlike the network block there is no second pass —
    a service's ``iam_role`` is written in rc.yml, so compose adds nothing.

    An unreferenced role is deliberately NOT an error: it costs nothing, and
    exporting its ARN for an out-of-band consumer is a legitimate reason to
    declare one. (Contrast an orphan interface VPC endpoint, which is a paid
    ENI per AZ serving no traffic, and which rc does refuse.)
    """
    known = set(roles)
    for svc_name, role_name in sorted(service_roles.items()):
        if role_name is None:
            continue
        if role_name not in known:
            raise ConfigError(
                f"service {svc_name!r}: iam_role {role_name!r} does not name a "
                f"declared iam_roles entry (known: {sorted(known) or 'none'})"
            )
