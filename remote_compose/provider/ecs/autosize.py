"""Heuristic auto-sizing for the ECS EC2 capacity ASG.

When the user omits ``provider_config.ecs.ec2_capacity.instance_type``, we
pick the smallest t3-family instance large enough to run the biggest single
EC2 task, then size the ASG (min/max/desired) to cover the sum of all EC2
task resource requests across three independent dimensions: total vCPU,
total memory, and total awsvpc task ENIs (rc-e5u.25.1 — see
``ENI_RESERVED_FOR_PRIMARY`` below). Whichever dimension needs the most
instances wins.

Conservative and explicit: this is enough for "get a workload running"
capacity planning. Production users with tight cost targets should set
``instance_type`` explicitly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable


@dataclass
class InstanceShape:
    name: str
    vcpu: int  # 1 vCPU = 1024 ECS CPU units
    # int for every shape except t3/t3a/t4g .nano (0.5 GiB nominal) in
    # KNOWN_INSTANCE_SHAPES below -- float only to accommodate that one tier.
    memory_gib: "int | float"
    # AWS's per-instance-type ceiling on attached elastic network interfaces
    # (EC2 API: ``NetworkInfo.MaximumNetworkInterfaces``; console: EC2 >
    # Instance Types > "Maximum number of network interfaces"). One of these
    # is always the instance's own primary ENI (see
    # ``ENI_RESERVED_FOR_PRIMARY``), so this is NOT the number of awsvpc
    # tasks the instance can host -- that's ``max_enis -
    # ENI_RESERVED_FOR_PRIMARY``, computed in ``auto_size()``.
    #
    # None means "not modeled": auto_size() skips the ENI dimension for that
    # shape entirely and falls back to cpu/memory-only sizing, same as
    # before this constraint existed. That's the default for a
    # caller-supplied custom ``ladder=`` (see ``TestCustomLadder``) -- rc
    # only carries verified numbers for its own T3_LADDER.
    max_enis: "int | None" = None
    # Tasks this instance type can host when the account's ``awsvpcTrunking``
    # setting is ENABLED (rc-hguq). Trunking attaches one trunk ENI and gives
    # each task a *branch* interface, so the ceiling stops being
    # ``max_enis - 1`` and becomes a much larger published per-type number --
    # m5.xlarge goes from 3 tasks to 20.
    #
    # Within rc's own tables (T3_LADDER / KNOWN_INSTANCE_SHAPES) None means
    # VERIFIED INELIGIBLE, not unknown: every entry was checked against AWS's
    # eni-trunking-supported-instance-types tables, where the whole T family
    # is absent and m5.metal / c5.metal are named in the explicit
    # not-supported list. A shape rc has no row for is never consulted at
    # all, so the two states never collide in practice.
    trunked_task_limit: "int | None" = None
    # Set by ``with_trunking()`` to pin the usable task-ENI slot count
    # directly. Exists so trunking does not have to be threaded as a boolean
    # through auto_size() / measure_fleet() / check_fixed_shape_capacity()
    # and every one of their call sites and tests -- and so that no field
    # ever holds a number that misdescribes itself (``max_enis`` means the
    # EC2 API's MaximumNetworkInterfaces and nothing else, trunked or not).
    task_slots_override: "int | None" = None

    @property
    def trunking_supported(self) -> bool:
        """Whether AWS lists this type as ENI-trunking-eligible."""
        return self.trunked_task_limit is not None

    @property
    def task_eni_slots(self) -> "int | None":
        """awsvpc tasks this shape can host, or None when not modeled.

        Without trunking that is ``max_enis - ENI_RESERVED_FOR_PRIMARY``.
        With it (see ``with_trunking``) it is AWS's published trunked limit.
        """
        if self.task_slots_override is not None:
            return self.task_slots_override
        if self.max_enis is None:
            return None
        return self.max_enis - ENI_RESERVED_FOR_PRIMARY

    def without_task_enis(self) -> "InstanceShape":
        """This shape with the task-ENI dimension switched OFF entirely.

        For ``network_mode: bridge`` (rc-u122), where tasks share the
        container instance's ENI and no branch interface is allocated per
        task. ``max_enis=None`` is the existing "not modeled" signal, so
        ``task_eni_slots`` returns None and ``auto_size`` skips the ENI
        dimension -- no new concept, and the memory/CPU dimensions are
        untouched. ``trunked_task_limit`` goes with it so a later
        ``with_trunking()`` cannot resurrect a ceiling that does not apply.
        """
        return replace(
            self, max_enis=None, trunked_task_limit=None, task_slots_override=None
        )

    def with_trunking(self) -> "InstanceShape":
        """This shape as it behaves with ``awsvpcTrunking`` enabled.

        Returns self unchanged for an ineligible type, so callers can apply
        it unconditionally -- which is what makes the whole feature a no-op
        for the default t3 ladder without a single conditional at the call
        sites.
        """
        if self.trunked_task_limit is None:
            return self
        return replace(self, task_slots_override=self.trunked_task_limit)


# Conservative t3 ladder. Add m5/m6i family via explicit instance_type.
#
# max_enis verified live against the AWS API (2026-08-13):
#   aws ec2 describe-instance-types --filters Name=instance-type,Values=t3.* \
#     --query 'InstanceTypes[*].[InstanceType,NetworkInfo.MaximumNetworkInterfaces]'
#   -> t3.small=3, t3.medium=3, t3.large=3, t3.xlarge=4, t3.2xlarge=4
# Cross-checked two ways: (1) AWS ECS's own published "task limit without
# ENI trunking" table (eni-trunking-supported-instance-types.html) equals
# max_enis - 1 for every instance type it lists that we spot-checked
# (m5.large 3->2, c5.large 3->2, a1.medium 2->1, c5.xlarge 4->3); (2) that
# same page's exhaustive supported-instance-type tables (general purpose,
# compute optimized, memory optimized, storage optimized, accelerated
# computing, HPC) never list a single t3.* entry -- the T family is not
# ENI-trunking-eligible at all, at any size, which is why rc-e5u.25.1 chose
# "document + size around the ceiling" over "opt into trunking": trunking
# would be a no-op for rc's default ladder no matter how it's wired up.
T3_LADDER: list[InstanceShape] = [
    InstanceShape("t3.small", vcpu=2, memory_gib=2, max_enis=3),
    InstanceShape("t3.medium", vcpu=2, memory_gib=4, max_enis=3),
    InstanceShape("t3.large", vcpu=2, memory_gib=8, max_enis=3),
    InstanceShape("t3.xlarge", vcpu=4, memory_gib=16, max_enis=4),
    InstanceShape("t3.2xlarge", vcpu=8, memory_gib=32, max_enis=4),
]


# Lookup-by-exact-name table for ec2_capacity.instance_type set EXPLICITLY
# (bypasses auto_size() and T3_LADDER entirely -- see check_fixed_shape_capacity
# below). Covers the burstable (t3/t3a/t4g) and general-purpose/compute-optimized
# (m5/m6i/c5/c6i) families, verified live against the AWS API (2026-08-18,
# us-east-2 -- vCPU/memory/max-ENI are properties of the instance type
# definition, not region-specific, same basis T3_LADDER's header comment
# relies on). Deliberately NOT derived from a per-family/per-size pattern:
# t3a.small is the concrete reason why. Every t3.* and t4g.* "small" tier has
# max_enis=3 (2 usable), but t3a.small is max_enis=2 (1 usable) -- the ONLY
# size in these four families where the AMD (t3a) variant's ENI ceiling
# doesn't match its Intel (t3) / Graviton (t4g) same-size siblings. A
# table generated by deriving t3a's numbers from t3's would silently get
# this one entry wrong. Every entry below is its own literal, independently
# verified row for exactly this reason -- do not "simplify" this into a
# smaller per-family/per-size formula.
#
# trunked_task_limit is AWS's published "Task limit with ENI trunking" for
# each type, transcribed from
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/eni-trunking-supported-instance-types.html
# (read 2026-08-19, rc-hguq). Two independent checks on that transcription:
#
#   1. The same tables carry a "Task limit WITHOUT ENI trunking" column, and
#      it equals max_enis - ENI_RESERVED_FOR_PRIMARY for every single row rc
#      models -- m5.large 2, m5.xlarge 3, m5.4xlarge 7, m5.16xlarge 14,
#      c5.large 2, c6i.32xlarge 15 and the rest. That is an end-to-end
#      confirmation of the max_enis column above, from a different AWS source
#      than the describe-instance-types call it was built from.
#   2. Absence is meaningful, not an oversight: no t3.*, t3a.* or t4g.* row
#      appears anywhere in those tables, which is the positive evidence
#      behind T3_LADDER's "the T family is not ENI-trunking-eligible at any
#      size" claim. m5.metal and c5.metal are likewise named in the page's
#      explicit not-supported list despite their families being eligible --
#      so they keep trunked_task_limit=None while their siblings do not.
#      Do NOT infer a metal tier's limit from its family.
#
# Unlisted instance types (any type not a key here) are simply not modeled --
# check_fixed_shape_capacity() skips validation for them, same "not modeled"
# precedent as InstanceShape.max_enis=None for a caller-supplied ladder.
KNOWN_INSTANCE_SHAPES: dict[str, InstanceShape] = {
    s.name: s
    for s in [
        InstanceShape("t3.nano", vcpu=2, memory_gib=0.5, max_enis=2),
        InstanceShape("t3.micro", vcpu=2, memory_gib=1, max_enis=2),
        InstanceShape("t3.small", vcpu=2, memory_gib=2, max_enis=3),
        InstanceShape("t3.medium", vcpu=2, memory_gib=4, max_enis=3),
        InstanceShape("t3.large", vcpu=2, memory_gib=8, max_enis=3),
        InstanceShape("t3.xlarge", vcpu=4, memory_gib=16, max_enis=4),
        InstanceShape("t3.2xlarge", vcpu=8, memory_gib=32, max_enis=4),
        InstanceShape("t3a.nano", vcpu=2, memory_gib=0.5, max_enis=2),
        InstanceShape("t3a.micro", vcpu=2, memory_gib=1, max_enis=2),
        # t3a.small: max_enis=2 (1 usable), NOT 3 like t3.small/t4g.small.
        InstanceShape("t3a.small", vcpu=2, memory_gib=2, max_enis=2),
        InstanceShape("t3a.medium", vcpu=2, memory_gib=4, max_enis=3),
        InstanceShape("t3a.large", vcpu=2, memory_gib=8, max_enis=3),
        InstanceShape("t3a.xlarge", vcpu=4, memory_gib=16, max_enis=4),
        InstanceShape("t3a.2xlarge", vcpu=8, memory_gib=32, max_enis=4),
        InstanceShape("t4g.nano", vcpu=2, memory_gib=0.5, max_enis=2),
        InstanceShape("t4g.micro", vcpu=2, memory_gib=1, max_enis=2),
        InstanceShape("t4g.small", vcpu=2, memory_gib=2, max_enis=3),
        InstanceShape("t4g.medium", vcpu=2, memory_gib=4, max_enis=3),
        InstanceShape("t4g.large", vcpu=2, memory_gib=8, max_enis=3),
        InstanceShape("t4g.xlarge", vcpu=4, memory_gib=16, max_enis=4),
        InstanceShape("t4g.2xlarge", vcpu=8, memory_gib=32, max_enis=4),
        InstanceShape(
            "m5.large", vcpu=2, memory_gib=8, max_enis=3, trunked_task_limit=10
        ),
        InstanceShape(
            "m5.xlarge", vcpu=4, memory_gib=16, max_enis=4, trunked_task_limit=20
        ),
        InstanceShape(
            "m5.2xlarge", vcpu=8, memory_gib=32, max_enis=4, trunked_task_limit=40
        ),
        InstanceShape(
            "m5.4xlarge", vcpu=16, memory_gib=64, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "m5.8xlarge", vcpu=32, memory_gib=128, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "m5.12xlarge", vcpu=48, memory_gib=192, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "m5.16xlarge", vcpu=64, memory_gib=256, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape(
            "m5.24xlarge", vcpu=96, memory_gib=384, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape("m5.metal", vcpu=96, memory_gib=384, max_enis=15),
        InstanceShape(
            "m6i.large", vcpu=2, memory_gib=8, max_enis=3, trunked_task_limit=10
        ),
        InstanceShape(
            "m6i.xlarge", vcpu=4, memory_gib=16, max_enis=4, trunked_task_limit=20
        ),
        InstanceShape(
            "m6i.2xlarge", vcpu=8, memory_gib=32, max_enis=4, trunked_task_limit=40
        ),
        InstanceShape(
            "m6i.4xlarge", vcpu=16, memory_gib=64, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "m6i.8xlarge", vcpu=32, memory_gib=128, max_enis=8, trunked_task_limit=90
        ),
        InstanceShape(
            "m6i.12xlarge", vcpu=48, memory_gib=192, max_enis=8, trunked_task_limit=120
        ),
        InstanceShape(
            "m6i.16xlarge", vcpu=64, memory_gib=256, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape(
            "m6i.24xlarge", vcpu=96, memory_gib=384, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape(
            "m6i.32xlarge",
            vcpu=128,
            memory_gib=512,
            max_enis=15,
            trunked_task_limit=120,
        ),
        InstanceShape(
            "m6i.metal", vcpu=128, memory_gib=512, max_enis=15, trunked_task_limit=120
        ),
        # c5/c6i memory is HALF of the same-size m5/m6i/t3 shape -- e.g.
        # c5.xlarge is 8 GiB, not 16 (the t3.xlarge/m5.xlarge value). Do not
        # collapse these into a shared "xlarge row" across families.
        InstanceShape(
            "c5.large", vcpu=2, memory_gib=4, max_enis=3, trunked_task_limit=10
        ),
        InstanceShape(
            "c5.xlarge", vcpu=4, memory_gib=8, max_enis=4, trunked_task_limit=20
        ),
        InstanceShape(
            "c5.2xlarge", vcpu=8, memory_gib=16, max_enis=4, trunked_task_limit=40
        ),
        InstanceShape(
            "c5.4xlarge", vcpu=16, memory_gib=32, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "c5.9xlarge", vcpu=36, memory_gib=72, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "c5.12xlarge", vcpu=48, memory_gib=96, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "c5.18xlarge", vcpu=72, memory_gib=144, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape(
            "c5.24xlarge", vcpu=96, memory_gib=192, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape("c5.metal", vcpu=96, memory_gib=192, max_enis=15),
        InstanceShape(
            "c6i.large", vcpu=2, memory_gib=4, max_enis=3, trunked_task_limit=10
        ),
        InstanceShape(
            "c6i.xlarge", vcpu=4, memory_gib=8, max_enis=4, trunked_task_limit=20
        ),
        InstanceShape(
            "c6i.2xlarge", vcpu=8, memory_gib=16, max_enis=4, trunked_task_limit=40
        ),
        InstanceShape(
            "c6i.4xlarge", vcpu=16, memory_gib=32, max_enis=8, trunked_task_limit=60
        ),
        InstanceShape(
            "c6i.8xlarge", vcpu=32, memory_gib=64, max_enis=8, trunked_task_limit=90
        ),
        InstanceShape(
            "c6i.12xlarge", vcpu=48, memory_gib=96, max_enis=8, trunked_task_limit=120
        ),
        InstanceShape(
            "c6i.16xlarge", vcpu=64, memory_gib=128, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape(
            "c6i.24xlarge", vcpu=96, memory_gib=192, max_enis=15, trunked_task_limit=120
        ),
        InstanceShape(
            "c6i.32xlarge",
            vcpu=128,
            memory_gib=256,
            max_enis=15,
            trunked_task_limit=120,
        ),
        InstanceShape(
            "c6i.metal", vcpu=128, memory_gib=256, max_enis=15, trunked_task_limit=120
        ),
    ]
}


# Memory an EC2 container instance's nominal RAM never actually exposes to
# ECS tasks: the Linux kernel/platform overhead the ECS agent subtracts when
# it registers the instance, plus the same order of magnitude AWS itself
# reserves for critical system processes. AWS's memory-management docs give
# a worked example (m4.large: 8192 MiB nominal -> ~7985 MiB registered, i.e.
# ~207 MiB gone to kernel/platform overhead before any task ever runs) and
# separately document ECS_RESERVED_MEMORY=256 as their own example value for
# carving out headroom for system processes:
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/memory-management.html
# There's no single documented universal constant, so we use 256 MiB as a
# fixed, conservative floor >= the observed overhead. Without this, a task
# requesting an instance's full nominal memory (e.g. 4096 MiB on a t3.medium)
# would be deemed "fitting" by nominal arithmetic while never actually having
# enough allocatable memory to place in real ECS, and would sit PENDING
# forever.
RESERVED_MEMORY_MIB = 256


# awsvpc network mode gives every task its own ENI (unlike Fargate, EC2 has
# no ENI trunking on by default -- see the module-level comment on
# T3_LADDER). Every EC2 instance carries a primary network interface that
# EC2 attaches at launch and that can never be reassigned to a task; it
# always counts as one of ``InstanceShape.max_enis``. So the ceiling on
# awsvpc *task* ENIs per instance is always ``max_enis - 1``, verified
# against AWS ECS's own published "task limit without ENI trunking" numbers
# for several non-t3 instance types (see the T3_LADDER comment) -- every one
# of those equals max_enis - 1, never max_enis - 2 or more. This is a fixed
# AWS platform fact, not a heuristic estimate like RESERVED_MEMORY_MIB, so
# unlike that constant it isn't exposed as an auto_size() override.
ENI_RESERVED_FOR_PRIMARY = 1


# ECS's own default deployment_maximum_percent for a rolling deploy, and
# what services.tf.j2 renders for every non-stateful service: up to 200% of
# desired count may be RUNNING at once while a deploy is in flight. Stateful
# services (EFS-mounting, singleton schedulers) render 100 instead — they are
# stop-then-start precisely so two tasks never share the data dir — so peak
# demand is per-service, not a blanket doubling of the fleet.
DEPLOYMENT_MAX_PERCENT_DEFAULT = 200


@dataclass
class EC2TaskDemand:
    name: str
    cpu_units: int
    memory_mib: int
    replicas: int = 1
    # Ceiling on running tasks during a rolling deploy, as a percentage of
    # `replicas` — aws_ecs_service.deployment_maximum_percent. Defaults to
    # ECS's own 200 so a caller that doesn't supply it still models a normal
    # rolling deploy rather than silently assuming steady state.
    deployment_maximum_percent: int = DEPLOYMENT_MAX_PERCENT_DEFAULT

    @property
    def peak_replicas(self) -> int:
        """Tasks that can be running at once mid-rolling-deploy.

        ECS rounds down when applying deployment_maximum_percent to a desired
        count, but it will always run at least `replicas` — a deploy that
        couldn't hold the current tasks would be an outage, not a roll.
        """
        scaled = (self.replicas * self.deployment_maximum_percent) // 100
        return max(self.replicas, scaled)


@dataclass
class Sizing:
    instance_type: str
    min_size: int
    desired_size: int
    max_size: int


def auto_size(
    tasks: Iterable[EC2TaskDemand],
    ladder: list[InstanceShape] = T3_LADDER,
    safety_headroom: float = 1.2,
    max_cap: int = 10,
    reserved_memory_mib: int = RESERVED_MEMORY_MIB,
    size_for_rolling_deploy: bool = False,
) -> Sizing:
    """Pick instance_type + ASG sizes to host the given EC2 task demand.

    Rules:
      1. instance_type is the smallest shape whose *allocatable* memory
         (nominal memory minus ``reserved_memory_mib`` for the ECS agent +
         OS) and vcpu fit the *largest single task* (each task must fit on
         one instance — ECS does not split).
      2. desired_size = ceil( sum_task_resources * safety_headroom / instance_capacity ),
         independently for three dimensions — CPU, memory, and awsvpc task
         ENIs (rc-e5u.25.1; see ``ENI_RESERVED_FOR_PRIMARY``) — whichever
         needs the most instances wins. The ENI dimension counts every task
         (``sum(t.replicas for t in tasks)``, unfiltered by cpu/memory
         demand — EC2 task-level cpu/memory are optional, unlike Fargate,
         but an awsvpc task still consumes exactly one ENI regardless of its
         resource request) and is skipped for shapes with
         ``max_enis=None`` (falls back to cpu/memory-only sizing, the
         pre-rc-e5u.25.1 behavior).

         ``safety_headroom`` is applied uniformly across all three
         dimensions for one legible knob.

         ``size_for_rolling_deploy`` (rc-anl6, ``ec2_capacity.
         size_for_rolling_deploy``) chooses WHICH demand those three
         dimensions are summed over. Default False sums steady-state
         ``replicas``. True sums ``peak_replicas`` instead — the up-to-200%
         task duplication ECS permits mid-rolling-deploy for non-stateful
         services (``deployment_maximum_percent`` in services.tf.j2, 100 for
         stateful ones) — so the fleet can hold a deploy without the ASG
         scaling out first. It is opt-in because it raises the instance count
         of every stack that turns it on, typically by 1.5-2x, and that is a
         cost decision rather than rc's to make silently. With it off the
         provider still WARNS when peak demand exceeds the sized fleet, so
         the choice is informed rather than invisible.
      3. min_size = max(1, desired_size - 1). max_size = min(max_cap, desired_size * 2).

    Raises ``ValueError`` if no shape in the ladder fits the largest task, or
    if the computed desired_size exceeds ``max_cap`` (an ASG with
    desired_size > max_size is invalid Terraform/AWS state) — callers should
    enlarge the ladder / raise ``ec2_capacity.max`` / set instance_type
    explicitly.
    """
    tasks = list(tasks)
    replicas_of = (
        (lambda t: t.peak_replicas)
        if size_for_rolling_deploy
        else (lambda t: t.replicas)
    )
    total_task_count = sum(replicas_of(t) for t in tasks)
    demands = [t for t in tasks if t.cpu_units > 0 or t.memory_mib > 0]
    if not demands:
        shape = ladder[0]
        desired = max(
            1, _instances_needed_for_enis(shape, total_task_count, safety_headroom)
        )
        return _finalize_sizing(shape, desired, max_cap)

    max_cpu_single = max(t.cpu_units for t in demands)
    max_mem_single = max(t.memory_mib for t in demands)

    shape = _smallest_fit(ladder, max_cpu_single, max_mem_single, reserved_memory_mib)
    if shape is None:
        raise ValueError(
            f"no instance shape in the ladder fits a task needing "
            f"{max_cpu_single} CPU units / {max_mem_single} MiB memory "
            f"(after reserving {reserved_memory_mib} MiB per instance for the "
            f"ECS agent + OS); set provider_config.ecs.ec2_capacity."
            f"instance_type explicitly"
        )

    total_cpu = sum(t.cpu_units * replicas_of(t) for t in demands)
    total_mem = sum(t.memory_mib * replicas_of(t) for t in demands)

    instance_cpu = shape.vcpu * 1024
    instance_mem = shape.memory_gib * 1024

    by_cpu = math.ceil(total_cpu * safety_headroom / instance_cpu)
    by_mem = math.ceil(total_mem * safety_headroom / instance_mem)
    by_eni = _instances_needed_for_enis(shape, total_task_count, safety_headroom)
    desired = max(1, by_cpu, by_mem, by_eni)

    return _finalize_sizing(shape, desired, max_cap)


@dataclass
class FleetPressure:
    """How hard a fleet of ``shape`` is squeezed by a set of task demands.

    Computed for a single (shape, instance count) pair so the provider can
    say something concrete instead of "it might be tight": how many
    instances steady state needs, how many a rolling deploy needs, and
    whether the shape binpacks at all.
    """

    shape: InstanceShape
    steady_instances: int
    peak_instances: int
    # Services whose single-task cpu request meets or exceeds the whole
    # instance's CPU. One task per instance, no binpacking, and therefore no
    # cost advantage over Fargate — which is the entire reason to run EC2.
    cpu_saturating_tasks: list[str] = field(default_factory=list)
    steady_task_count: int = 0
    peak_task_count: int = 0
    # Which of the three dimensions actually decided steady_instances:
    # "cpu", "memory", "eni", or "" when nothing constrains. rc-hguq ask 4 --
    # "you need 7 instances" and "you need 7 instances because of a
    # networking limit you can lift with one account setting" are very
    # different messages, and only the second is actionable.
    binding_dimension: str = ""
    # True when the ENI dimension is what binds AND the shape supports ENI
    # trunking that is not currently in effect -- i.e. this fleet is sized by
    # a networking artifact the operator can remove.
    eni_bound_but_trunkable: bool = False

    @property
    def binpacks(self) -> bool:
        """True when steady state puts more than one task on some instance."""
        return (
            self.steady_instances > 0 and self.steady_task_count > self.steady_instances
        )


def measure_fleet(
    shape: InstanceShape,
    tasks: Iterable[EC2TaskDemand],
    safety_headroom: float = 1.0,
) -> FleetPressure:
    """Instances ``shape`` needs at rest and mid-rolling-deploy.

    Both numbers take the max across the same three dimensions auto_size()
    uses — CPU, memory and awsvpc task ENIs. The only difference is that the
    peak figure counts each service's ``peak_replicas`` instead of its
    steady ``replicas``, which is what ECS actually permits to be running
    while a deploy is in flight (rc-anl6).

    ``safety_headroom`` defaults to 1.0 here, unlike auto_size(): this
    measures the demand as declared, so a caller reporting numbers to a user
    quotes real task counts rather than padded ones.
    """
    tasks = list(tasks)
    demands = [t for t in tasks if t.cpu_units > 0 or t.memory_mib > 0]
    instance_cpu = shape.vcpu * 1024
    instance_mem = shape.memory_gib * 1024

    def _dimensions(replica_of) -> dict[str, int]:
        by_cpu = by_mem = 0
        if demands:
            total_cpu = sum(t.cpu_units * replica_of(t) for t in demands)
            total_mem = sum(t.memory_mib * replica_of(t) for t in demands)
            by_cpu = math.ceil(total_cpu * safety_headroom / instance_cpu)
            by_mem = math.ceil(total_mem * safety_headroom / instance_mem)
        by_eni = _instances_needed_for_enis(
            shape, sum(replica_of(t) for t in tasks), safety_headroom
        )
        return {"cpu": by_cpu, "memory": by_mem, "eni": by_eni}

    def _instances(dims: dict[str, int]) -> int:
        return max(1, *dims.values()) if tasks else 0

    steady_dims = _dimensions(lambda t: t.replicas)
    steady = _instances(steady_dims)
    # Ties go to cpu/memory: naming ENI as the culprit when CPU needs the
    # same number of instances would send the operator to enable trunking
    # for no benefit.
    binding = ""
    if tasks:
        binding = max(steady_dims, key=lambda k: (steady_dims[k], k != "eni"))
        if steady_dims[binding] <= 0:
            binding = ""

    return FleetPressure(
        shape=shape,
        steady_instances=steady,
        peak_instances=_instances(_dimensions(lambda t: t.peak_replicas)),
        binding_dimension=binding,
        eni_bound_but_trunkable=(
            binding == "eni"
            and shape.trunking_supported
            and shape.task_slots_override is None
        ),
        cpu_saturating_tasks=sorted(
            t.name for t in demands if t.cpu_units >= instance_cpu
        ),
        steady_task_count=sum(t.replicas for t in tasks),
        peak_task_count=sum(t.peak_replicas for t in tasks),
    )


def _instances_needed_for_enis(
    shape: InstanceShape, total_task_count: int, safety_headroom: float
) -> int:
    """Instances needed so ``total_task_count`` awsvpc tasks fit within
    ``shape.task_eni_slots`` -- ``max_enis - ENI_RESERVED_FOR_PRIMARY``
    normally, or AWS's published trunked limit for a shape that has been
    through ``with_trunking()``.

    Returns 0 (i.e. "does not constrain sizing") when the shape's ENI
    ceiling isn't modeled (``task_eni_slots is None``) or there are no tasks.
    """
    usable_enis = shape.task_eni_slots
    if usable_enis is None or total_task_count <= 0:
        return 0
    return math.ceil(total_task_count * safety_headroom / usable_enis)


def _finalize_sizing(shape: InstanceShape, desired: int, max_cap: int) -> Sizing:
    """Build the min/desired/max Sizing, refusing to emit an invalid ASG.

    desired_size can never exceed max_size in a valid
    ``aws_autoscaling_group`` — silently clamping desired down to max_cap
    would under-provision exactly the capacity auto_size() exists to
    guarantee, so this raises instead of emitting broken Terraform.
    """
    if desired > max_cap:
        raise ValueError(
            f"auto-sizing needs {desired} {shape.name} instances to cover the "
            f"declared EC2 task demand, but max_cap={max_cap}; raise "
            f"provider_config.ecs.ec2_capacity.max explicitly, reduce replica "
            f"counts, set instance_type to a larger shape, or turn off "
            f"ec2_capacity.size_for_rolling_deploy if it is on (it sizes for "
            f"peak deploy demand, which is up to 2x steady state)"
        )
    max_size = min(max_cap, max(desired * 2, desired + 1))
    min_size = max(1, desired - 1)
    return Sizing(
        instance_type=shape.name,
        min_size=min_size,
        desired_size=desired,
        max_size=max_size,
    )


def _smallest_fit(
    ladder: list[InstanceShape],
    cpu_units: int,
    memory_mib: int,
    reserved_memory_mib: int = RESERVED_MEMORY_MIB,
) -> "InstanceShape | None":
    """Find the smallest shape whose *allocatable* memory covers the request.

    "Allocatable" is nominal memory minus ``reserved_memory_mib`` — memory a
    real ECS container instance never reports as available to tasks (see
    ``RESERVED_MEMORY_MIB`` above). A task requesting memory exactly equal to
    an instance's nominal memory must therefore NOT be considered a fit on
    that instance; it needs to bump to the next rung of the ladder.
    """
    required_vcpu = math.ceil(cpu_units / 1024)
    for shape in ladder:
        allocatable_mem_mib = shape.memory_gib * 1024 - reserved_memory_mib
        if shape.vcpu >= required_vcpu and allocatable_mem_mib >= memory_mib:
            return shape
    return None


# What rc knows about the account's awsvpcTrunking setting. rc-hguq ask 3:
# the old message asserted "ENI trunking is not enabled" as fact when rc had
# never looked, so an operator who HAD enabled it read a verified finding
# where there was only an assumption. These three states are reported
# differently, and UNKNOWN is never worded as "not enabled".
TRUNKING_ENABLED = "enabled"
TRUNKING_DISABLED = "disabled"
TRUNKING_UNKNOWN = "unknown"


# awsvpcTrunking is a PER-REGION ECS account setting, not a global one.
# `put-account-setting-default` applies to the region it is called in, so an
# account can (and routinely does) have it enabled in the region someone
# tested in and disabled everywhere else -- verified live on 033937118837
# (2026-08-19): enabled in us-east-2, disabled in us-west-1/us-west-2/
# us-east-1. Every message below names the region for exactly this reason:
# "trunking is disabled for this account" sends an operator who already
# enabled it somewhere hunting for a bug that isn't there.
def _region_phrase(region: "str | None") -> str:
    return f"in {region}" if region else "in this region"


def _trunking_clause(
    shape: InstanceShape, state: str, region: "str | None" = None
) -> str:
    """Parenthetical explaining WHERE the slot number came from."""
    if shape.task_slots_override is not None:
        return " -- AWS's published limit with ENI trunking enabled"
    base = (
        " after reserving 1 for the instance's own primary ENI, "
        "ENI trunking not in effect"
    )
    if not shape.trunking_supported:
        return (
            base + f"; {shape.name} is not one of the instance types AWS "
            "supports ENI trunking on"
        )
    if state == TRUNKING_DISABLED:
        return (
            base + f"; the awsvpcTrunking account setting is disabled "
            f"{_region_phrase(region)} -- it is PER-REGION, so enabling it "
            f"elsewhere does not apply here"
        )
    if state == TRUNKING_UNKNOWN:
        return (
            base + f"; rc has NOT checked whether awsvpcTrunking is enabled "
            f"{_region_phrase(region)}"
        )
    return base


def _trunking_remedy(
    shape: InstanceShape, state: str, region: "str | None" = None
) -> str:
    """Lead the remedy with trunking when trunking is the real answer."""
    if shape.task_slots_override is not None or not shape.trunking_supported:
        return ""
    region_flag = f" --region {region}" if region else ""
    if state == TRUNKING_DISABLED:
        return (
            f"{shape.name} supports ENI trunking, which would raise this to "
            f"{shape.trunked_task_limit} task(s) per instance -- enable it "
            f"{_region_phrase(region)} with `aws ecs "
            f"put-account-setting-default --name awsvpcTrunking --value "
            f"enabled{region_flag}` and re-run (the setting is per-region; "
            f"check with `aws ecs list-account-settings --name awsvpcTrunking "
            f"--effective-settings{region_flag}`); otherwise "
        )
    if state == TRUNKING_UNKNOWN:
        return (
            f"{shape.name} supports ENI trunking, which would raise this to "
            f"{shape.trunked_task_limit} task(s) per instance -- if "
            f"awsvpcTrunking is already enabled {_region_phrase(region)} "
            f"(it is per-region), set "
            f"provider_config.ecs.ec2_capacity.eni_trunking: true to tell rc "
            f"so; otherwise "
        )
    return ""


def check_fixed_shape_capacity(
    shape: InstanceShape,
    tasks: Iterable[EC2TaskDemand],
    desired_size: int,
    reserved_memory_mib: int = RESERVED_MEMORY_MIB,
    trunking_state: str = TRUNKING_UNKNOWN,
    region: "str | None" = None,
) -> None:
    """Validate that ``desired_size`` instances of a caller-chosen ``shape``
    can host the declared EC2 task demand.

    auto_size() never runs when ``ec2_capacity.instance_type`` is set
    explicitly (_resolve_ec2_capacity picks the shape from config, not the
    ladder) -- so none of its cpu/memory/ENI feasibility checks apply on
    that path today, and infeasible demand only surfaces later as tasks
    stuck PENDING in real ECS with no explanation. This checks the same
    three dimensions against the single fixed shape instead of searching
    T3_LADDER.

    Uses zero safety headroom (unlike auto_size()'s 1.2x): ``desired_size``
    is the caller's own explicit choice, not something this function is
    deriving, so it only flags demand that CANNOT fit at all -- not demand
    that's merely tight. Silently no-ops when ``shape`` isn't in
    KNOWN_INSTANCE_SHAPES (unverified instance type -- "not modeled", same
    precedent as ``InstanceShape.max_enis=None``); callers look the shape up
    themselves and skip calling this when the lookup misses.

    Raises ``ValueError`` (same convention as auto_size()) naming the
    dimension (single-task fit, total cpu, total memory, or total ENI count)
    that doesn't fit.
    """
    tasks = list(tasks)
    demands = [t for t in tasks if t.cpu_units > 0 or t.memory_mib > 0]
    if demands:
        max_cpu_single = max(t.cpu_units for t in demands)
        max_mem_single = max(t.memory_mib for t in demands)
        if (
            _smallest_fit([shape], max_cpu_single, max_mem_single, reserved_memory_mib)
            is None
        ):
            raise ValueError(
                f"ec2_capacity.instance_type={shape.name!r} cannot host a task "
                f"needing {max_cpu_single} CPU units / {max_mem_single} MiB "
                f"memory (after reserving {reserved_memory_mib} MiB per "
                f"instance for the ECS agent + OS); pick a larger "
                f"instance_type or reduce the task's cpu/memory"
            )

        total_cpu = sum(t.cpu_units * t.replicas for t in demands)
        total_mem = sum(t.memory_mib * t.replicas for t in demands)
        instance_cpu = shape.vcpu * 1024
        instance_mem = shape.memory_gib * 1024
        if total_cpu > desired_size * instance_cpu:
            raise ValueError(
                f"ec2_capacity.desired={desired_size} {shape.name} "
                f"instance(s) provide {desired_size * instance_cpu} total "
                f"CPU units, but declared EC2 task demand needs {total_cpu}; "
                f"raise ec2_capacity.desired or pick a larger instance_type"
            )
        if total_mem > desired_size * instance_mem:
            raise ValueError(
                f"ec2_capacity.desired={desired_size} {shape.name} "
                f"instance(s) provide {desired_size * instance_mem} MiB "
                f"total memory, but declared EC2 task demand needs "
                f"{total_mem} MiB; raise ec2_capacity.desired or pick a "
                f"larger instance_type"
            )

    total_task_count = sum(t.replicas for t in tasks)
    usable_enis = shape.task_eni_slots
    if usable_enis is not None and total_task_count > 0:
        if total_task_count > desired_size * usable_enis:
            raise ValueError(
                f"ec2_capacity.desired={desired_size} {shape.name} "
                f"instance(s) provide {desired_size * usable_enis} awsvpc "
                f"task ENI slots ({usable_enis} per instance"
                f"{_trunking_clause(shape, trunking_state, region)}"
                f"), but {total_task_count} EC2-launch task(s) are declared; "
                f"{_trunking_remedy(shape, trunking_state, region)}raise "
                f"ec2_capacity.desired, pick a larger instance_type, or "
                f"reduce replica counts"
            )
