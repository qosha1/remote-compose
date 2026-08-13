"""Unit tests for ECS EC2 capacity auto-sizing."""

from __future__ import annotations

import pytest

from remote_compose.provider.ecs.autosize import (
    EC2TaskDemand,
    InstanceShape,
    RESERVED_MEMORY_MIB,
    Sizing,
    T3_LADDER,
    auto_size,
)


class TestInstanceTypeChoice:
    def test_tiny_task_picks_smallest(self):
        sz = auto_size([EC2TaskDemand("web", cpu_units=256, memory_mib=512)])
        assert sz.instance_type == "t3.small"

    def test_1_vcpu_2gb_task_needs_headroom_bumps_past_small(self):
        """2048 MiB request == t3.small's full nominal memory (2 GiB); after
        reserving RESERVED_MEMORY_MIB for the agent/OS, t3.small no longer
        has enough allocatable memory, so this must bump to t3.medium."""
        sz = auto_size([EC2TaskDemand("x", cpu_units=1024, memory_mib=2048)])
        assert sz.instance_type == "t3.medium"

    def test_2_vcpu_4gb_task_needs_headroom_bumps_past_medium(self):
        """4096 MiB request == t3.medium's full nominal memory (4 GiB); after
        reserving RESERVED_MEMORY_MIB for the agent/OS, t3.medium no longer
        has enough allocatable memory, so this must bump to t3.large."""
        sz = auto_size([EC2TaskDemand("x", cpu_units=2048, memory_mib=4096)])
        assert sz.instance_type == "t3.large"

    def test_high_memory_task_climbs_ladder(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=1024, memory_mib=12288)])
        # 12 GiB needs at least t3.xlarge (16 GiB)
        assert sz.instance_type == "t3.xlarge"

    def test_high_cpu_task_climbs_ladder(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=6144, memory_mib=2048)])
        # 6144 CPU units = 6 vCPU → needs t3.2xlarge (8 vCPU)
        assert sz.instance_type == "t3.2xlarge"

    def test_unfittable_raises(self):
        with pytest.raises(ValueError, match="no instance shape"):
            auto_size([EC2TaskDemand("huge", cpu_units=32768, memory_mib=65536)])

    def test_largest_task_wins_instance_choice(self):
        """One tiny task shouldn't pull instance size down; the biggest must fit."""
        sz = auto_size(
            [
                EC2TaskDemand("web", cpu_units=256, memory_mib=512),
                EC2TaskDemand("worker", cpu_units=4096, memory_mib=8192),
            ]
        )
        assert sz.instance_type == "t3.xlarge"  # fits the 4 vCPU / 8 GiB worker


class TestAsgSizing:
    def test_summed_demand_sizes_asg(self):
        """summed 6144 CPU + 12 GiB mem with safety headroom should require multiple instances."""
        tasks = [
            EC2TaskDemand(
                "a", cpu_units=2048, memory_mib=4096, replicas=3
            ),  # 6144 cpu, 12 GB total
        ]
        sz = auto_size(tasks)
        # largest task = 2048/4096 → 4096 MiB == t3.medium's full nominal
        # memory, so after the agent/OS memory headroom it no longer fits
        # t3.medium and bumps to t3.large (2 vCPU / 8 GiB)
        # sum = 6144 cpu / 12288 mib = 6 vCPU / 12 GiB
        # 6 vCPU / 2 vCPU per instance = 3, * 1.2 headroom = ceil(3.6) = 4
        # 12 GiB / 8 GiB per instance = 1.5, * 1.2 headroom = ceil(1.8) = 2
        assert sz.instance_type == "t3.large"
        assert sz.desired_size >= 3

    def test_min_max_respects_desired(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=1024, memory_mib=2048)])
        assert sz.min_size <= sz.desired_size <= sz.max_size
        assert sz.max_size <= 10  # default cap

    def test_empty_task_list_returns_defaults(self):
        sz = auto_size([])
        assert sz.desired_size >= 1
        assert sz.instance_type  # some default


class TestMemoryHeadroomReservation:
    """rc-e5u.25.2: a task sized to an instance's full nominal memory must
    never be considered a fit for that instance. Real ECS container
    instances never register their full nominal RAM as allocatable — the
    agent + OS reserve some of it first (see RESERVED_MEMORY_MIB) — so a
    task requesting the nominal amount would sit PENDING forever."""

    def test_task_at_exact_nominal_memory_raises_when_no_larger_rung(self):
        """Isolate the fit check with a single-rung ladder: a task asking for
        exactly that shape's nominal memory has no headroom to land, and
        there's nowhere else to bump to, so this must raise."""
        single_rung = [InstanceShape("t3.medium", vcpu=2, memory_gib=4)]
        with pytest.raises(ValueError, match="no instance shape"):
            auto_size(
                [EC2TaskDemand("x", cpu_units=1024, memory_mib=4096)],
                ladder=single_rung,
            )

    def test_task_at_exact_nominal_memory_bumps_to_next_rung(self):
        """Same 4096 MiB request against the full ladder: must skip
        t3.medium (no headroom left) and land on t3.large instead."""
        sz = auto_size([EC2TaskDemand("x", cpu_units=1024, memory_mib=4096)])
        assert sz.instance_type == "t3.large"

    def test_task_leaving_reserved_headroom_still_fits_nominal_shape(self):
        """A task that leaves at least RESERVED_MEMORY_MIB of slack should
        still land on the shape its nominal memory suggests — the fix must
        not be more conservative than the documented reservation."""
        sz = auto_size(
            [EC2TaskDemand("x", cpu_units=1024, memory_mib=4096 - RESERVED_MEMORY_MIB)]
        )
        assert sz.instance_type == "t3.medium"

    def test_reserved_memory_mib_is_configurable(self):
        """Callers can override the reservation via auto_size(...,
        reserved_memory_mib=...); zero headroom restores the old boundary
        behavior for callers who explicitly opt out."""
        sz = auto_size(
            [EC2TaskDemand("x", cpu_units=1024, memory_mib=4096)],
            reserved_memory_mib=0,
        )
        assert sz.instance_type == "t3.medium"


class TestCustomLadder:
    def test_user_supplies_alternate_ladder(self):
        custom = [
            InstanceShape("m5.large", vcpu=2, memory_gib=8),
            InstanceShape("m5.xlarge", vcpu=4, memory_gib=16),
        ]
        sz = auto_size(
            [EC2TaskDemand("x", cpu_units=2048, memory_mib=6144)], ladder=custom
        )
        assert sz.instance_type == "m5.large"


class TestIntegrationShape:
    def test_returns_sizing_dataclass(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=256, memory_mib=512)])
        assert isinstance(sz, Sizing)
        assert sz.instance_type in [s.name for s in T3_LADDER]
