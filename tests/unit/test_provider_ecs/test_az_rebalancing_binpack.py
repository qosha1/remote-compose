"""rc-5a4g: rc asked ECS for binpack while leaving AZ rebalancing ENABLED.

ECS rejects that combination, and the default is history-dependent rather
than config-dependent (API_CreateService, availabilityZoneRebalancing):

    CreateService with no value -> ECS defaults to ENABLED
    UpdateService with no value -> ECS KEEPS the service's existing value

rc rendered ordered_placement_strategy binpack for every EC2 service but
only pinned availability_zone_rebalancing = DISABLED inside the STATEFUL
branch, for the unrelated maximumPercent <= 100 conflict. The AWS provider
treats an unrendered Optional+Computed argument as "keep the live value", so
services first created on Fargate carried ENABLED into the EC2 move and
UpdateService 400'd.

Field evidence: 6 of 7 debuggai-api services failed exactly this way. The one
that migrated cleanly (celery-beat) did so only because it is a singleton
scheduler and had therefore already been pinned DISABLED by the stateful
branch. Same rc.yml, same apply, opposite outcomes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.plan_analysis import (
    detect_binpack_az_rebalancing_conflicts,
    render_binpack_conflict_warning,
)

_SERVICE_RE = re.compile(r'resource "aws_ecs_service" "(\w+)" \{(.*?)\n\}', re.S)
_AZR_RE = re.compile(r'^\s*availability_zone_rebalancing\s*=\s*"(\w+)"', re.M)


def _ctx(tmp_path: Path, services, default_launch_type="EC2") -> DeployContext:
    return DeployContext(
        project="azr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-2",
                "cluster": "c",
                "vpc_cidr": "10.0.0.0/16",
                "default_launch_type": default_launch_type,
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
    )


def _render(tmp_path, services, **kw) -> dict[str, str]:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, **kw), out)
    return {n: b for n, b in _SERVICE_RE.findall((out / "services.tf").read_text())}


class TestRenderedInvariant:
    """The durable guard: if rc emits binpack, rc owns this field."""

    def test_every_binpack_service_pins_rebalancing_off(self, tmp_path):
        blocks = _render(
            tmp_path,
            {
                "web": ServiceSpec(
                    name="web", cpu=256, memory=512, public=True, port=80
                ),
                "worker": ServiceSpec(name="worker", cpu=256, memory=512),
                # Singleton scheduler -> stateful via the name heuristic.
                "celery-beat": ServiceSpec(name="celery-beat", cpu=256, memory=512),
                # EFS mount -> stateful.
                "pg": ServiceSpec(
                    name="pg",
                    cpu=256,
                    memory=512,
                    volumes=[{"name": "data", "mount": "/v"}],
                ),
            },
        )
        assert blocks, "no services rendered"
        for name, body in blocks.items():
            assert "binpack" in body, name
            assert _AZR_RE.findall(body) == ["DISABLED"], name

    def test_exactly_one_assignment_even_for_stateful_ec2(self, tmp_path):
        """Stateful services already pin it for the maximumPercent conflict;
        a second assignment would be a duplicate argument terraform rejects."""
        blocks = _render(
            tmp_path,
            {
                "pg": ServiceSpec(
                    name="pg",
                    cpu=256,
                    memory=512,
                    volumes=[{"name": "data", "mount": "/v"}],
                )
            },
        )
        assert len(_AZR_RE.findall(blocks["pg"])) == 1

    def test_fargate_services_are_untouched(self, tmp_path):
        """AZ rebalancing is desirable on Fargate — rc must not disable it
        there just because it disables it for binpacked EC2 services."""
        blocks = _render(
            tmp_path,
            {"web": ServiceSpec(name="web", cpu=256, memory=512)},
            default_launch_type="FARGATE",
        )
        body = blocks["web"]
        assert "binpack" not in body
        assert _AZR_RE.findall(body) == []

    def test_mixed_stack_splits_correctly(self, tmp_path):
        blocks = _render(
            tmp_path,
            {
                "onec2": ServiceSpec(name="onec2", cpu=256, memory=512),
                "onfargate": ServiceSpec(
                    name="onfargate", cpu=256, memory=512, launch_type="FARGATE"
                ),
            },
        )
        assert _AZR_RE.findall(blocks["onec2"]) == ["DISABLED"]
        assert _AZR_RE.findall(blocks["onfargate"]) == []


def _plan(actions, before_azr=None, after_azr=None, binpack=True, name="django"):
    after: dict = {"name": name}
    if binpack:
        after["ordered_placement_strategy"] = [{"type": "binpack", "field": "memory"}]
    if after_azr:
        after["availability_zone_rebalancing"] = after_azr
    before: dict = {"name": name}
    if before_azr:
        before["availability_zone_rebalancing"] = before_azr
    return {
        "resource_changes": [
            {
                "address": f"aws_ecs_service.{name}",
                "type": "aws_ecs_service",
                "name": name,
                "change": {"actions": actions, "before": before, "after": after},
            }
        ]
    }


class TestPlanDetector:
    """Only reachable against live service state, so it belongs in the
    plan-time warning set rather than in config validation."""

    def test_flags_binpack_added_to_a_live_enabled_service(self):
        [c] = detect_binpack_az_rebalancing_conflicts(
            _plan(["update"], before_azr="ENABLED")
        )
        assert c.label == "django"
        text = render_binpack_conflict_warning([c])
        assert "UpdateService returns 400" in text
        assert "depends on LIVE service state" in text

    def test_silent_when_the_plan_also_disables_it(self):
        """What rc renders today — the detector must not cry wolf on it."""
        assert (
            detect_binpack_az_rebalancing_conflicts(
                _plan(["update"], before_azr="ENABLED", after_azr="DISABLED")
            )
            == []
        )

    def test_silent_when_live_service_is_already_disabled(self):
        """celery-beat's case: it migrated cleanly."""
        assert (
            detect_binpack_az_rebalancing_conflicts(
                _plan(["update"], before_azr="DISABLED")
            )
            == []
        )

    def test_create_with_no_value_is_a_conflict(self):
        """ECS defaults NEW services to ENABLED, so binpack + unset fails."""
        assert len(detect_binpack_az_rebalancing_conflicts(_plan(["create"]))) == 1

    def test_create_that_sets_disabled_is_fine(self):
        assert (
            detect_binpack_az_rebalancing_conflicts(
                _plan(["create"], after_azr="DISABLED")
            )
            == []
        )

    def test_no_binpack_no_conflict(self):
        assert (
            detect_binpack_az_rebalancing_conflicts(
                _plan(["update"], before_azr="ENABLED", binpack=False)
            )
            == []
        )

    @pytest.mark.parametrize("actions", [["no-op"], ["read"]])
    def test_unchanged_services_are_ignored(self, actions):
        assert (
            detect_binpack_az_rebalancing_conflicts(
                _plan(actions, before_azr="ENABLED")
            )
            == []
        )

    @pytest.mark.parametrize(
        "plan", [{}, {"resource_changes": None}, {"resource_changes": ["junk"]}]
    )
    def test_malformed_plan_does_not_raise(self, plan):
        assert detect_binpack_az_rebalancing_conflicts(plan) == []

    def test_empty_render_is_empty_string(self):
        assert render_binpack_conflict_warning([]) == ""


class TestPreapplyPlanGate:
    """An EC2 stack now gets the pre-apply plan too — the saved plan is
    reused as the apply's input, so it costs no extra terraform cycle."""

    def _ctx_for(self, tmp_path, launch_type, ignore=False):
        ctx = _ctx(
            tmp_path,
            {"api": ServiceSpec(name="api", cpu=256, memory=512)},
            default_launch_type=launch_type,
        )
        ctx.provider_config["ecs"]["ignore_task_definition_changes"] = ignore
        return ctx

    def test_ec2_stack_triggers_the_plan_inspection(self, tmp_path):
        assert (
            ECSProvider()._needs_preapply_plan(self._ctx_for(tmp_path, "EC2")) is True
        )

    def test_plain_fargate_stack_does_not(self, tmp_path):
        assert (
            ECSProvider()._needs_preapply_plan(self._ctx_for(tmp_path, "FARGATE"))
            is False
        )

    def test_fargate_stack_with_ignore_flag_still_does(self, tmp_path):
        assert (
            ECSProvider()._needs_preapply_plan(
                self._ctx_for(tmp_path, "FARGATE", ignore=True)
            )
            is True
        )

    def test_per_service_ec2_override_triggers_it(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"w": ServiceSpec(name="w", cpu=256, memory=512, launch_type="EC2")},
            default_launch_type="FARGATE",
        )
        assert ECSProvider()._needs_preapply_plan(ctx) is True
