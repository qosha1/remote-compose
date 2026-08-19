"""rc-anl6: autosize was correct at rest and undersized during a deploy.

auto_size() picks the smallest shape fitting the largest single task and
sizes the ASG for STEADY STATE. ECS permits up to 200% task duplication
during a rolling deploy, so the fleet could be right at rest and badly
undersized at the only moment that matters.

debuggai-api is the trap in concrete form: celery-worker requests exactly
2048 CPU units, autosize picks t3.large which IS exactly 2048 — one task per
instance, zero binpacking, Fargate economics on an EC2 bill — and a roll
wants ~9 instances against an autosized desired of 6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.autosize import (
    KNOWN_INSTANCE_SHAPES,
    EC2TaskDemand,
    auto_size,
    measure_fleet,
)

# The stack from the bead, as declared.
DEBUGGAI_API = [
    EC2TaskDemand("django", 1024, 2048, replicas=2),
    EC2TaskDemand("nginx", 256, 512, replicas=2),
    EC2TaskDemand("celery-worker", 2048, 4096, replicas=3),
    EC2TaskDemand("celery-beat", 512, 1024, replicas=1, deployment_maximum_percent=100),
]


class TestPeakReplicas:
    def test_default_models_a_normal_200_percent_roll(self):
        assert EC2TaskDemand("w", 256, 512, replicas=3).peak_replicas == 6

    def test_stateful_service_does_not_duplicate(self):
        """deployment_maximum_percent=100 is stop-then-start — that's the
        whole point of the stateful path in services.tf.j2."""
        d = EC2TaskDemand("pg", 256, 512, replicas=1, deployment_maximum_percent=100)
        assert d.peak_replicas == 1

    def test_peak_never_drops_below_steady(self):
        """A deploy that couldn't hold the current tasks is an outage, not a
        roll — so rounding down must not go under `replicas`."""
        d = EC2TaskDemand("x", 256, 512, replicas=1, deployment_maximum_percent=150)
        assert d.peak_replicas == 1


class TestMeasureFleet:
    def test_reproduces_the_reported_numbers(self):
        """Steady 5-6, peak ~9 on t3.large: the bead's own arithmetic."""
        pressure = measure_fleet(KNOWN_INSTANCE_SHAPES["t3.large"], DEBUGGAI_API)
        assert pressure.steady_instances == 5
        assert pressure.peak_instances == 9
        assert pressure.cpu_saturating_tasks == ["celery-worker"]
        # The fleet binpacks in AGGREGATE (nginx tasks are small), which is
        # exactly why the aggregate signal alone would have missed this: the
        # pathology is per-service — celery-worker fills a whole t3.large by
        # itself. That is what cpu_saturating_tasks catches and why the
        # provider checks it before falling back to the aggregate.
        assert pressure.binpacks is True

    def test_the_manual_workaround_is_measurably_better(self):
        """t3.xlarge is what the operator pinned by hand; it binpacks."""
        pressure = measure_fleet(KNOWN_INSTANCE_SHAPES["t3.xlarge"], DEBUGGAI_API)
        assert pressure.cpu_saturating_tasks == []
        assert pressure.binpacks is True
        assert pressure.peak_instances < 9

    def test_no_tasks_needs_no_instances(self):
        pressure = measure_fleet(KNOWN_INSTANCE_SHAPES["t3.large"], [])
        assert pressure.steady_instances == 0
        assert pressure.binpacks is False


class TestSizeForRollingDeployKnob:
    def test_default_is_unchanged_steady_state_sizing(self):
        """Nobody's bill moves without an rc.yml edit."""
        assert auto_size(DEBUGGAI_API).desired_size == 6
        assert auto_size(DEBUGGAI_API, size_for_rolling_deploy=False).desired_size == 6

    def test_enabled_sizes_for_the_roll(self):
        sizing = auto_size(DEBUGGAI_API, max_cap=20, size_for_rolling_deploy=True)
        assert sizing.desired_size > 6

    def test_enabled_respects_stateful_services(self):
        """A stateful-only stack never duplicates, so the knob is a no-op."""
        tasks = [
            EC2TaskDemand("pg", 512, 1024, replicas=1, deployment_maximum_percent=100)
        ]
        assert (
            auto_size(tasks, size_for_rolling_deploy=True).desired_size
            == auto_size(tasks).desired_size
        )

    def test_blowing_the_cap_names_the_knob_that_caused_it(self):
        with pytest.raises(ValueError, match="size_for_rolling_deploy"):
            auto_size(DEBUGGAI_API, size_for_rolling_deploy=True)


def _ctx(tmp_path: Path, services, ec2_capacity=None) -> DeployContext:
    ecs: dict = {
        "region": "us-west-2",
        "cluster": "c",
        "vpc_cidr": "10.0.0.0/16",
        "default_launch_type": "EC2",
    }
    if ec2_capacity is not None:
        ecs["ec2_capacity"] = ec2_capacity
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
    )


def _warnings_for(tmp_path, services, ec2_capacity=None) -> list[str]:
    provider = ECSProvider()
    provider.emit_terraform(_ctx(tmp_path, services, ec2_capacity), tmp_path / "tf")
    return provider._warnings


class TestFleetPressureWarnings:
    def test_cpu_saturation_is_no_longer_a_thing_on_ec2(self, tmp_path):
        """startsim-u88y CHANGED THIS. It used to warn that a task requesting a
        whole instance's CPU means "nothing binpacks... the cost premise of
        running on EC2 does not hold".

        That warning was diagnosing the RESERVATION, and the reservation is gone:
        services.tf.j2 now omits task-level cpu on EC2, so a task declaring 2048
        units reserves nothing and shares the instance CPU. One task no longer
        fills one instance, so the warning is not merely silent — it would be
        false.

        The fleet still binpacks; it binpacks on memory and ENI slots, which are
        the dimensions ECS actually reserves for an EC2 task now.
        """
        warnings = _warnings_for(
            tmp_path,
            {
                "celery-worker": ServiceSpec(
                    name="celery-worker", cpu=2048, memory=4096, replicas=1
                )
            },
            {"instance_type": "t3.large", "desired": 2, "max": 4},
        )
        assert not [
            x for x in warnings if "ENTIRE CPU" in x
        ], "warned about CPU saturation for a task that no longer reserves CPU"

    def test_no_binpacking_at_all_is_reported(self, tmp_path):
        warnings = _warnings_for(
            tmp_path,
            {"solo": ServiceSpec(name="solo", cpu=256, memory=512, replicas=1)},
            {"instance_type": "t3.large", "desired": 1, "max": 2},
        )
        assert any("no binpacking at all" in w for w in warnings)

    def test_roll_headroom_warning_names_the_knob_and_the_numbers(self, tmp_path):
        services = {
            "django": ServiceSpec(name="django", cpu=1024, memory=2048, replicas=2),
            "worker": ServiceSpec(name="worker", cpu=1024, memory=2048, replicas=3),
        }
        warnings = _warnings_for(
            tmp_path, services, {"instance_type": "t3.large", "desired": 3, "max": 6}
        )
        [w] = [x for x in warnings if "PENDING" in x]
        assert "size_for_rolling_deploy" in w
        assert "deployment_maximum_percent" in w
        # The failure mode this stack actually hit in production.
        assert "worker did not pick up the task" in w

    def test_silent_when_the_fleet_already_holds_a_roll(self, tmp_path):
        services = {
            "a": ServiceSpec(name="a", cpu=256, memory=512, replicas=1),
            "b": ServiceSpec(name="b", cpu=256, memory=512, replicas=1),
        }
        warnings = _warnings_for(
            tmp_path,
            services,
            {"instance_type": "t3.2xlarge", "desired": 4, "max": 6},
        )
        assert not [w for w in warnings if "PENDING" in w]

    def test_unmodeled_instance_type_reports_nothing(self, tmp_path):
        warnings = _warnings_for(
            tmp_path,
            {"w": ServiceSpec(name="w", cpu=2048, memory=4096, replicas=3)},
            {"instance_type": "r7iz.metal-32xl", "desired": 1, "max": 2},
        )
        assert not [w for w in warnings if "ENTIRE CPU" in w or "PENDING" in w]

    def test_explicit_instance_type_still_only_warns_never_raises(self, tmp_path):
        """A config that works today must not start failing: peak headroom
        informs warnings, not the hard-fail feasibility checks."""
        services = {
            "worker": ServiceSpec(name="worker", cpu=1024, memory=2048, replicas=3)
        }
        _warnings_for(
            tmp_path, services, {"instance_type": "t3.large", "desired": 2, "max": 4}
        )  # must not raise

    def test_fargate_stack_gets_no_ec2_warnings(self, tmp_path):
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            {"web": ServiceSpec(name="web", cpu=256, memory=512, replicas=2)},
        )
        ctx.provider_config["ecs"]["default_launch_type"] = "FARGATE"
        provider.emit_terraform(ctx, tmp_path / "tf")
        assert provider._warnings == []


class TestKnobThroughConfig:
    def test_rc_yml_knob_reaches_auto_size(self, tmp_path):
        services = {
            "worker": ServiceSpec(name="worker", cpu=512, memory=1024, replicas=3)
        }
        provider = ECSProvider()
        capacity = provider._resolve_ec2_capacity(
            {"ec2_capacity": {"size_for_rolling_deploy": True, "max": 20}},
            [EC2TaskDemand("worker", 512, 1024, replicas=3)],
        )
        steady = provider._resolve_ec2_capacity(
            {"ec2_capacity": {"max": 20}},
            [EC2TaskDemand("worker", 512, 1024, replicas=3)],
        )
        assert capacity["desired_size"] > steady["desired_size"]
        assert services  # declared shape mirrors the demand above
