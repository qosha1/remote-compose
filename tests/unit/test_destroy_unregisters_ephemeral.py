"""Tests for rc destroy unregistering its ephemeral entry (rc-e5u.46.6).

A single-stack `rc destroy --yes` against a project that was registered
via `rc deploy --ttl` / `rc up --ttl` must remove the local registry
entry after a successful provider.destroy. Otherwise stale entries
accumulate and `rc list --ephemeral` shows phantom stacks (verified
.46.6 e2e run #3, 2026-04-26).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.ephemeral import EphemeralRecord


@pytest.fixture
def runner():
    return CliRunner()


def _write_rc_yml_v2(tmp_path: Path, project: str, region: str = "us-west-1") -> Path:
    rc = {
        "version": 2,
        "project": project,
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {"region": region}},
        "terraform": {"backend": {"type": "local"}},
        "services": {},
    }
    rc_path = tmp_path / "rc.yml"
    rc_path.write_text(yaml.safe_dump(rc))
    # Compose file just needs to exist for build_deploy_context.
    (tmp_path / "docker-compose.yml").write_text("version: '3'\nservices: {}\n")
    return rc_path


def test_destroy_removes_ephemeral_registry_entry(runner, tmp_path):
    """Successful single-stack destroy → registry entry for this project
    + region is removed."""
    rc_path = _write_rc_yml_v2(tmp_path, project="test-46-6")

    fake_v2 = MagicMock()
    fake_v2.project = "test-46-6"

    with patch("remote_compose.cli_v2.load_rc_yml",
               return_value=(2, {"provider_config": {"ecs": {"region": "us-west-1"}}},
                             fake_v2)) as load_v2_dispatch, \
         patch("remote_compose.cli_v2.build_deploy_context"), \
         patch("remote_compose.cli_v2.resolve_provider") as rp, \
         patch("remote_compose.ephemeral.remove_stack") as rm:
        rp.return_value = MagicMock()
        result = runner.invoke(cli, ["-c", str(rc_path), "destroy", "--yes"])

    assert result.exit_code == 0, result.output
    rm.assert_called_once_with(project="test-46-6", region="us-west-1")


def test_destroy_unregister_failure_does_not_break_destroy(runner, tmp_path):
    """If the registry mutation fails for any reason (corrupt file,
    permission, etc.) the destroy still exits 0 — the AWS resources
    are already gone, the user shouldn't see an error about a local
    bookkeeping file."""
    rc_path = _write_rc_yml_v2(tmp_path, project="test-46-6")
    fake_v2 = MagicMock()
    fake_v2.project = "test-46-6"

    with patch("remote_compose.cli_v2.load_rc_yml",
               return_value=(2, {"provider_config": {"ecs": {"region": "us-west-1"}}},
                             fake_v2)), \
         patch("remote_compose.cli_v2.build_deploy_context"), \
         patch("remote_compose.cli_v2.resolve_provider") as rp, \
         patch("remote_compose.ephemeral.remove_stack",
               side_effect=RuntimeError("registry locked")):
        rp.return_value = MagicMock()
        result = runner.invoke(cli, ["-c", str(rc_path), "destroy", "--yes"])

    # Destroy still succeeds even though the registry mutation blew up.
    assert result.exit_code == 0, result.output


def test_destroy_no_ephemeral_entry_is_a_noop(runner, tmp_path):
    """Most stacks aren't ephemeral. remove_stack returns False for a
    project that was never registered; no error to the user."""
    rc_path = _write_rc_yml_v2(tmp_path, project="non-ephemeral-app")

    fake_v2 = MagicMock()
    fake_v2.project = "non-ephemeral-app"

    with patch("remote_compose.cli_v2.load_rc_yml",
               return_value=(2, {"provider_config": {"ecs": {"region": "us-west-1"}}},
                             fake_v2)), \
         patch("remote_compose.cli_v2.build_deploy_context"), \
         patch("remote_compose.cli_v2.resolve_provider") as rp, \
         patch("remote_compose.ephemeral.remove_stack",
               return_value=False) as rm:
        rp.return_value = MagicMock()
        result = runner.invoke(cli, ["-c", str(rc_path), "destroy", "--yes"])

    assert result.exit_code == 0, result.output
    # remove_stack was attempted; no-op return is fine.
    rm.assert_called_once_with(project="non-ephemeral-app", region="us-west-1")
