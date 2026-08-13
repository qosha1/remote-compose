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
from dataclasses import dataclass
from typing import Iterable


@dataclass
class InstanceShape:
    name: str
    vcpu: int  # 1 vCPU = 1024 ECS CPU units
    memory_gib: int
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


@dataclass
class EC2TaskDemand:
    name: str
    cpu_units: int
    memory_mib: int
    replicas: int = 1


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
         dimensions for one legible knob, but it is only an approximation
         for ENIs: it does NOT model the up-to-200% task duplication ECS
         permits mid-rolling-deploy for non-stateful services
         (deployment_maximum_percent=200 in services.tf.j2) — a 2-replica
         service can briefly need 4 ENI slots while a deploy is in flight.
         Left as a follow-up; bump ``safety_headroom`` or ``ec2_capacity.max``
         explicitly if tasks sit PENDING for ENIs during rolling deploys.
      3. min_size = max(1, desired_size - 1). max_size = min(max_cap, desired_size * 2).

    Raises ``ValueError`` if no shape in the ladder fits the largest task, or
    if the computed desired_size exceeds ``max_cap`` (an ASG with
    desired_size > max_size is invalid Terraform/AWS state) — callers should
    enlarge the ladder / raise ``ec2_capacity.max`` / set instance_type
    explicitly.
    """
    tasks = list(tasks)
    total_task_count = sum(t.replicas for t in tasks)
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

    total_cpu = sum(t.cpu_units * t.replicas for t in demands)
    total_mem = sum(t.memory_mib * t.replicas for t in demands)

    instance_cpu = shape.vcpu * 1024
    instance_mem = shape.memory_gib * 1024

    by_cpu = math.ceil(total_cpu * safety_headroom / instance_cpu)
    by_mem = math.ceil(total_mem * safety_headroom / instance_mem)
    by_eni = _instances_needed_for_enis(shape, total_task_count, safety_headroom)
    desired = max(1, by_cpu, by_mem, by_eni)

    return _finalize_sizing(shape, desired, max_cap)


def _instances_needed_for_enis(
    shape: InstanceShape, total_task_count: int, safety_headroom: float
) -> int:
    """Instances needed so ``total_task_count`` awsvpc tasks fit within
    ``shape``'s usable task-ENI slots (``max_enis - ENI_RESERVED_FOR_PRIMARY``).

    Returns 0 (i.e. "does not constrain sizing") when the shape's ENI
    ceiling isn't modeled (``max_enis is None``) or there are no tasks.
    """
    if shape.max_enis is None or total_task_count <= 0:
        return 0
    usable_enis = shape.max_enis - ENI_RESERVED_FOR_PRIMARY
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
            f"counts, or set instance_type to a larger shape"
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
