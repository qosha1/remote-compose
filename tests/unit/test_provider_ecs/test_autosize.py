"""Unit tests for ECS EC2 capacity auto-sizing."""

from __future__ import annotations

import pytest

from remote_compose.provider.ecs.autosize import (
    EC2TaskDemand,
    InstanceShape,
    Sizing,
    T3_LADDER,
    auto_size,
)


class TestInstanceTypeChoice:
    def test_tiny_task_picks_smallest(self):
        sz = auto_size([EC2TaskDemand("web", cpu_units=256, memory_mib=512)])
        assert sz.instance_type == "t3.small"

    def test_1_vcpu_2gb_task_fits_small(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=1024, memory_mib=2048)])
        assert sz.instance_type == "t3.small"

    def test_2_vcpu_4gb_task_picks_medium(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=2048, memory_mib=4096)])
        assert sz.instance_type == "t3.medium"

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
        sz = auto_size([
            EC2TaskDemand("web",   cpu_units=256,  memory_mib=512),
            EC2TaskDemand("worker", cpu_units=4096, memory_mib=8192),
        ])
        assert sz.instance_type == "t3.xlarge"  # fits the 4 vCPU / 8 GiB worker


class TestAsgSizing:
    def test_summed_demand_sizes_asg(self):
        """summed 6144 CPU + 12 GiB mem with safety headroom should require multiple instances."""
        tasks = [
            EC2TaskDemand("a", cpu_units=2048, memory_mib=4096, replicas=3),  # 6144 cpu, 12 GB total
        ]
        sz = auto_size(tasks)
        # largest task = 2048/4096 → t3.medium (2 vCPU / 4 GiB)
        # sum = 6144 cpu / 12288 mib = 6 vCPU / 12 GiB
        # 6 vCPU / 2 vCPU per instance = 3, * 1.2 headroom = ceil(3.6) = 4
        # 12 GiB / 4 GiB per instance = 3, * 1.2 headroom = ceil(3.6) = 4
        assert sz.instance_type == "t3.medium"
        assert sz.desired_size >= 3

    def test_min_max_respects_desired(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=1024, memory_mib=2048)])
        assert sz.min_size <= sz.desired_size <= sz.max_size
        assert sz.max_size <= 10  # default cap

    def test_empty_task_list_returns_defaults(self):
        sz = auto_size([])
        assert sz.desired_size >= 1
        assert sz.instance_type  # some default


class TestCustomLadder:
    def test_user_supplies_alternate_ladder(self):
        custom = [
            InstanceShape("m5.large", vcpu=2, memory_gib=8),
            InstanceShape("m5.xlarge", vcpu=4, memory_gib=16),
        ]
        sz = auto_size([EC2TaskDemand("x", cpu_units=2048, memory_mib=6144)], ladder=custom)
        assert sz.instance_type == "m5.large"


class TestIntegrationShape:
    def test_returns_sizing_dataclass(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=256, memory_mib=512)])
        assert isinstance(sz, Sizing)
        assert sz.instance_type in [s.name for s in T3_LADDER]
