"""rc-j04: rc deploy --dry-run on v2 stacks must route to plan, not deploy.

Filed as GitHub issue #2 by start-simpli session: --dry-run was silently
ignored in the v2 dispatch path; users got a real apply.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from remote_compose.cli import cli


def _v2_rc_yml(tmp_path: Path) -> Path:
    rc = tmp_path / "rc.yml"
    rc.write_text(textwrap.dedent("""
        version: 2
        project: dry-run-test
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
            cluster: dry-run-test-cluster
            vpc_cidr: 10.0.0.0/16
        services:
          web:
            public: true
            port: 80
    """).strip())
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx:alpine\n    ports: ['80:80']\n"
    )
    return rc


def test_dry_run_routes_to_plan_not_deploy(tmp_path):
    rc = _v2_rc_yml(tmp_path)
    runner = CliRunner()
    calls: list[tuple[str, dict]] = []

    def fake_dispatch(config_path, command, **kwargs):
        calls.append((command, kwargs))
        return True  # claim the v2 path either way

    with mock.patch(
        "remote_compose.cli_v2.dispatch_if_v2",
        side_effect=fake_dispatch,
    ):
        result = runner.invoke(
            cli,
            ["-c", str(rc), "deploy", "--dry-run"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    # Critical: plan was dispatched, deploy was NOT.
    commands_called = [c for c, _ in calls]
    assert (
        "plan" in commands_called
    ), f"--dry-run should route to plan; got {commands_called}"
    assert (
        "deploy" not in commands_called
    ), f"--dry-run must NOT trigger deploy; got {commands_called}"
    assert "--dry-run" in result.output or "dry-run" in result.output.lower()


def test_no_dry_run_still_deploys(tmp_path):
    rc = _v2_rc_yml(tmp_path)
    runner = CliRunner()
    calls: list[str] = []

    def fake_dispatch(config_path, command, **kwargs):
        calls.append(command)
        return True

    with mock.patch(
        "remote_compose.cli_v2.dispatch_if_v2",
        side_effect=fake_dispatch,
    ):
        result = runner.invoke(
            cli,
            ["-c", str(rc), "deploy"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    assert "deploy" in calls
    assert "plan" not in calls
