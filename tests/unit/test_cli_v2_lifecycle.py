"""Unit tests for the v2 lifecycle dispatch surface.

Three concerns proven here:

1. `rc lifecycle <hook>` resolves the right declarer when the hook is
   declared on exactly one service, errors when ambiguous or missing.
2. `lifecycle hook with run_once=true + probe` short-circuits when the
   probe returns 0 (already-applied), runs the command otherwise.
3. `_run_auto_on_deploy_hooks` walks every service in declaration order,
   only running hooks where auto_on_deploy=true, and surfacing failures
   as warnings (not raising).

These tests stub provider.exec() with a recording mock so no real ECS /
SSM / boto3 / subprocess calls happen.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner

from remote_compose.cli import cli as rc_cli
from remote_compose.cli_v2 import (
    _run_auto_on_deploy_hooks,
    build_deploy_context,
    load_rc_yml,
)
from remote_compose.provider.base import ExecResult

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _write_v2_project(
    tmp_path: Path,
    services_yml: dict,
    project: str = "myapp",
) -> Path:
    """Write a docker-compose.yml + rc.yml v2 pair, return rc.yml path."""
    compose = {"services": {name: {"image": "busybox"} for name in services_yml}}
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    rc = {
        "version": 2,
        "project": project,
        "compose_file": "docker-compose.yml",
        "provider": "fake",
        "services": services_yml,
    }
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(rc, sort_keys=False))
    return p


@pytest.fixture(autouse=True)
def _reset_fake_provider():
    """FakeProvider holds class-level state between tests; isolate."""
    from remote_compose.provider.fake import FakeProvider

    FakeProvider.reset()
    yield
    FakeProvider.reset()


# ---------------------------------------------------------------------
# 1. rc lifecycle <hook> declarer resolution
# ---------------------------------------------------------------------


class TestLifecycleDeclarerResolution:
    def test_unique_declarer_runs_without_explicit_service(self, tmp_path):
        """One service declares 'migrate' → that service runs the hook."""
        rc_path = _write_v2_project(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {
                        "migrate": {"command": ["python", "manage.py", "migrate"]},
                    },
                },
                "worker": {"image": "busybox"},
            },
        )

        # Mock provider.exec to record the call + return success.
        recorded = []

        def fake_exec(ctx, service, command, interactive=False):
            recorded.append((service, command, interactive))
            return ExecResult(exit_code=0, stdout="ok\n", stderr="")

        with mock.patch(
            "remote_compose.provider.fake.FakeProvider.exec",
            side_effect=fake_exec,
            autospec=False,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli,
                ["--config", str(rc_path), "lifecycle", "migrate"],
            )

        assert result.exit_code == 0, result.output
        assert len(recorded) == 1
        svc, cmd, _ = recorded[0]
        assert svc == "django"
        assert cmd == ["python", "manage.py", "migrate"]

    def test_explicit_service_disambiguates_when_multiple_declarers(self, tmp_path):
        """Two services declare 'shell' → explicit service arg picks one."""
        rc_path = _write_v2_project(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {"shell": {"command": ["python"]}},
                },
                "rails": {
                    "image": "busybox",
                    "lifecycle": {"shell": {"command": ["bin/rails", "console"]}},
                },
            },
        )

        recorded = []

        def fake_exec(ctx, service, command, interactive=False):
            recorded.append((service, command))
            return ExecResult(exit_code=0, stdout="", stderr="")

        with mock.patch(
            "remote_compose.provider.fake.FakeProvider.exec",
            side_effect=fake_exec,
            autospec=False,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli,
                ["--config", str(rc_path), "lifecycle", "shell", "rails"],
            )

        assert result.exit_code == 0, result.output
        assert recorded == [("rails", ["bin/rails", "console"])]

    def test_ambiguous_hook_without_service_errors(self, tmp_path):
        """Multiple declarers + no explicit service → exits non-zero with hint."""
        rc_path = _write_v2_project(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {"shell": {"command": ["python"]}},
                },
                "rails": {
                    "image": "busybox",
                    "lifecycle": {"shell": {"command": ["bin/rails", "console"]}},
                },
            },
        )

        runner = CliRunner()
        result = runner.invoke(
            rc_cli,
            ["--config", str(rc_path), "lifecycle", "shell"],
        )
        assert result.exit_code != 0
        assert (
            "multiple services declare" in result.output.lower()
            or "multiple services declare" in (result.stderr_bytes or b"").decode()
        )

    def test_unknown_hook_errors(self, tmp_path):
        rc_path = _write_v2_project(
            tmp_path,
            {
                "django": {"image": "busybox"},
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            rc_cli,
            ["--config", str(rc_path), "lifecycle", "doesnotexist"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------
# 2. run_once + probe short-circuit
# ---------------------------------------------------------------------


class TestRunOnceProbe:
    def test_probe_exit_0_skips_command(self, tmp_path):
        """probe returns 0 (already-applied) → command must NOT run."""
        rc_path = _write_v2_project(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {
                        "createsuperuser": {
                            "command": [
                                "python",
                                "manage.py",
                                "createsuperuser",
                                "--noinput",
                            ],
                            "run_once": True,
                            "probe": [
                                "python",
                                "-c",
                                "from x import User; exit(0 if User.objects.exists() else 1)",
                            ],
                        },
                    },
                },
            },
        )

        # Probe call returns 0 (truthy), command call should never happen.
        calls: list[tuple[str, list[str]]] = []

        def fake_exec(ctx, service, command, interactive=False):
            calls.append((service, command))
            # First call is the probe — return exit 0 to short-circuit.
            return ExecResult(exit_code=0, stdout="", stderr="")

        with mock.patch(
            "remote_compose.provider.fake.FakeProvider.exec",
            side_effect=fake_exec,
            autospec=False,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli,
                ["--config", str(rc_path), "lifecycle", "createsuperuser"],
            )

        assert result.exit_code == 0, result.output
        # Exactly one exec call — the probe — and NO command call.
        assert len(calls) == 1
        _, probe_cmd = calls[0]
        assert "manage.py" not in " ".join(
            probe_cmd
        ) or "createsuperuser" not in " ".join(
            probe_cmd
        ), f"command appears to have run despite probe exit 0: {probe_cmd}"
        assert (
            "skipping" in result.output.lower()
            or "already done" in result.output.lower()
        )

    def test_probe_nonzero_runs_command(self, tmp_path):
        """probe returns nonzero → command must run after."""
        rc_path = _write_v2_project(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {
                        "createsuperuser": {
                            "command": [
                                "python",
                                "manage.py",
                                "createsuperuser",
                                "--noinput",
                            ],
                            "run_once": True,
                            "probe": ["python", "-c", "exit(1)"],
                        },
                    },
                },
            },
        )

        calls: list[list[str]] = []

        def fake_exec(ctx, service, command, interactive=False):
            calls.append(command)
            # Probe returns 1, command returns 0.
            if "manage.py" in command and "createsuperuser" in command:
                return ExecResult(exit_code=0, stdout="", stderr="")
            return ExecResult(exit_code=1, stdout="", stderr="")  # probe fails

        with mock.patch(
            "remote_compose.provider.fake.FakeProvider.exec",
            side_effect=fake_exec,
            autospec=False,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli,
                ["--config", str(rc_path), "lifecycle", "createsuperuser"],
            )

        assert result.exit_code == 0, result.output
        # Two calls: probe, then command.
        assert len(calls) == 2
        assert "manage.py" in calls[1] and "createsuperuser" in calls[1]


# ---------------------------------------------------------------------
# 3. _run_auto_on_deploy_hooks behavior
# ---------------------------------------------------------------------


class TestAutoOnDeployHooks:
    def _ctx_and_v2(self, tmp_path, services_yml):
        rc_path = _write_v2_project(tmp_path, services_yml)
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        return ctx, v2

    def test_only_runs_hooks_with_auto_on_deploy_true(self, tmp_path):
        ctx, v2 = self._ctx_and_v2(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {
                        "migrate": {
                            "command": ["python", "manage.py", "migrate"],
                            "auto_on_deploy": True,
                        },
                        "shell": {
                            "command": ["python"],
                            # auto_on_deploy: false (default)
                        },
                    },
                },
            },
        )

        provider = mock.MagicMock()
        provider.exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")

        _run_auto_on_deploy_hooks(provider, ctx, v2)

        # Only one exec call — the migrate hook.
        assert provider.exec.call_count == 1
        _, kwargs = provider.exec.call_args
        # called positionally: provider.exec(ctx, svc_name, list(hook.command))
        args = provider.exec.call_args.args
        assert args[1] == "django"
        assert args[2] == ["python", "manage.py", "migrate"]

    def test_no_hooks_means_no_exec_calls(self, tmp_path):
        ctx, v2 = self._ctx_and_v2(
            tmp_path,
            {
                "django": {"image": "busybox"},
                "worker": {"image": "busybox"},
            },
        )

        provider = mock.MagicMock()
        _run_auto_on_deploy_hooks(provider, ctx, v2)
        assert provider.exec.call_count == 0

    def test_hook_failure_does_not_raise(self, tmp_path):
        """A failing auto_on_deploy hook is surfaced as a warning, not raised."""
        ctx, v2 = self._ctx_and_v2(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {
                        "migrate": {
                            "command": ["python", "manage.py", "migrate"],
                            "auto_on_deploy": True,
                        },
                    },
                },
            },
        )

        provider = mock.MagicMock()
        provider.exec.return_value = ExecResult(
            exit_code=1,
            stdout="",
            stderr="boom",
        )

        # Must not raise.
        _run_auto_on_deploy_hooks(provider, ctx, v2)
        assert provider.exec.call_count == 1

    def test_run_once_probe_satisfied_skips_command(self, tmp_path):
        ctx, v2 = self._ctx_and_v2(
            tmp_path,
            {
                "django": {
                    "image": "busybox",
                    "lifecycle": {
                        "createsuperuser": {
                            "command": ["python", "manage.py", "createsuperuser"],
                            "auto_on_deploy": True,
                            "run_once": True,
                            "probe": ["python", "-c", "import sys; sys.exit(0)"],
                        },
                    },
                },
            },
        )

        provider = mock.MagicMock()
        provider.exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")

        _run_auto_on_deploy_hooks(provider, ctx, v2)

        # Exactly one exec — the probe — and NOT the command.
        assert provider.exec.call_count == 1
        args = provider.exec.call_args.args
        assert args[2] == [
            "python",
            "-c",
            "import sys; sys.exit(0)",
        ], f"only the probe should have run; got {args[2]}"
