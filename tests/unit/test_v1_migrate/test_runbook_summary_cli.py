"""Phase 4.2 unit tests: runbook, MIGRATION_SUMMARY.md rendering, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.v1_migrate.discover import V1Stack, ResourceInventory
from remote_compose.v1_migrate.plan import build_plan
from remote_compose.v1_migrate.runbook import (
    RunbookEntry,
    find_undo_for_phase,
    format_undo_runbook,
    write_runbook_json,
)


FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "v1_migrate"
V1_RC_YML = FIXTURES / "ss-debuggai-prod.rc.yml"
INVENTORY_JSON = FIXTURES / "inventory.json"


@pytest.fixture
def plan():
    stack = V1Stack.from_yaml(V1_RC_YML)
    inv = ResourceInventory.from_json(INVENTORY_JSON)
    return build_plan(stack, inv)


# ---------------------------------------------------------------------
# RunbookEntry + write_runbook_json
# ---------------------------------------------------------------------

class TestRunbook:
    def test_begin_finish_lifecycle(self):
        e = RunbookEntry.begin("import_state", undo_command="cp bak orig")
        assert e.ok is False
        assert e.finished_at is None
        e.finish(ok=True, details="6 imports applied")
        assert e.ok is True
        assert e.finished_at is not None
        assert "6 imports" in e.details

    def test_write_runbook_json_roundtrip(self, tmp_path):
        e1 = RunbookEntry.begin("validate", undo_command="")
        e1.finish(ok=True, details="no drift")
        e2 = RunbookEntry.begin("import_state", undo_command="cp bak orig")
        e2.finish(ok=False, details="terraform plan failed")
        path = tmp_path / "runbook.json"
        write_runbook_json([e1, e2], path)
        loaded = json.loads(path.read_text())
        assert len(loaded) == 2
        assert loaded[0]["phase"] == "validate"
        assert loaded[1]["ok"] is False

    def test_find_undo_for_phase(self):
        entries = [
            RunbookEntry(phase="validate", started_at="t0", finished_at="t1",
                         ok=True, undo_command="", details="ok"),
            RunbookEntry(phase="import_state", started_at="t2", finished_at="t3",
                         ok=True, undo_command="cp bak orig", details="ok"),
        ]
        assert find_undo_for_phase(entries, "import_state") == "cp bak orig"
        assert find_undo_for_phase(entries, "validate") is None  # empty undo
        assert find_undo_for_phase(entries, "missing") is None

    def test_format_undo_runbook_reverse_order(self):
        entries = [
            RunbookEntry(phase="emit_v2_terraform", started_at="t0",
                         finished_at="t1", ok=True,
                         undo_command="rm -rf out", details="ok"),
            RunbookEntry(phase="import_state", started_at="t2",
                         finished_at="t3", ok=True,
                         undo_command="cp bak orig", details="ok"),
        ]
        out = format_undo_runbook(entries)
        # Reverse order: import_state should appear before emit_v2_terraform
        assert out.index("import_state") < out.index("emit_v2_terraform")


# ---------------------------------------------------------------------
# MigrationPlan.render_summary_md
# ---------------------------------------------------------------------

class TestRenderSummaryMd:
    def test_summary_includes_all_sections(self, plan):
        md = plan.render_summary_md()
        assert "# Migration Plan" in md
        assert "## Blast radius" in md
        assert "## Terraform imports" in md
        assert "## Secrets (referenced by ARN" in md
        assert "## External IAM" in md
        assert "## ECR repositories" in md
        assert "## Phases (with undo commands)" in md

    def test_blast_radius_values_present(self, plan):
        md = plan.render_summary_md()
        # Prod fixture: 7 running tasks, 15 secrets in fixture, dns external
        assert "running_tasks**: 7" in md
        assert "secrets_count**: 15" in md
        assert "dns_managed_externally**: True" in md

    def test_live_postgres_mount_in_imports_section(self, plan):
        md = plan.render_summary_md()
        assert "fsap-004097e867c7bb755" in md

    def test_secrets_show_full_arns(self, plan):
        md = plan.render_summary_md()
        assert (
            "arn:aws:secretsmanager:us-west-2:033937118837:secret:"
            "ss-debuggai-prod/POSTGRES_PASSWORD"
        ) in md

    def test_phase_undo_commands_included(self, plan):
        md = plan.render_summary_md()
        assert "cp live.tfstate.bak live.tfstate" in md
        assert "rm -rf terraform/v2-generated/" in md


# ---------------------------------------------------------------------
# CLI smoke — `rc v1 migrate plan`
# ---------------------------------------------------------------------

class TestCliPlan:
    def test_plan_against_inventory_snapshot(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        result = runner.invoke(cli, [
            "v1", "migrate", "plan",
            str(V1_RC_YML),
            "--inventory-snapshot", str(INVENTORY_JSON),
            "--out", str(out_dir),
        ])
        assert result.exit_code == 0, result.output
        assert (out_dir / "main.tf").exists()
        assert (out_dir / "imports.tf").exists()
        assert (out_dir / "rc.yml.v2").exists()
        assert (out_dir / "MIGRATION_SUMMARY.md").exists()
        assert (out_dir / "runbook.json").exists()
        # Runbook seeded with 5 phase descriptors.
        seeded = json.loads((out_dir / "runbook.json").read_text())
        assert len(seeded) == 5
        names = [e["phase"] for e in seeded]
        assert names == [
            "validate", "emit_v2_terraform", "import_state",
            "services_cutover", "decommission_v1",
        ]

    def test_plan_refuses_overwrite_without_force(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "rc.yml.v2").write_text("# stale")
        result = runner.invoke(cli, [
            "v1", "migrate", "plan",
            str(V1_RC_YML),
            "--inventory-snapshot", str(INVENTORY_JSON),
            "--out", str(out_dir),
        ])
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output.lower()

    def test_plan_force_overwrites(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "rc.yml.v2").write_text("# stale")
        result = runner.invoke(cli, [
            "v1", "migrate", "plan",
            str(V1_RC_YML),
            "--inventory-snapshot", str(INVENTORY_JSON),
            "--out", str(out_dir),
            "--force",
        ])
        assert result.exit_code == 0, result.output
        # Stale content replaced with v2 content.
        assert "# stale" not in (out_dir / "rc.yml.v2").read_text()


# ---------------------------------------------------------------------
# CLI smoke — `rc v1 migrate apply`
# ---------------------------------------------------------------------

class TestCliApply:
    def test_apply_refuses_without_sandbox_tfstate(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "v1", "migrate", "apply",
            str(V1_RC_YML),
            "--out", str(tmp_path),
            "--inventory-snapshot", str(INVENTORY_JSON),
        ])
        # click reports the missing required option as exit code 2.
        assert result.exit_code == 2
        assert "sandbox" in result.output.lower()

    def test_apply_validate_phase_only(self, tmp_path):
        runner = CliRunner()
        sandbox = tmp_path / "tfstate.copy"
        sandbox.write_text(
            '{"version": 4, "terraform_version": "1.6.0", "resources": []}'
        )
        # Pre-create out_dir since it must exist.
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = runner.invoke(cli, [
            "v1", "migrate", "apply",
            str(V1_RC_YML),
            "--out", str(out_dir),
            "--inventory-snapshot", str(INVENTORY_JSON),
            "--sandbox-tfstate", str(sandbox),
            "--phase", "validate",
            "--auto-approve",
        ])
        assert result.exit_code == 0, result.output
        assert "[validate] OK" in result.output
        assert (out_dir / "runbook.json").exists()
