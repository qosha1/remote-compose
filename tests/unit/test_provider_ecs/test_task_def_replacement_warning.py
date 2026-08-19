"""rc-avcr: ignore_task_definition_changes fails open on a FORCED replacement.

The flag's whole promise is "terraform is not the source of truth for these
task definitions' container definitions". `lifecycle ignore_changes`
delivers that for UPDATES and cannot deliver it for REPLACEMENTS — changing
launch_type flips requires_compatibilities FARGATE -> EC2, which is ForceNew,
and the rendered replacement carries none of the secrets a reconcile script
wired onto the live revision.

These tests cover the detector (pure, over a plan-JSON fixture) and the
wiring that runs it before `terraform apply`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.plan_analysis import (
    detect_task_definition_replacements,
    render_replacement_warning,
)


def _containers(secret_names, env_names=(), container="django") -> str:
    return json.dumps(
        [
            {
                "name": container,
                "image": "x:latest",
                "secrets": [
                    {"name": n, "valueFrom": f"arn:...:{n}"} for n in secret_names
                ],
                "environment": [{"name": n, "value": "v"} for n in env_names],
            }
        ]
    )


def _change(
    *,
    actions,
    before_secrets=(),
    after_secrets=(),
    before_env=(),
    after_env=(),
    family="app-django",
    replace_paths=None,
    after_unknown=None,
    address="aws_ecs_task_definition.django",
):
    change = {
        "actions": list(actions),
        "before": {
            "family": family,
            "container_definitions": _containers(before_secrets, before_env),
        },
        "after": {
            "family": family,
            "container_definitions": _containers(after_secrets, after_env),
        },
    }
    if replace_paths is not None:
        change["replace_paths"] = replace_paths
    if after_unknown is not None:
        change["after_unknown"] = after_unknown
    return {
        "address": address,
        "type": "aws_ecs_task_definition",
        "name": address.split(".")[-1],
        "change": change,
    }


class TestDetection:
    def test_replacement_reports_every_dropped_secret(self):
        """The debuggai-api case: 38 SM secrets on the live revision, none on
        the rendered replacement."""
        live = [f"SECRET_{i}" for i in range(38)]
        plan = {
            "resource_changes": [
                _change(
                    actions=["delete", "create"],
                    before_secrets=live,
                    after_secrets=[],
                    replace_paths=[["requires_compatibilities"]],
                )
            ]
        }
        [found] = detect_task_definition_replacements(plan)
        assert len(found.dropped_secrets) == 38
        assert found.dropped_secrets[0].startswith("django.SECRET_")
        assert found.forced_by == ["requires_compatibilities"]
        assert found.is_lossy is True

    def test_create_before_destroy_order_is_also_a_replacement(self):
        plan = {
            "resource_changes": [
                _change(
                    actions=["create", "delete"],
                    before_secrets=["A"],
                    after_secrets=[],
                )
            ]
        }
        assert len(detect_task_definition_replacements(plan)) == 1

    @pytest.mark.parametrize(
        "actions", [["update"], ["create"], ["delete"], ["no-op"], []]
    )
    def test_non_replacements_are_ignored(self, actions):
        """An in-place update is exactly what ignore_changes already
        suppresses — reporting it would be noise."""
        plan = {
            "resource_changes": [
                _change(actions=actions, before_secrets=["A"], after_secrets=[])
            ]
        }
        assert detect_task_definition_replacements(plan) == []

    def test_non_task_definition_replacements_are_ignored(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_ecs_service.django",
                    "type": "aws_ecs_service",
                    "change": {"actions": ["delete", "create"], "before": {}},
                }
            ]
        }
        assert detect_task_definition_replacements(plan) == []

    def test_replacement_that_keeps_everything_is_not_lossy(self):
        plan = {
            "resource_changes": [
                _change(
                    actions=["delete", "create"],
                    before_secrets=["A", "B"],
                    after_secrets=["A", "B"],
                )
            ]
        }
        [found] = detect_task_definition_replacements(plan)
        assert found.dropped_secrets == []
        assert found.is_lossy is False
        assert render_replacement_warning([found]) == ""

    def test_dropped_env_vars_count_too(self):
        plan = {
            "resource_changes": [
                _change(
                    actions=["delete", "create"],
                    before_env=["DJANGO_SETTINGS", "EXTRA"],
                    after_env=["DJANGO_SETTINGS"],
                )
            ]
        }
        [found] = detect_task_definition_replacements(plan)
        assert found.dropped_env == ["django.EXTRA"]
        assert found.is_lossy is True

    def test_secrets_are_qualified_by_container(self):
        """Two containers can carry the same env name; losing one matters."""
        before = json.dumps(
            [
                {"name": "django", "secrets": [{"name": "DB_URL"}]},
                {"name": "nginx", "secrets": [{"name": "DB_URL"}]},
            ]
        )
        after = json.dumps([{"name": "django", "secrets": [{"name": "DB_URL"}]}])
        plan = {
            "resource_changes": [
                {
                    "address": "aws_ecs_task_definition.web",
                    "type": "aws_ecs_task_definition",
                    "change": {
                        "actions": ["delete", "create"],
                        "before": {"family": "web", "container_definitions": before},
                        "after": {"family": "web", "container_definitions": after},
                    },
                }
            ]
        }
        [found] = detect_task_definition_replacements(plan)
        assert found.dropped_secrets == ["nginx.DB_URL"]

    def test_unknown_rendered_side_is_flagged_as_a_floor(self):
        plan = {
            "resource_changes": [
                _change(
                    actions=["delete", "create"],
                    before_secrets=["A"],
                    after_secrets=["A"],
                    after_unknown={"container_definitions": True},
                )
            ]
        }
        [found] = detect_task_definition_replacements(plan)
        assert found.after_unknown is True
        assert found.is_lossy is True
        assert "floor" in render_replacement_warning([found])

    @pytest.mark.parametrize(
        "plan", [{}, {"resource_changes": None}, {"resource_changes": ["junk"]}]
    )
    def test_malformed_plan_yields_nothing_rather_than_raising(self, plan):
        assert detect_task_definition_replacements(plan) == []

    def test_unparseable_container_definitions_does_not_raise(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_ecs_task_definition.x",
                    "type": "aws_ecs_task_definition",
                    "change": {
                        "actions": ["delete", "create"],
                        "before": {"container_definitions": "not json{"},
                        "after": None,
                    },
                }
            ]
        }
        [found] = detect_task_definition_replacements(plan)
        assert found.dropped_secrets == []


class TestWarningText:
    def test_names_service_count_and_forcing_attribute(self):
        plan = {
            "resource_changes": [
                _change(
                    actions=["delete", "create"],
                    before_secrets=[f"S{i}" for i in range(38)],
                    after_secrets=[],
                    family="debuggai-api-celery-beat",
                    replace_paths=[["requires_compatibilities"]],
                )
            ]
        }
        text = render_replacement_warning(detect_task_definition_replacements(plan))
        assert "debuggai-api-celery-beat" in text
        assert "38 secret(s)" in text
        assert "requires_compatibilities" in text
        assert "ignore_changes cannot suppress a replacement" in text

    def test_empty_for_no_replacements(self):
        assert render_replacement_warning([]) == ""


class _FakeRunner:
    """Records terraform calls; serves a scripted plan JSON from show_json."""

    def __init__(self, plan_json=None, show_raises=None):
        self.plan_json = plan_json or {}
        self.show_raises = show_raises
        self.calls: list[tuple] = []

    def init(self):
        self.calls.append(("init",))

    def plan(self, out_file=None):
        self.calls.append(("plan", out_file))
        from remote_compose.terraform.runner import PlanSummary

        return PlanSummary(create=0, update=1, destroy=0, raw="")

    def show_json(self, plan_file):
        self.calls.append(("show_json", plan_file))
        if self.show_raises:
            raise self.show_raises
        return self.plan_json

    def apply(self, plan_file=None, auto_approve=True):
        self.calls.append(("apply", plan_file))

    def output(self, name=None):
        return {}

    def import_resource(self, address, resource_id):
        self.calls.append(("import", address))


def _ctx(tmp_path: Path, ignore=True) -> DeployContext:
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "c",
                "vpc_cidr": "10.0.0.0/16",
                "ignore_task_definition_changes": ignore,
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={"django": ServiceSpec(name="django", cpu=256, memory=512)},
    )


_LOSSY_PLAN = {
    "resource_changes": [
        _change(
            actions=["delete", "create"],
            before_secrets=["DATABASE_URL", "SECRET_KEY"],
            after_secrets=[],
            replace_paths=[["requires_compatibilities"]],
        )
    ]
}


class TestPlanWiring:
    def test_plan_saves_and_inspects_the_plan_when_flag_is_on(self, tmp_path):
        runner = _FakeRunner(_LOSSY_PLAN)
        provider = ECSProvider(runner_factory=lambda _d: runner)
        result = provider.plan(_ctx(tmp_path, ignore=True))

        assert any(c[0] == "show_json" for c in runner.calls)
        assert any("DATABASE_URL" in w for w in result.warnings)

    def test_plan_does_no_extra_work_when_flag_is_off(self, tmp_path):
        runner = _FakeRunner(_LOSSY_PLAN)
        provider = ECSProvider(runner_factory=lambda _d: runner)
        result = provider.plan(_ctx(tmp_path, ignore=False))

        assert not any(c[0] == "show_json" for c in runner.calls)
        assert ("plan", None) in runner.calls
        assert not any("ignore_task_definition_changes" in w for w in result.warnings)


class TestDeployWiring:
    def test_deploy_warns_before_apply_and_reuses_the_plan(self, tmp_path, monkeypatch):
        runner = _FakeRunner(_LOSSY_PLAN)
        emitted: list[str] = []
        provider = ECSProvider(
            runner_factory=lambda _d: runner, progress=emitted.append
        )
        monkeypatch.setattr(provider, "_build_and_push_images", lambda *a, **k: [])

        result = provider.deploy(_ctx(tmp_path, ignore=True))

        order = [c[0] for c in runner.calls]
        assert order.index("show_json") < order.index("apply")
        # The apply consumes the very plan that was inspected, so the warning
        # describes exactly what ran.
        apply_call = next(c for c in runner.calls if c[0] == "apply")
        plan_call = next(c for c in runner.calls if c[0] == "plan")
        assert apply_call[1] is not None and apply_call[1] == plan_call[1]
        assert any("DATABASE_URL" in w for w in result.warnings)
        # And it reached the terminal before terraform touched anything.
        assert any("DATABASE_URL" in line for line in emitted)

    def test_deploy_unaffected_when_flag_is_off(self, tmp_path, monkeypatch):
        runner = _FakeRunner(_LOSSY_PLAN)
        provider = ECSProvider(runner_factory=lambda _d: runner)
        monkeypatch.setattr(provider, "_build_and_push_images", lambda *a, **k: [])

        provider.deploy(_ctx(tmp_path, ignore=False))

        assert ("apply", None) in runner.calls
        assert not any(c[0] == "show_json" for c in runner.calls)

    def test_unreadable_plan_warns_but_does_not_break_the_deploy(
        self, tmp_path, monkeypatch
    ):
        runner = _FakeRunner(show_raises=RuntimeError("terraform show blew up"))
        provider = ECSProvider(runner_factory=lambda _d: runner)
        monkeypatch.setattr(provider, "_build_and_push_images", lambda *a, **k: [])

        result = provider.deploy(_ctx(tmp_path, ignore=True))

        assert any("could not read the terraform plan" in w for w in result.warnings)
        assert any(c[0] == "apply" for c in runner.calls)
