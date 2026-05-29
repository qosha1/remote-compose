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
) -> Sizing:
    """Pick instance_type + ASG sizes to host the given EC2 task demand.

    Rules:
      1. instance_type is the smallest shape that fits the *largest single task*
         (each task must fit on one instance — ECS does not split).
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

    shape = _smallest_fit(ladder, max_cpu_single, max_mem_single)
    if shape is None:
        raise ValueError(
            f"no instance shape in the ladder fits a task needing "
            f"{max_cpu_single} CPU units / {max_mem_single} MiB memory; "
            f"set provider_config.ecs.ec2_capacity.instance_type explicitly"
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
    ladder: list[InstanceShape], cpu_units: int, memory_mib: int
) -> "InstanceShape | None":
    required_vcpu = math.ceil(cpu_units / 1024)
    required_mem_gib = math.ceil(memory_mib / 1024)
    for shape in ladder:
        if shape.vcpu >= required_vcpu and shape.memory_gib >= required_mem_gib:
            return shape
    return None
