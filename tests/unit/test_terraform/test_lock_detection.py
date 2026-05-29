"""Failing-RED tests for remote-compose-ysh.

When a previous rc invocation holds the local terraform state lock
(.terraform.tfstate.lock.info), subsequent rc deploy / rc up calls today
hang silently — terraform inherits stdout/stderr but doesn't surface the
lock-busy condition fast enough, and rc has no pre-flight check.

The fix: provider.deploy() (or TerraformRunner.init/apply) should detect
the lock file and raise a clear, fast error pointing at the holding PID
BEFORE invoking terraform.

These tests fail until ysh is fixed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import RecordingTerraformRunner


def _write_held_lock(tf_dir: Path, holder_pid: int = 99999) -> Path:
    """Simulate an in-flight terraform apply by dropping a lock file."""
    tf_dir.mkdir(parents=True, exist_ok=True)
    lock_path = tf_dir / ".terraform.tfstate.lock.info"
    lock_path.write_text(
        json.dumps(
            {
                "ID": "abc-1234",
                "Operation": "OperationTypeApply",
                "Info": "",
                "Who": "user@host",
                "Version": "1.5.0",
                "Created": "2026-04-29T12:00:00.000Z",
                "Path": str(tf_dir / "terraform.tfstate"),
                "PID": holder_pid,
            }
        )
    )
    return lock_path


def _ctx(tmp_path: Path) -> DeployContext:
    return DeployContext(
        project="lock-test",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "lock-test",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(
                name="api",
                cpu=256,
                memory=512,
                type="application",
                image="nginx:alpine",
            ),
        },
        secrets=[],
    )


class TestLockDetectedFast:
    def test_held_lock_raises_clear_error_within_one_second(self, tmp_path):
        """When a stale/held .terraform.tfstate.lock.info file is present in
        the working dir, provider.deploy() should fail fast (< 1s) with a
        clear message identifying the lock holder — not hang on terraform's
        retry loop or block on subprocess output buffering.
        """
        tf_dir = tmp_path / "terraform"
        _write_held_lock(tf_dir, holder_pid=42424)

        ctx = _ctx(tmp_path)
        sess = mock.MagicMock()

        # Use a real-ish runner that would normally exec `terraform`. The
        # runner_factory still goes through emit_terraform first, so the
        # working dir gets created. Lock detection should fire BEFORE
        # runner.init() is invoked.
        recording = RecordingTerraformRunner(tf_dir)

        provider = ECSProvider(
            runner_factory=lambda d: recording,
            session_factory=lambda c: sess,
        )

        start = time.monotonic()
        with pytest.raises(Exception) as exc_info:
            provider.deploy(ctx)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, (
            f"lock detection took {elapsed:.2f}s — must be < 1s; instead "
            f"of failing fast, rc let terraform's retry loop run"
        )
        msg = str(exc_info.value).lower()
        assert (
            "lock" in msg
        ), f"error message must mention 'lock'; got: {exc_info.value!r}"
        assert "42424" in str(exc_info.value), (
            f"error message should identify the holder PID; " f"got: {exc_info.value!r}"
        )

    def test_no_lock_file_does_not_block_deploy(self, tmp_path):
        """Sanity: with no lock file, deploy proceeds normally (no false
        positive from the fast-fail path)."""
        ctx = _ctx(tmp_path)
        sess = mock.MagicMock()
        recording = RecordingTerraformRunner(tmp_path / "terraform")
        recording.script("output", "{}")

        provider = ECSProvider(
            runner_factory=lambda d: recording,
            session_factory=lambda c: sess,
        )
        # No lock file present — deploy should complete (no force-roll
        # because no build_context, but no exception either).
        result = provider.deploy(ctx)
        assert result is not None
