"""Turn a validated ``iam_roles:`` block into terraform view models.

Same shape and same reasons as ``network_plan``: kept out of ``provider.py``
(already ~3.5k lines) and free of any AWS SDK or terraform invocation, so the
whole layer is unit-testable on plain dicts.

The one design commitment worth stating up front:

**The shared role is never replaced, only bypassed.** ``aws_iam_role.task``
is emitted unconditionally and stays the default for every service that does
not name an ``iam_role:``. It is the ``task_role_arn`` in every task
definition of every stack already deployed, so removing or renaming it would
force a task-def rewrite on stacks that never asked for this feature.
Declared roles are additional resources with distinct terraform addresses;
nothing about the shared role's emission changes.

Each declared role does, however, get a verbatim copy of the shared role's
ECS-Exec inline policy. ``enable_execute_command = true`` is set on every rc
service, and ``rc exec`` / ``rc db backup`` / ``rc db restore`` all go through
it — the SSM agent inside the container opens its channels using the *task*
role. A least-privilege role without those four ``ssmmessages:*`` actions
would silently break every one of those commands for that service, which is
not a trade-off anyone is asking for when they scope an S3 grant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ...config._iam_types import IamRoleV2
from .network_plan import tf_ident


@dataclass
class IamRoleView:
    """One declared task role and its attachments."""

    name: str
    tf_name: str
    description: Optional[str]
    managed_policies: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    # Pre-rendered inline policy document, or None when the role declares no
    # statements. Serialized in Python rather than via HCL ``jsonencode`` for
    # the same reason the shared role's is: an IAM Condition is a nested JSON
    # map that an HCL object literal cannot express faithfully.
    policy_json: Optional[str] = None

    @property
    def role_ref(self) -> str:
        return f"aws_iam_role.{self.tf_name}"

    @property
    def arn_ref(self) -> str:
        return f"{self.role_ref}.arn"

    @property
    def tag_key_width(self) -> int:
        """Quoted-key column width that keeps the ``tags`` block fmt-clean."""
        return max((len(f'"{k}"') for k in self.tags), default=0)


@dataclass
class IamPlan:
    """Everything iam.tf.j2 / outputs.tf.j2 need for declared roles."""

    roles: list[IamRoleView] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.roles

    @property
    def output_key_width(self) -> int:
        return max((len(f'"{r.name}"') for r in self.roles), default=0)

    def role_arn_ref(self, declared_name: str) -> str:
        for role in self.roles:
            if role.name == declared_name:
                return role.arn_ref
        raise KeyError(declared_name)


def build_iam_plan(roles: dict[str, IamRoleV2]) -> IamPlan:
    """Resolve a validated ``iam_roles:`` block into terraform view models.

    Sorted by declared name so the emitted HCL is a function of the config
    alone — dict insertion order must not be able to churn the output and
    invalidate the emitter's content-hash revision id.
    """
    views: list[IamRoleView] = []
    for name in sorted(roles):
        role = roles[name]
        doc = role.policy_document()
        views.append(
            IamRoleView(
                name=name,
                tf_name=f"rc_role_{tf_ident(name)}",
                description=role.description,
                managed_policies=list(role.managed_policies),
                tags=dict(role.tags),
                policy_json=json.dumps(doc, indent=2) if doc else None,
            )
        )
    return IamPlan(roles=views)
