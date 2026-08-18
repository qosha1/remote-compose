"""Unit tests for ECS EC2 capacity auto-sizing."""

from __future__ import annotations

import pytest

from remote_compose.provider.ecs.autosize import (
    EC2TaskDemand,
    ENI_RESERVED_FOR_PRIMARY,
    InstanceShape,
    KNOWN_INSTANCE_SHAPES,
    RESERVED_MEMORY_MIB,
    Sizing,
    T3_LADDER,
    auto_size,
    check_fixed_shape_capacity,
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


class TestEniDensityConstraint:
    """rc-e5u.25.1: awsvpc gives every EC2 task its own ENI, and without ENI
    trunking (not viable for T3_LADDER -- see the T3_LADDER comment in
    autosize.py) the number of tasks an instance can host is capped by
    ``max_enis - ENI_RESERVED_FOR_PRIMARY``, not by cpu/memory. A pile of
    tiny, high-replica-count tasks can therefore need more instances than
    cpu/memory math alone would suggest."""

    def test_eni_reservation_is_exactly_one(self):
        """The instance's own primary ENI is the only ENI that's never
        available to a task -- this is a fixed AWS platform fact rc's math
        depends on, not a tunable, so pin it against regression."""
        assert ENI_RESERVED_FOR_PRIMARY == 1

    def test_high_replica_tiny_task_forces_extra_instances_on_t3_small(self):
        """3 replicas of a tiny task (128 cpu / 128 MiB) fit t3.small by cpu
        and memory with room to spare (desired=1 by those dimensions alone),
        but t3.small has max_enis=3 -> usable_enis=2, so 3 concurrent awsvpc
        tasks need ceil(3 * 1.2 / 2) = 2 instances."""
        sz = auto_size(
            [EC2TaskDemand("tiny", cpu_units=128, memory_mib=128, replicas=3)]
        )
        assert sz.instance_type == "t3.small"
        assert sz.desired_size == 2

    def test_eni_dimension_scales_with_replica_count_not_resource_size(self):
        """Same tiny per-task footprint, more replicas -> more instances
        needed, even though cpu/memory math alone would still say 1."""
        sz = auto_size(
            [EC2TaskDemand("tiny", cpu_units=128, memory_mib=128, replicas=5)]
        )
        assert sz.instance_type == "t3.small"
        # ceil(5 * 1.2 / 2) = 3
        assert sz.desired_size == 3

    def test_larger_shape_has_more_usable_enis(self):
        """t3.xlarge has max_enis=4 -> usable_enis=3, one more slot per
        instance than the small/medium/large rungs (all usable_enis=2)."""
        single_rung = [InstanceShape("t3.xlarge", vcpu=4, memory_gib=16, max_enis=4)]
        sz = auto_size(
            [EC2TaskDemand("tiny", cpu_units=128, memory_mib=128, replicas=7)],
            ladder=single_rung,
        )
        # ceil(7 * 1.2 / 3) = 3; cpu/mem math alone would say 1.
        assert sz.desired_size == 3

    def test_custom_ladder_without_max_enis_skips_eni_dimension(self):
        """A caller-supplied ladder that doesn't set max_enis (the default)
        gets the pre-rc-e5u.25.1 behavior: cpu/memory only. This is the
        documented escape hatch for custom ladders rc hasn't verified ENI
        numbers for -- it must not silently apply T3 numbers to an
        unrelated shape."""
        custom = [InstanceShape("m5.large", vcpu=2, memory_gib=8)]  # max_enis=None
        sz = auto_size(
            [EC2TaskDemand("tiny", cpu_units=128, memory_mib=128, replicas=50)],
            ladder=custom,
        )
        # cpu: ceil(128*50*1.2/2048) = 4. If the (unmodeled) ENI dimension
        # were applied here it would demand many more instances than this.
        assert sz.desired_size == 4

    def test_total_task_count_includes_zero_resource_tasks(self):
        """EC2 task-level cpu/memory are optional (unlike Fargate) -- a task
        declaring cpu=0/memory=0 is filtered out of the cpu/memory sizing
        math entirely (it falls into auto_size()'s "no demand" branch), but
        it still launches an awsvpc task that consumes one ENI. The ENI
        dimension must count it anyway."""
        sz = auto_size(
            [EC2TaskDemand("unbounded", cpu_units=0, memory_mib=0, replicas=5)]
        )
        assert sz.instance_type == "t3.small"
        # ceil(5 * 1.2 / 2) = 3
        assert sz.desired_size == 3

    def test_max_cap_exactly_equal_to_eni_driven_desired_does_not_raise(self):
        """The tightest boundary: max_cap set to exactly the ENI-driven
        desired_size must still produce a valid min <= desired <= max
        Sizing, not a false-positive raise."""
        sz = auto_size(
            [EC2TaskDemand("tiny", cpu_units=128, memory_mib=128, replicas=30)],
            max_cap=18,
        )
        # ceil(30 * 1.2 / 2) = 18, exactly at the ceiling.
        assert sz.min_size == 17
        assert sz.desired_size == 18
        assert sz.max_size == 18

    def test_eni_driven_desired_exceeding_max_cap_raises(self):
        """desired_size can never legally exceed max_size in a real ASG.
        Silently clamping would under-provision exactly the capacity
        auto_size() exists to guarantee, so this must raise loudly instead
        of emitting a broken aws_autoscaling_group (min/desired > max)."""
        with pytest.raises(ValueError, match="max_cap"):
            auto_size(
                [EC2TaskDemand("tiny", cpu_units=128, memory_mib=128, replicas=30)]
            )


class TestIntegrationShape:
    def test_returns_sizing_dataclass(self):
        sz = auto_size([EC2TaskDemand("x", cpu_units=256, memory_mib=512)])
        assert isinstance(sz, Sizing)
        assert sz.instance_type in [s.name for s in T3_LADDER]


class TestKnownInstanceShapes:
    """rc-e5u.25.10: ec2_capacity.instance_type set explicitly bypasses
    auto_size() entirely, so nothing else validates cpu/memory/ENI fit for
    it. KNOWN_INSTANCE_SHAPES + check_fixed_shape_capacity close that gap
    for verified instance types."""

    def test_t3a_small_eni_ceiling_differs_from_t3_and_t4g_small(self):
        """The whole reason this table isn't derived from a per-family
        formula: t3a.small has HALF the usable ENI slots of its same-size
        t3/t4g siblings despite an identical vCPU/memory shape."""
        assert KNOWN_INSTANCE_SHAPES["t3.small"].max_enis == 3
        assert KNOWN_INSTANCE_SHAPES["t4g.small"].max_enis == 3
        assert KNOWN_INSTANCE_SHAPES["t3a.small"].max_enis == 2

    def test_c5_xlarge_memory_is_half_of_m5_xlarge(self):
        """c5/c6i (compute-optimized) carry half the memory of m5/m6i/t3 at
        the same vCPU count and 'xlarge' label -- a table that collapsed
        same-size-label rows across families would get this wrong."""
        assert KNOWN_INSTANCE_SHAPES["c5.xlarge"].memory_gib == 8
        assert KNOWN_INSTANCE_SHAPES["m5.xlarge"].memory_gib == 16
        assert KNOWN_INSTANCE_SHAPES["t3.xlarge"].memory_gib == 16

    def test_nano_fractional_memory(self):
        assert KNOWN_INSTANCE_SHAPES["t3.nano"].memory_gib == 0.5


class TestCheckFixedShapeCapacity:
    def _shape(self, **overrides):
        base = dict(name="m5.large", vcpu=2, memory_gib=8, max_enis=3)
        base.update(overrides)
        return InstanceShape(**base)

    def test_fits_does_not_raise(self):
        check_fixed_shape_capacity(
            self._shape(),
            [EC2TaskDemand("web", cpu_units=512, memory_mib=1024, replicas=2)],
            desired_size=1,
        )

    def test_single_task_too_big_for_shape_raises(self):
        with pytest.raises(ValueError, match="cannot host a task"):
            check_fixed_shape_capacity(
                self._shape(),
                [EC2TaskDemand("huge", cpu_units=4096, memory_mib=16384)],
                desired_size=5,
            )

    def test_total_cpu_exceeds_fleet_capacity_raises(self):
        with pytest.raises(ValueError, match="total CPU units"):
            check_fixed_shape_capacity(
                self._shape(),
                [EC2TaskDemand("a", cpu_units=2048, memory_mib=512, replicas=2)],
                desired_size=1,  # 1 * 2 vCPU (2048 units) < 2 * 2048 needed
            )

    def test_total_memory_exceeds_fleet_capacity_raises(self):
        with pytest.raises(ValueError, match="total memory"):
            check_fixed_shape_capacity(
                self._shape(),
                [EC2TaskDemand("a", cpu_units=256, memory_mib=6144, replicas=2)],
                desired_size=1,  # 1 * 8192 MiB < 2 * 6144 needed
            )

    def test_eni_count_exceeds_fleet_capacity_raises(self):
        # t3a.small: max_enis=2 -> 1 usable per instance.
        with pytest.raises(ValueError, match="awsvpc task ENI slots"):
            check_fixed_shape_capacity(
                KNOWN_INSTANCE_SHAPES["t3a.small"],
                [EC2TaskDemand("w", cpu_units=256, memory_mib=256, replicas=3)],
                desired_size=1,  # 1 usable ENI slot < 3 tasks
            )

    def test_eni_dimension_skipped_when_shape_has_no_max_enis(self):
        check_fixed_shape_capacity(
            self._shape(max_enis=None),
            [EC2TaskDemand("w", cpu_units=1, memory_mib=1, replicas=50)],
            desired_size=1,
        )

    def test_zero_demand_tasks_only_checked_on_eni_dimension(self):
        """cpu_units=0/memory_mib=0 tasks (EC2's task-level resources are
        optional, unlike Fargate's) skip the cpu/memory dimensions but still
        count toward the ENI dimension -- an awsvpc task always consumes
        exactly one ENI regardless of its resource request."""
        with pytest.raises(ValueError, match="awsvpc task ENI slots"):
            check_fixed_shape_capacity(
                KNOWN_INSTANCE_SHAPES["t3a.small"],
                [EC2TaskDemand("w", cpu_units=0, memory_mib=0, replicas=3)],
                desired_size=1,
            )
