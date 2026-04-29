"""Tests for `rc status` stale-revision detection + `rc deploy --reconcile`
(rc-e5u.44.24).

Covers:
  - ServiceStatus.is_stale property
  - render_status shows the revision column + STALE summary line
  - rc deploy --reconcile auto-discovers stale services + force-rolls them
  - --reconcile + --services / --tag is rejected (mutually exclusive)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.cli_v2 import render_status
from remote_compose.provider.base import ServiceStatus, StatusReport


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# ServiceStatus.is_stale
# ---------------------------------------------------------------------------

class TestIsStale:
    def test_running_lower_than_latest_is_stale(self):
        s = ServiceStatus(name="x", desired=1, running=1, health="healthy",
                          running_revision=2, latest_revision=3)
        assert s.is_stale is True

    def test_running_equals_latest_not_stale(self):
        s = ServiceStatus(name="x", desired=1, running=1, health="healthy",
                          running_revision=3, latest_revision=3)
        assert s.is_stale is False

    def test_no_revision_data_not_stale(self):
        # Provider didn't track revisions (FakeProvider, k8s) — not stale.
        s = ServiceStatus(name="x", desired=1, running=1, health="healthy")
        assert s.is_stale is False

    def test_only_one_revision_known_not_stale(self):
        # Latest unknown but running known — can't determine staleness.
        s = ServiceStatus(name="x", desired=1, running=1, health="healthy",
                          running_revision=2, latest_revision=None)
        assert s.is_stale is False


# ---------------------------------------------------------------------------
# render_status: revision column + STALE summary
# ---------------------------------------------------------------------------

class TestRenderStatus:
    def _report(self, services):
        return StatusReport(services=services, cluster_health="degraded")

    def test_no_revisions_no_revision_column(self):
        r = self._report([
            ServiceStatus(name="api", desired=1, running=1, health="healthy"),
        ])
        out = render_status(r)
        assert "revision" not in out.lower()
        assert "STALE" not in out

    def test_revision_column_appears_when_any_service_has_revision(self):
        r = self._report([
            ServiceStatus(name="api", desired=1, running=1, health="healthy",
                          running_revision=3, latest_revision=3),
        ])
        out = render_status(r)
        assert "revision" in out.lower()
        assert "3 = 3" in out  # not stale → '=' marker

    def test_stale_row_renders_arrow(self):
        r = self._report([
            ServiceStatus(name="celery", desired=1, running=1, health="stale",
                          running_revision=1, latest_revision=2),
        ])
        out = render_status(r)
        assert "1 → 2" in out  # arrow for stale
        assert "stale" in out.lower()

    def test_summary_line_lists_stale_services(self):
        r = self._report([
            ServiceStatus(name="api", desired=1, running=1, health="healthy",
                          running_revision=2, latest_revision=2),
            ServiceStatus(name="worker", desired=1, running=1, health="stale",
                          running_revision=1, latest_revision=2),
            ServiceStatus(name="beat", desired=1, running=1, health="stale",
                          running_revision=1, latest_revision=2),
        ])
        out = render_status(r)
        assert "STALE: " in out
        assert "worker" in out and "beat" in out
        assert "rc deploy --reconcile" in out


# ---------------------------------------------------------------------------
# rc deploy --reconcile
# ---------------------------------------------------------------------------

class TestReconcileFlag:
    def _v2_rc_yml(self, tmp_path) -> Path:
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text("version: 2\nproject: testp\n")
        return rc_yml

    def test_reconcile_with_no_stale_does_nothing(self, runner, tmp_path):
        rc_yml = self._v2_rc_yml(tmp_path)
        v2 = MagicMock()
        report = StatusReport(services=[
            ServiceStatus(name="api", desired=1, running=1, health="healthy",
                          running_revision=2, latest_revision=2),
        ], cluster_health="healthy")
        with patch("remote_compose.cli_v2.load_rc_yml",
                   return_value=(2, {}, v2)), \
             patch("remote_compose.cli_v2.build_deploy_context"), \
             patch("remote_compose.cli_v2.resolve_provider") as rp, \
             patch("remote_compose.cli_v2.dispatch_if_v2") as disp:
            rp.return_value.status.return_value = report
            result = runner.invoke(cli, ["-c", str(rc_yml), "deploy", "--reconcile"])
        assert result.exit_code == 0, result.output
        assert "No stale services detected" in result.output
        # dispatch never invoked — nothing to reconcile
        disp.assert_not_called()

    def test_reconcile_force_rolls_stale_services_only(self, runner, tmp_path):
        rc_yml = self._v2_rc_yml(tmp_path)
        v2 = MagicMock()
        report = StatusReport(services=[
            ServiceStatus(name="api", desired=1, running=1, health="healthy",
                          running_revision=2, latest_revision=2),
            ServiceStatus(name="worker", desired=1, running=1, health="stale",
                          running_revision=1, latest_revision=2),
            ServiceStatus(name="beat", desired=1, running=1, health="stale",
                          running_revision=1, latest_revision=2),
        ], cluster_health="degraded")
        with patch("remote_compose.cli_v2.load_rc_yml",
                   return_value=(2, {}, v2)), \
             patch("remote_compose.cli_v2.build_deploy_context"), \
             patch("remote_compose.cli_v2.resolve_provider") as rp, \
             patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True) as disp:
            rp.return_value.status.return_value = report
            result = runner.invoke(cli, ["-c", str(rc_yml), "deploy", "--reconcile"])
        assert result.exit_code == 0, result.output
        # dispatch called with exactly the stale services
        disp.assert_called_once()
        kwargs = disp.call_args.kwargs
        assert sorted(kwargs["services"]) == ["beat", "worker"]
        # api (healthy, in-sync) NOT in the list
        assert "api" not in kwargs["services"]

    def test_reconcile_with_services_flag_is_rejected(self, runner, tmp_path):
        rc_yml = self._v2_rc_yml(tmp_path)
        result = runner.invoke(cli, [
            "-c", str(rc_yml), "deploy", "--reconcile", "--services", "django",
        ])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_reconcile_with_tag_flag_is_rejected(self, runner, tmp_path):
        rc_yml = self._v2_rc_yml(tmp_path)
        result = runner.invoke(cli, [
            "-c", str(rc_yml), "deploy", "--reconcile", "--tag", "v1",
        ])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_reconcile_on_v1_rcyml_errors_clearly(self, runner, tmp_path):
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text("cluster: legacy\n")  # v1 schema
        with patch("remote_compose.cli_v2.load_rc_yml",
                   return_value=(1, {"cluster": "legacy"}, None)):
            result = runner.invoke(cli, ["-c", str(rc_yml), "deploy", "--reconcile"])
        assert result.exit_code != 0
        assert "v2" in result.output.lower()

    def test_reconcile_on_missing_rcyml_errors(self, runner, tmp_path):
        rc_yml = tmp_path / "rc.yml"  # NOT created
        result = runner.invoke(cli, ["-c", str(rc_yml), "deploy", "--reconcile"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()
