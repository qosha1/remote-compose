"""Unit tests for remote_compose.terraform.runner.

Runner invocations are exercised via ``RecordingTerraformRunner`` (no real
terraform subprocess). The real-terraform code path is covered by the
integration tier (tests/integration/).
"""

from __future__ import annotations

import pytest

from remote_compose.terraform.runner import (
    PlanSummary,
    RecordingTerraformRunner,
    TerraformRunner,
    _parse_plan_summary,
)


@pytest.fixture
def runner(tmp_path):
    return RecordingTerraformRunner(working_dir=tmp_path)


class TestRecordingRunner:
    def test_init_records_call(self, runner):
        runner.init()
        assert runner.calls[0].args[:2] == ["init", "-input=false"]

    def test_init_without_backend(self, runner):
        runner.init(backend=False)
        assert "-backend=false" in runner.calls[0].args

    def test_init_with_upgrade(self, runner):
        runner.init(upgrade=True)
        assert "-upgrade" in runner.calls[0].args

    def test_validate(self, runner):
        runner.validate()
        assert runner.calls[0].args == ["validate"]

    def test_plan_returns_summary(self, runner):
        runner.script("plan", "Plan: 3 to add, 1 to change, 0 to destroy.\n")
        summary = runner.plan()
        assert isinstance(summary, PlanSummary)
        assert summary.create == 3
        assert summary.update == 1
        assert summary.destroy == 0

    def test_plan_no_changes(self, runner):
        runner.script("plan", "No changes. Your configuration ...")
        summary = runner.plan()
        assert summary.create == summary.update == summary.destroy == 0

    def test_apply_with_auto_approve(self, runner):
        runner.apply()
        assert "-auto-approve" in runner.calls[0].args

    def test_apply_with_plan_file(self, runner, tmp_path):
        plan = tmp_path / "plan.out"
        runner.apply(plan_file=plan)
        args = runner.calls[0].args
        assert str(plan) in args
        assert "-auto-approve" not in args

    def test_destroy_with_auto_approve(self, runner):
        runner.destroy()
        assert "-auto-approve" in runner.calls[0].args

    def test_output_returns_parsed_json(self, runner):
        runner.script("output", '{"foo": {"value": "bar"}}')
        result = runner.output()
        assert result == {"foo": {"value": "bar"}}

    def test_output_empty_returns_empty_dict(self, runner):
        result = runner.output()
        assert result == {}

    def test_show_json_parses_a_saved_plan(self, runner, tmp_path):
        """rc-avcr reads resource_changes[] rather than grepping the human
        plan output, which is a rendering and not an interface."""
        runner.script(
            "show",
            '{"format_version": "1.2", "resource_changes": [{"type": "x"}]}',
        )
        result = runner.show_json(tmp_path / "p.tfplan")
        assert result["resource_changes"] == [{"type": "x"}]
        assert runner.calls[0].args[:2] == ["show", "-json"]
        assert str(tmp_path / "p.tfplan") in runner.calls[0].args

    def test_show_json_empty_returns_empty_dict(self, runner, tmp_path):
        assert runner.show_json(tmp_path / "p.tfplan") == {}

    def test_show_json_does_not_stream_the_payload_to_progress(self, tmp_path):
        """A real stack's plan JSON is hundreds of KB on one line — streaming
        it would bury the output the user is reading."""
        seen: list[str] = []
        real = TerraformRunner(
            working_dir=tmp_path, terraform_bin="/bin/echo", progress=seen.append
        )
        # /bin/echo stands in for terraform: it prints its args back, so
        # anything that leaked into `seen` came from the output stream.
        assert real._run(["show", "-json"], quiet=True).strip() == "show -json"
        assert seen == ["$ terraform show -json"]

        loud: list[str] = []
        TerraformRunner(
            working_dir=tmp_path, terraform_bin="/bin/echo", progress=loud.append
        )._run(["plan"])
        assert "plan" in loud[1:]


class TestPlanSummaryParser:
    def test_standard_summary(self):
        s = _parse_plan_summary("...\nPlan: 5 to add, 2 to change, 1 to destroy.\n")
        assert s.create == 5 and s.update == 2 and s.destroy == 1

    def test_no_changes_wording(self):
        s = _parse_plan_summary(
            "No changes. Your infrastructure matches the configuration."
        )
        assert s.create == s.update == s.destroy == 0

    def test_unknown_format_returns_zeros(self):
        s = _parse_plan_summary("something else entirely")
        assert s.create == s.update == s.destroy == 0
