"""Heuristic auto-sizing for the ECS EC2 capacity ASG.

When the user omits ``provider_config.ecs.ec2_capacity.instance_type``, we
pick the smallest t3-family instance large enough to run the biggest single
EC2 task, then size the ASG (min/max/desired) to cover the sum of all EC2
task resource requests.

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


# Conservative t3 ladder. Add m5/m6i family via explicit instance_type.
T3_LADDER: list[InstanceShape] = [
    InstanceShape("t3.small", vcpu=2, memory_gib=2),
    InstanceShape("t3.medium", vcpu=2, memory_gib=4),
    InstanceShape("t3.large", vcpu=2, memory_gib=8),
    InstanceShape("t3.xlarge", vcpu=4, memory_gib=16),
    InstanceShape("t3.2xlarge", vcpu=8, memory_gib=32),
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
      2. desired_size = ceil( sum_task_resources * safety_headroom / instance_capacity ).
         We check both CPU and memory; whichever needs more instances wins.
      3. min_size = max(1, desired_size - 1). max_size = min(max_cap, desired_size * 2).

    Raises ``ValueError`` if no shape in the ladder fits the largest task —
    callers should either enlarge the ladder or set instance_type explicitly.
    """
    demands = [t for t in tasks if t.cpu_units > 0 or t.memory_mib > 0]
    if not demands:
        return Sizing(
            instance_type=ladder[0].name, min_size=1, desired_size=1, max_size=2
        )

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
    desired = max(1, by_cpu, by_mem)
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
