"""Tests for `rc destroy --all-ephemeral` (rc-e5u.44.15).

Same machinery as `rc reap --all` but exposed via the destroy verb. Covers:
  - registry empty -> friendly no-op message, no prompt
  - registry has stacks -> single confirmation, sequenced provider.destroy,
    successes removed from registry, failures don't stop the run
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.ephemeral import EphemeralRecord


@pytest.fixture
def runner():
    return CliRunner()


def _make_record(
    project: str,
    region: str = "us-west-1",
    rc_yml_path: str = "/tmp/x.yml",
    terraform_dir: str = "/tmp/tf",
) -> EphemeralRecord:
    return EphemeralRecord(
        project=project,
        region=region,
        aws_profile="default",
        expires_at="2999-01-01T00:00:00Z",  # not expired
        rc_yml_path=rc_yml_path,
        terraform_dir=terraform_dir,
        created_at="2026-04-25T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Empty registry: no-op + clear message
# ---------------------------------------------------------------------------


def test_empty_registry_prints_and_exits(runner):
    with patch("remote_compose.ephemeral.list_records", return_value=[]):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral"])
    assert result.exit_code == 0
    assert "No ephemeral stacks in registry" in result.output


def test_no_confirmation_prompt_on_empty_registry(runner):
    """Don't prompt if there's nothing to do — would be confusing UX."""
    with patch("remote_compose.ephemeral.list_records", return_value=[]):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral"], input="n\n")
    assert "Destroy these" not in result.output


# ---------------------------------------------------------------------------
# Single confirmation prompt covers every stack in registry
# ---------------------------------------------------------------------------


def test_lists_all_stacks_before_prompting(runner):
    records = [_make_record("proj-a"), _make_record("proj-b", region="us-east-2")]
    with patch("remote_compose.ephemeral.list_records", return_value=records):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral"], input="n\n")
    assert "proj-a" in result.output
    assert "proj-b" in result.output
    assert "us-west-1" in result.output
    assert "us-east-2" in result.output
    # Exactly ONE prompt at the bottom — not one per stack
    assert result.output.count("Destroy these") == 1


def test_decline_aborts(runner):
    records = [_make_record("proj-a")]
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.cli_v2.load_rc_yml") as load,
    ):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral"], input="n\n")
    # User said no → load_rc_yml never called
    load.assert_not_called()
    assert "aborted" in result.output.lower()


def test_yes_flag_skips_prompt(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("version: 2\nproject: proj-a\n")
    records = [_make_record("proj-a", rc_yml_path=str(rc_yml))]
    fake_v2 = MagicMock()
    fake_v2.project = "proj-a"
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch(
            "remote_compose.cli_v2.load_rc_yml", return_value=(2, {}, fake_v2)
        ) as load,
        patch("remote_compose.cli_v2.build_deploy_context"),
        patch("remote_compose.cli_v2.resolve_provider") as rp,
    ):
        provider = MagicMock()
        rp.return_value = provider
        result = runner.invoke(cli, ["destroy", "--all-ephemeral", "--yes"])
    assert result.exit_code == 0, result.output
    assert load.called
    provider.destroy.assert_called_once()
    rm.assert_called_once_with(project="proj-a", region="us-west-1")


# ---------------------------------------------------------------------------
# Failures on one stack don't stop the rest
# ---------------------------------------------------------------------------


def test_failure_on_one_does_not_stop_others(runner, tmp_path):
    rc_a = tmp_path / "a.yml"
    rc_a.write_text("version: 2\nproject: proj-a\n")
    rc_b = tmp_path / "b.yml"
    rc_b.write_text("version: 2\nproject: proj-b\n")
    records = [
        _make_record("proj-a", rc_yml_path=str(rc_a)),
        _make_record("proj-b", rc_yml_path=str(rc_b)),
    ]
    fake_a = MagicMock()
    fake_a.project = "proj-a"
    fake_b = MagicMock()
    fake_b.project = "proj-b"

    # provider.destroy fails for proj-a, succeeds for proj-b
    provider = MagicMock()

    def destroy_side(ctx):
        if getattr(ctx, "_proj", None) == "proj-a":
            raise RuntimeError("simulated AWS error")

    provider.destroy.side_effect = destroy_side

    def bdc_side(v2, raw, path):
        m = MagicMock()
        m._proj = v2.project
        return m

    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch(
            "remote_compose.cli_v2.load_rc_yml",
            side_effect=lambda p: (2, {}, fake_a if "a.yml" in str(p) else fake_b),
        ),
        patch("remote_compose.cli_v2.build_deploy_context", side_effect=bdc_side),
        patch("remote_compose.cli_v2.resolve_provider", return_value=provider),
    ):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral", "--yes"])

    # Both destroy attempts happened
    assert provider.destroy.call_count == 2
    # Only the succeeded one (proj-b) was removed from the registry
    rm.assert_called_once_with(project="proj-b", region="us-west-1")
    # Process exited non-zero because at least one failed
    assert result.exit_code != 0
    assert "failed" in result.output.lower()


# ---------------------------------------------------------------------------
# Missing rc.yml -> registry entry left in place, error reported
# ---------------------------------------------------------------------------


def test_missing_files_falls_back_to_audit_clean_removes_entry(runner, tmp_path):
    """rc-b9z: rc.yml + tf_dir both missing → fall back to AWS audit.
    Audit clean → registry entry removed, exit code 0."""
    from remote_compose.audit import AuditReport

    records = [
        _make_record(
            "proj-a",
            rc_yml_path=str(tmp_path / "does-not-exist.yml"),
            terraform_dir=str(tmp_path / "also-does-not-exist"),
        )
    ]
    clean_report = AuditReport(project="proj-a", region="us-west-1", findings=[])
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch("remote_compose.cli_v2.load_rc_yml") as load,
        patch("remote_compose.audit.audit_project", return_value=clean_report) as audit,
        patch("boto3.Session"),
    ):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral", "--yes"])
    load.assert_not_called()
    audit.assert_called_once()
    rm.assert_called_once_with(project="proj-a", region="us-west-1")
    assert result.exit_code == 0
    output = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "audit clean" in output


def test_missing_files_falls_back_to_audit_dirty_keeps_entry(runner, tmp_path):
    """rc-b9z: rc.yml + tf_dir both missing → audit fallback finds leftovers.
    Registry entry stays, exit code non-zero, manual cleanup hint printed."""
    from remote_compose.audit import AuditReport, AuditFinding

    records = [
        _make_record(
            "proj-a",
            rc_yml_path=str(tmp_path / "does-not-exist.yml"),
            terraform_dir=str(tmp_path / "also-does-not-exist"),
        )
    ]
    dirty_report = AuditReport(
        project="proj-a",
        region="us-west-1",
        findings=[AuditFinding(resource_type="log_group", identifier="/aws/leftover")],
    )
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch("remote_compose.cli_v2.load_rc_yml") as load,
        patch("remote_compose.audit.audit_project", return_value=dirty_report),
        patch("boto3.Session"),
    ):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral", "--yes"])
    load.assert_not_called()
    rm.assert_not_called()
    assert result.exit_code != 0
    output = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "1 leftover" in output
    assert "rc audit --project proj-a" in output


# ---------------------------------------------------------------------------
# rc-e5u.46.8: rc.yml missing but terraform_dir exists → fallback path
# ---------------------------------------------------------------------------


def test_rc_yml_missing_falls_back_to_terraform_dir(runner, tmp_path):
    """When rc.yml is gone but terraform_dir is intact (stale registry +
    state on disk), fall back to running terraform destroy directly. The
    AWS resources get cleaned up; the registry entry gets removed."""
    tf_dir = tmp_path / "terraform-module"
    tf_dir.mkdir()
    # No rc.yml file present at the registered path.
    records = [
        _make_record(
            "proj-a",
            rc_yml_path=str(tmp_path / "deleted.yml"),
            terraform_dir=str(tf_dir),
        )
    ]
    runner_instance = MagicMock()
    runner_instance.init.return_value = None
    runner_instance.destroy.return_value = None
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch(
            "remote_compose.terraform.runner.TerraformRunner",
            return_value=runner_instance,
        ) as tf_cls,
    ):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral", "--yes"])

    # Assert TerraformRunner was constructed for the right dir + destroy ran.
    assert tf_cls.called, result.output
    runner_instance.init.assert_called_once()
    runner_instance.destroy.assert_called_once()
    # Registry entry removed on success.
    rm.assert_called_once_with(project="proj-a", region="us-west-1")
    assert result.exit_code == 0, result.output
    assert "terraform_dir fallback" in result.output


def test_terraform_destroy_fallback_failure_keeps_registry_entry(runner, tmp_path):
    """If terraform destroy errors during the fallback, leave the registry
    entry in place + non-zero exit code, like the provider.destroy path."""
    from remote_compose.terraform.runner import TerraformError

    tf_dir = tmp_path / "terraform-module"
    tf_dir.mkdir()
    records = [
        _make_record(
            "proj-a",
            rc_yml_path=str(tmp_path / "deleted.yml"),
            terraform_dir=str(tf_dir),
        )
    ]
    runner_instance = MagicMock()
    runner_instance.init.return_value = None
    runner_instance.destroy.side_effect = TerraformError(
        cmd=["terraform", "destroy"],
        returncode=1,
        stdout="",
        stderr="aws denied",
    )
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch(
            "remote_compose.terraform.runner.TerraformRunner",
            return_value=runner_instance,
        ),
    ):
        result = runner.invoke(cli, ["destroy", "--all-ephemeral", "--yes"])

    rm.assert_not_called()
    assert result.exit_code != 0
    assert "FAILED" in result.output


# ---------------------------------------------------------------------------
# rc reap --all still works (regression: shared helper)
# ---------------------------------------------------------------------------


def test_rc_reap_all_still_works_via_shared_helper(runner, tmp_path):
    """The .44.15 refactor extracted _destroy_ephemeral_targets; verify
    rc reap --all still uses the same flow + same outcome shape."""
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("version: 2\nproject: proj-a\n")
    records = [_make_record("proj-a", rc_yml_path=str(rc_yml))]
    fake_v2 = MagicMock()
    fake_v2.project = "proj-a"
    with (
        patch("remote_compose.ephemeral.list_records", return_value=records),
        patch("remote_compose.ephemeral.remove_stack") as rm,
        patch("remote_compose.cli_v2.load_rc_yml", return_value=(2, {}, fake_v2)),
        patch("remote_compose.cli_v2.build_deploy_context"),
        patch("remote_compose.cli_v2.resolve_provider") as rp,
    ):
        rp.return_value = MagicMock()
        result = runner.invoke(cli, ["reap", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    rm.assert_called_once_with(project="proj-a", region="us-west-1")
