"""`rc plan` must not let a regroup look like a routine deploy (rc-93ol).

Moving a live estate from N services to M groups is DESTRUCTIVE: the merged
members' ``aws_ecs_service`` resources are destroyed and their containers come
back inside a survivor's task. On the estate this epic was measured against that
means each tenant's postgres stops and restarts.

In terraform plan JSON that has a specific, detectable shape — several
``aws_ecs_service`` deletes alongside an ``aws_ecs_service`` that is NOT being
deleted — and it is worth telling apart from a normal deploy, where nothing is
deleted at all.
"""

from __future__ import annotations

import pytest

from remote_compose.provider.ecs.plan_analysis import (
    detect_task_group_regroup,
    render_regroup_warning,
)

pytestmark = pytest.mark.unit

SERVICE = "aws_ecs_service"
TASK_DEF = "aws_ecs_task_definition"


def _rc(type_: str, name: str, actions: list[str]) -> dict:
    return {
        "address": f"{type_}.{name}",
        "type": type_,
        "name": name,
        "change": {"actions": actions},
    }


def _plan(*changes: dict) -> dict:
    return {"resource_changes": list(changes)}


def _regroup_plan() -> dict:
    """nginx survives and gains containers; django/frontend/reingest go away."""
    return _plan(
        _rc(SERVICE, "nginx", ["update"]),
        _rc(TASK_DEF, "nginx", ["create", "delete"]),
        _rc(SERVICE, "django", ["delete"]),
        _rc(TASK_DEF, "django", ["delete"]),
        _rc(SERVICE, "frontend", ["delete"]),
        _rc(SERVICE, "reingest", ["delete"]),
    )


class TestNotARegroup:
    def test_an_empty_plan_is_not_a_regroup(self):
        assert detect_task_group_regroup({}) is None
        assert detect_task_group_regroup(_plan()) is None

    def test_a_routine_deploy_is_not_a_regroup(self):
        """New task-def revisions + service updates, nothing destroyed."""
        plan = _plan(
            _rc(SERVICE, "nginx", ["update"]),
            _rc(TASK_DEF, "nginx", ["create", "delete"]),
            _rc(SERVICE, "django", ["update"]),
            _rc(TASK_DEF, "django", ["create", "delete"]),
        )
        assert detect_task_group_regroup(plan) is None

    def test_a_first_apply_is_not_a_regroup(self):
        plan = _plan(
            _rc(SERVICE, "nginx", ["create"]),
            _rc(SERVICE, "django", ["create"]),
        )
        assert detect_task_group_regroup(plan) is None

    def test_a_full_teardown_is_not_a_regroup(self):
        """Every service destroyed and none surviving is a destroy, not a
        regroup — telling the operator to expect a merge would be wrong."""
        plan = _plan(
            _rc(SERVICE, "nginx", ["delete"]),
            _rc(SERVICE, "django", ["delete"]),
        )
        assert detect_task_group_regroup(plan) is None

    def test_removing_a_single_service_is_not_a_regroup(self):
        """One delete beside untouched services is a service removal. Reported
        elsewhere; calling it a regroup would be a false alarm."""
        plan = _plan(
            _rc(SERVICE, "nginx", ["no-op"]),
            _rc(SERVICE, "django", ["delete"]),
        )
        assert detect_task_group_regroup(plan) is None

    def test_malformed_plan_json_does_not_raise(self):
        for bad in ({"resource_changes": "junk"}, {"resource_changes": [None, 7]}):
            assert detect_task_group_regroup(bad) is None


class TestDetectsARegroup:
    def test_two_or_more_deletes_beside_a_survivor_is_a_regroup(self):
        found = detect_task_group_regroup(_regroup_plan())
        assert found is not None

    def test_it_names_the_destroyed_services(self):
        found = detect_task_group_regroup(_regroup_plan())
        assert found.destroyed == ["django", "frontend", "reingest"]

    def test_it_names_the_survivors(self):
        found = detect_task_group_regroup(_regroup_plan())
        assert found.surviving == ["nginx"]

    def test_a_created_service_counts_as_a_survivor(self):
        """A group named after NO member: every old service dies and a new
        group service is created."""
        plan = _plan(
            _rc(SERVICE, "data", ["create"]),
            _rc(SERVICE, "postgres", ["delete"]),
            _rc(SERVICE, "redis", ["delete"]),
        )
        found = detect_task_group_regroup(plan)
        assert found is not None
        assert found.destroyed == ["postgres", "redis"]
        assert found.surviving == ["data"]
        assert found.created == ["data"]

    def test_an_in_place_survivor_is_not_reported_as_created(self):
        found = detect_task_group_regroup(_regroup_plan())
        assert found.created == []
        assert found.updated == ["nginx"]

    def test_replacement_of_a_service_counts_as_destroyed(self):
        plan = _plan(
            _rc(SERVICE, "nginx", ["update"]),
            _rc(SERVICE, "django", ["delete", "create"]),
            _rc(SERVICE, "frontend", ["delete"]),
        )
        found = detect_task_group_regroup(plan)
        assert found is not None
        assert "django" in found.destroyed


class TestWarningText:
    def test_empty_when_there_is_no_regroup(self):
        assert render_regroup_warning(None) == ""

    def test_names_both_sides(self):
        msg = render_regroup_warning(detect_task_group_regroup(_regroup_plan()))
        for name in ("django", "frontend", "reingest", "nginx"):
            assert name in msg

    def test_says_the_destroyed_services_stop(self):
        msg = render_regroup_warning(detect_task_group_regroup(_regroup_plan()))
        assert "not a routine deploy" in msg.lower()
        assert "stop" in msg.lower()

    def test_says_the_survivor_rolls_in_place(self):
        msg = render_regroup_warning(detect_task_group_regroup(_regroup_plan()))
        assert "in place" in msg.lower()

    def test_warns_that_this_has_not_been_field_verified(self):
        """rc-ero's caveat applies here: no grouped stack has been applied
        against real AWS yet, and a runbook that hides that is worse than none."""
        msg = render_regroup_warning(detect_task_group_regroup(_regroup_plan()))
        assert "efs" in msg.lower()

    def test_mentions_taking_a_backup_first(self):
        msg = render_regroup_warning(detect_task_group_regroup(_regroup_plan()))
        assert "backup" in msg.lower()


class TestWiredIntoThePlanPath:
    def test_inspect_plan_surfaces_the_regroup_warning(self, tmp_path):
        """_inspect_plan is where every live-state detector runs, so a regroup
        reaches the operator through the same channel as the others."""
        from unittest import mock

        from remote_compose.provider.ecs import ECSProvider

        runner = mock.MagicMock()
        runner.show_json.return_value = _regroup_plan()
        provider = ECSProvider()
        warned: list[str] = []
        provider._warn = lambda m: warned.append(m)
        provider._warn_on_task_def_replacement = lambda r, p: []

        messages = provider._inspect_plan(runner, tmp_path / "p.tfplan")
        assert any("NOT a routine deploy" in m for m in messages)
        assert warned == messages

    def test_a_routine_plan_produces_no_regroup_warning(self, tmp_path):
        from unittest import mock

        from remote_compose.provider.ecs import ECSProvider

        runner = mock.MagicMock()
        runner.show_json.return_value = _plan(
            _rc(SERVICE, "nginx", ["update"]),
            _rc(TASK_DEF, "nginx", ["create", "delete"]),
        )
        provider = ECSProvider()
        provider._warn = lambda m: None
        provider._warn_on_task_def_replacement = lambda r, p: []
        assert provider._inspect_plan(runner, tmp_path / "p.tfplan") == []
