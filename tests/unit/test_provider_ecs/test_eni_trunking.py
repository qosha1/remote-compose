"""rc-hguq: model ENI trunking so a fleet is sized by workload, not networking.

Without trunking an awsvpc task costs one whole ENI, and ENI counts are FLAT
across the useful part of the m5 range -- m5.2xlarge is twice the box of an
m5.xlarge with the same 3 task slots. debuggai-api's 11 right-sized tasks
(4.5 vCPU / 24.8 GiB of actual work) therefore needed 4 instances at rest and
~8 mid-roll: 28 vCPU of instances to run 4.5 vCPU of work.

The awsvpcTrunking account setting lifts that cap, and it is PER-REGION --
which is the trap these tests pin hardest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.autosize import (
    KNOWN_INSTANCE_SHAPES,
    T3_LADDER,
    TRUNKING_DISABLED,
    TRUNKING_UNKNOWN,
    EC2TaskDemand,
    check_fixed_shape_capacity,
    measure_fleet,
)

# The #348 stack: 7 services, 11 tasks, reservations right-sized from seven
# days of CloudWatch (a 17x CPU over-reservation removed).
DEBUGGAI_348 = {
    "django": (512, 2048, 2),
    "nginx": (256, 512, 2),
    "celery-worker": (512, 2048, 3),
    "celery-beat": (256, 512, 1),
    "celery-browser": (512, 4096, 1),
    "redis": (256, 512, 1),
    "flower": (256, 512, 1),
}
DEMANDS_348 = [
    EC2TaskDemand(n, cpu, mem, replicas=r) for n, (cpu, mem, r) in DEBUGGAI_348.items()
]


class TestTrunkedLimitTable:
    def test_untrunked_column_confirms_the_max_enis_table(self):
        """AWS's published "task limit WITHOUT trunking" must equal rc's own
        max_enis - 1 for every modeled shape.

        That column comes from a different AWS source than the
        describe-instance-types call max_enis was built from, so agreement is
        an independent check on the whole table.
        """
        published_untrunked = {
            "m5.large": 2,
            "m5.xlarge": 3,
            "m5.2xlarge": 3,
            "m5.4xlarge": 7,
            "m5.8xlarge": 7,
            "m5.12xlarge": 7,
            "m5.16xlarge": 14,
            "m5.24xlarge": 14,
            "c5.large": 2,
            "c5.xlarge": 3,
            "c5.2xlarge": 3,
            "c5.4xlarge": 7,
            "c5.18xlarge": 14,
            "c6i.large": 2,
            "c6i.xlarge": 3,
            "m6i.8xlarge": 7,
            "m6i.16xlarge": 14,
        }
        for name, expected in published_untrunked.items():
            assert KNOWN_INSTANCE_SHAPES[name].task_eni_slots == expected, name

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("m5.xlarge", 20),
            ("m5.2xlarge", 40),
            ("m5.4xlarge", 60),
            ("m6i.8xlarge", 90),
            ("c6i.12xlarge", 120),
        ],
    )
    def test_trunked_limits_are_the_published_numbers(self, name, expected):
        assert KNOWN_INSTANCE_SHAPES[name].trunked_task_limit == expected
        assert KNOWN_INSTANCE_SHAPES[name].with_trunking().task_eni_slots == expected

    @pytest.mark.parametrize(
        "name", ["t3.large", "t3.2xlarge", "t3a.xlarge", "t4g.medium"]
    )
    def test_burstable_family_is_verified_ineligible(self, name):
        """No t3/t3a/t4g row appears anywhere in AWS's trunking tables. That
        absence is the positive evidence behind T3_LADDER's claim, and it is
        why trunking is a no-op for rc's default ladder."""
        shape = KNOWN_INSTANCE_SHAPES[name]
        assert shape.trunking_supported is False
        assert shape.with_trunking() is shape

    def test_whole_default_ladder_is_ineligible(self):
        assert not any(s.trunking_supported for s in T3_LADDER)

    @pytest.mark.parametrize("name", ["m5.metal", "c5.metal"])
    def test_metal_siblings_are_excluded_despite_eligible_families(self, name):
        """The trap: same family as eligible siblings, named in AWS's
        explicit not-supported list. A limit inferred from the family would
        silently be wrong."""
        assert KNOWN_INSTANCE_SHAPES[name].trunking_supported is False
        assert KNOWN_INSTANCE_SHAPES["c6i.metal"].trunking_supported is True


class TestFleetSizing:
    def test_reproduces_the_reported_eni_bound_fleet(self):
        """4 instances at rest for 11 tasks whose CPU/memory would fit on 2."""
        pressure = measure_fleet(KNOWN_INSTANCE_SHAPES["m5.xlarge"], DEMANDS_348)
        assert pressure.steady_instances == 4
        assert pressure.binding_dimension == "eni"
        assert pressure.eni_bound_but_trunkable is True

    def test_bigger_box_same_family_buys_nothing(self):
        """m5.2xlarge is twice the instance with the same 3 task slots --
        the reason 'just buy a bigger box' does not work here."""
        xl = measure_fleet(KNOWN_INSTANCE_SHAPES["m5.xlarge"], DEMANDS_348)
        xxl = measure_fleet(KNOWN_INSTANCE_SHAPES["m5.2xlarge"], DEMANDS_348)
        assert xl.steady_instances == xxl.steady_instances == 4

    def test_trunking_makes_the_workload_the_constraint_again(self):
        pressure = measure_fleet(
            KNOWN_INSTANCE_SHAPES["m5.xlarge"].with_trunking(), DEMANDS_348
        )
        assert pressure.steady_instances == 2
        assert pressure.binding_dimension in ("cpu", "memory")
        assert pressure.eni_bound_but_trunkable is False


class TestHonestMessaging:
    """Ask 3: stop asserting 'ENI trunking is not enabled' as a fact."""

    def _msg(self, state, region=None):
        with pytest.raises(ValueError) as exc:
            check_fixed_shape_capacity(
                KNOWN_INSTANCE_SHAPES["m5.xlarge"],
                DEMANDS_348,
                desired_size=3,
                trunking_state=state,
                region=region,
            )
        return str(exc.value)

    def test_unchecked_says_unchecked_not_disabled(self):
        msg = self._msg(TRUNKING_UNKNOWN, "us-west-2")
        assert "has NOT checked" in msg
        assert "is disabled" not in msg
        assert "eni_trunking: true" in msg

    def test_checked_disabled_says_so_and_names_the_region(self):
        msg = self._msg(TRUNKING_DISABLED, "us-west-2")
        assert "disabled in us-west-2" in msg
        assert "PER-REGION" in msg
        assert "--region us-west-2" in msg

    def test_names_what_trunking_would_buy(self):
        assert "20 task(s) per instance" in self._msg(TRUNKING_DISABLED, "us-west-2")

    def test_ineligible_shape_says_so_rather_than_suggesting_trunking(self):
        with pytest.raises(ValueError) as exc:
            check_fixed_shape_capacity(
                KNOWN_INSTANCE_SHAPES["t3.large"],
                # cpu/memory 0 so ONLY the ENI dimension can fail (EC2
                # task-level cpu/memory are optional, unlike Fargate, but an
                # awsvpc task still consumes a slot).
                [EC2TaskDemand("w", 0, 0, replicas=9)],
                desired_size=1,
                trunking_state=TRUNKING_DISABLED,
                region="us-west-2",
            )
        msg = str(exc.value)
        assert "not one of the instance types AWS supports ENI trunking on" in msg
        assert "put-account-setting-default" not in msg


def _ctx(tmp_path: Path, region="us-west-2", ec2_capacity=None, services=None):
    ecs: dict = {
        "region": region,
        "cluster": "c",
        "vpc_cidr": "10.0.0.0/16",
        "default_launch_type": "EC2",
    }
    if ec2_capacity is not None:
        ecs["ec2_capacity"] = ec2_capacity
    return DeployContext(
        project="debuggai-api",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services
        or {
            n: ServiceSpec(name=n, cpu=cpu, memory=mem, replicas=r)
            for n, (cpu, mem, r) in DEBUGGAI_348.items()
        },
    )


class _ECS:
    def __init__(self, value="enabled", rows=None, error=None):
        self.value = value
        self.rows = rows
        self.error = error
        self.calls: list[dict] = []

    def list_account_settings(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        if self.rows is not None:
            return {"settings": self.rows}
        return {"settings": [{"name": "awsvpcTrunking", "value": self.value}]}


class _Session:
    def __init__(self, ecs):
        self.ecs = ecs

    def client(self, name, region_name=None):
        self.ecs.region_name = region_name
        return self.ecs


class TestDetection:
    def _provider(self, ecs):
        return ECSProvider(session_factory=lambda _c: _Session(ecs))

    def test_reads_the_effective_per_region_setting(self, tmp_path):
        ecs = _ECS(value="enabled")
        ctx = _ctx(tmp_path, region="us-east-2")
        self._provider(ecs).preflight(ctx)
        assert ctx.eni_trunking is True
        assert ecs.calls == [{"name": "awsvpcTrunking", "effectiveSettings": True}]
        # Queried in the STACK's region, not the ambient default. The account
        # this was built against had it enabled in us-east-2 and disabled in
        # us-west-1/2 and us-east-1.
        assert ecs.region_name == "us-east-2"

    def test_disabled_is_a_finding(self, tmp_path):
        ctx = _ctx(tmp_path)
        self._provider(_ECS(value="disabled")).preflight(ctx)
        assert ctx.eni_trunking is False

    def test_absent_row_means_never_set_which_means_disabled(self, tmp_path):
        ctx = _ctx(tmp_path)
        self._provider(_ECS(rows=[])).preflight(ctx)
        assert ctx.eni_trunking is False

    def test_failed_lookup_stays_unknown_and_warns(self, tmp_path):
        """A probe that could not run is not evidence trunking is off."""
        ctx = _ctx(tmp_path)
        provider = self._provider(_ECS(error=RuntimeError("AccessDenied")))
        provider.preflight(ctx)
        assert ctx.eni_trunking is None
        assert any("ecs:ListAccountSettings" in w for w in provider._warnings)

    def test_fargate_only_stack_makes_no_call(self, tmp_path):
        ecs = _ECS()
        ctx = _ctx(
            tmp_path,
            services={"web": ServiceSpec(name="web", cpu=256, memory=512)},
        )
        ctx.provider_config["ecs"]["default_launch_type"] = "FARGATE"
        self._provider(ecs).preflight(ctx)
        assert ecs.calls == []
        assert ctx.eni_trunking is None

    def test_explicit_config_skips_the_call_entirely(self, tmp_path):
        ecs = _ECS()
        ctx = _ctx(tmp_path, ec2_capacity={"eni_trunking": True})
        self._provider(ecs).preflight(ctx)
        assert ctx.eni_trunking is True
        assert ecs.calls == []

    def test_bad_knob_value_is_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, ec2_capacity={"eni_trunking": "yes-please"})
        with pytest.raises(ProviderConfigError, match="eni_trunking"):
            self._provider(_ECS()).preflight(ctx)


class TestEndToEnd348:
    def test_rejected_when_trunking_is_off(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            ec2_capacity={
                "instance_type": "m5.xlarge",
                "min": 2,
                "desired": 3,
                "max": 6,
            },
        )
        ctx.eni_trunking = False
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        assert "disabled in us-west-2" in str(exc.value)

    def test_accepted_once_trunking_is_on(self, tmp_path):
        """The #348 config: 11 tasks, m5.xlarge x3."""
        ctx = _ctx(
            tmp_path,
            ec2_capacity={
                "instance_type": "m5.xlarge",
                "min": 2,
                "desired": 3,
                "max": 6,
            },
        )
        ctx.eni_trunking = True
        ECSProvider().emit_terraform(ctx, tmp_path / "tf")  # must not raise

    def test_asserting_trunking_on_an_ineligible_shape_is_rejected(self, tmp_path):
        """Ask 2: taking the operator's word here would size against a
        ceiling that does not exist and leave tasks PENDING forever."""
        ctx = _ctx(
            tmp_path,
            ec2_capacity={
                "instance_type": "t3.xlarge",
                "eni_trunking": True,
                "desired": 6,
                "max": 8,
            },
        )
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider().preflight(ctx)
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        assert "does not support ENI trunking on t3.xlarge" in str(exc.value)

    def test_eni_bound_fleet_names_trunking_as_the_lever(self, tmp_path):
        """Ask 4: 'you need 4 instances' vs 'you need 4 instances because of a
        networking limit you can lift'."""
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            ec2_capacity={"instance_type": "m5.xlarge", "desired": 4, "max": 8},
        )
        ctx.eni_trunking = False
        provider.emit_terraform(ctx, tmp_path / "tf")
        [w] = [x for x in provider._warnings if "set by NETWORKING" in x]
        assert "20 task slot(s)" in w
        assert "PER-REGION" in w
        assert "--region us-west-2" in w

    def test_no_networking_warning_once_trunking_applies(self, tmp_path):
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            ec2_capacity={"instance_type": "m5.xlarge", "desired": 3, "max": 6},
        )
        ctx.eni_trunking = True
        provider.emit_terraform(ctx, tmp_path / "tf")
        assert not [x for x in provider._warnings if "set by NETWORKING" in x]
