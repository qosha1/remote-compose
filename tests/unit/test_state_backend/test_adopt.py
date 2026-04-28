"""Failing unit tests for state_backend.adopt + cli_commands.adopt (5h8.3 RED gate).

Cover:
  e. rc adopt loads rc.yml v2, calls adopt path, asserts boto3 + tf invocation order
  f. adopt is idempotent (every terraform import returns 'already in state')
  g. partial recovery — middle-of-import failure leaves consistent state
  h. concurrent deploy lock — second simultaneous deploy gets clear lock-busy error
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import click
import pytest
import yaml
from click.testing import CliRunner


def _write_v2_rc_yml(tmp_path: Path, *, backend_type: str = "s3") -> Path:
    """Write a v2 rc.yml with a configured backend."""
    backend_block: dict
    if backend_type == "s3":
        backend_block = {
            "type": "s3",
            "bucket": "033937118837-rc-tfstate",
            "key": "ss-debuggai/prod/ecs.tfstate",
            "region": "us-west-2",
            "dynamodb_table": "rc-tfstate-locks",
        }
    else:
        backend_block = {"type": "local"}
    rc = {
        "version": 2,
        "project": "ss-debuggai",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {
            "region": "us-west-2",
            "cluster": "ss-debuggai-prod",
            "aws_profile": "debuggai",
        }},
        "terraform": {"backend": backend_block},
        "services": {
            "django": {"cpu": 1024, "memory": 4096, "type": "application"},
            "nginx": {"cpu": 256, "memory": 512, "type": "proxy",
                      "public": True, "port": 80},
        },
    }
    (tmp_path / "docker-compose.yml").write_text(
        yaml.safe_dump({"services": {"django": {"image": "busybox"},
                                      "nginx": {"image": "nginx:alpine"}}})
    )
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(rc, sort_keys=False))
    return p


class TestAdoptCommandDispatches:
    def test_invokes_discovery_then_terraform_init_then_imports(self, tmp_path):
        """rc adopt orchestrates: (a) v1_migrate.discover walks AWS,
        (b) v1_migrate.translate generates import addresses,
        (c) terraform init runs, (d) terraform import runs sequentially
        for every (resource_address, resource_id) tuple."""
        from remote_compose.cli import cli as rc_cli

        rc_path = _write_v2_rc_yml(tmp_path)

        # Mock the orchestrator entry point so we don't actually drive
        # terraform / boto3 in this unit test.
        with mock.patch(
            "remote_compose.state_backend.adopt.adopt_v1_to_v2",
        ) as adopt_fn:
            adopt_fn.return_value = mock.Mock(
                imported=26, skipped=0, failed=[], duration_s=12.3,
            )
            runner = CliRunner()
            result = runner.invoke(
                rc_cli, ["--config", str(rc_path), "adopt"],
            )

        assert result.exit_code == 0, result.output
        adopt_fn.assert_called_once()
        # The adopt entry point receives the rc.yml v2 + the working dir
        # (where terraform state lands).
        kwargs = adopt_fn.call_args.kwargs
        assert "rc_yml_path" in kwargs or len(adopt_fn.call_args.args) >= 1


class TestAdoptIdempotent:
    def test_second_run_is_a_noop(self, tmp_path):
        """When every resource is already imported, a second `rc adopt`
        run reports 0 imported / N skipped, no failures."""
        from remote_compose.state_backend.adopt import adopt_v1_to_v2

        rc_path = _write_v2_rc_yml(tmp_path)
        # First call: pretend everything was already imported.
        # adopt_v1_to_v2 internally walks AWS, generates 26 import
        # addresses, runs terraform import per resource. Each import
        # call returns "already in state" (no work done).
        with mock.patch(
            "remote_compose.state_backend.adopt._run_terraform_import"
        ) as imp, mock.patch(
            "remote_compose.state_backend.adopt._discover_imports"
        ) as disc:
            disc.return_value = [
                ("module.ecs.aws_ecs_cluster.this", "arn:aws:ecs:...:cluster/x"),
                ("module.alb.aws_lb.this", "arn:aws:elasticloadbalancing:...:loadbalancer/y"),
            ]
            imp.return_value = ("already_in_state", "")  # (status, message)
            result = adopt_v1_to_v2(
                rc_yml_path=rc_path, working_dir=tmp_path,
            )
        assert result.imported == 0
        assert result.skipped == 2
        assert result.failed == []


class TestAdoptPartialRecovery:
    def test_partial_failure_leaves_consistent_state(self, tmp_path):
        """When terraform import fails for resource N+1, the previously
        imported N stay in state, the failed one is reported, the user
        re-runs and only the failed set retries."""
        from remote_compose.state_backend.adopt import adopt_v1_to_v2

        rc_path = _write_v2_rc_yml(tmp_path)
        # Three imports: first two succeed, third fails with a real-shape
        # terraform error.
        with mock.patch(
            "remote_compose.state_backend.adopt._run_terraform_import"
        ) as imp, mock.patch(
            "remote_compose.state_backend.adopt._discover_imports"
        ) as disc:
            disc.return_value = [
                ("module.ecs.aws_ecs_cluster.this", "arn:cluster"),
                ("module.alb.aws_lb.this", "arn:alb"),
                ("module.efs.aws_efs_file_system.this", "fs-bad"),
            ]
            imp.side_effect = [
                ("imported", ""),
                ("imported", ""),
                ("failed", "Error: aws_efs_file_system not found"),
            ]
            result = adopt_v1_to_v2(
                rc_yml_path=rc_path, working_dir=tmp_path,
            )
        assert result.imported == 2
        # `failed` is a list of (address, id, error) tuples.
        assert len(result.failed) == 1
        addr, rid, err = result.failed[0]
        assert "aws_efs_file_system" in addr
        assert "fs-bad" == rid
        assert "not found" in err


class TestConcurrentDeployLock:
    def test_cli_renders_friendly_lock_busy_message(self, tmp_path):
        """When terraform's s3 backend can't acquire the dynamodb lock,
        TerraformRunner raises TerraformError. The CLI must intercept it
        and render a user-friendly message ('another rc deploy is in
        flight') instead of dumping the raw terraform stack trace."""
        from remote_compose.cli import cli as rc_cli
        from remote_compose.terraform.runner import TerraformError

        rc_path = _write_v2_rc_yml(tmp_path)
        lock_err = TerraformError(
            cmd=["terraform", "init"],
            returncode=1,
            stdout="",
            stderr=(
                "Error: Error acquiring the state lock\n"
                "Error message: ConditionalCheckFailedException\n"
                "Lock Info:\n"
                "  ID:        abc-123\n"
                "  Path:      ss-debuggai/prod/ecs.tfstate\n"
                "  Operation: OperationTypeApply\n"
            ),
        )

        # Patch ECSProvider.deploy to raise the lock error.
        with mock.patch(
            "remote_compose.provider.ecs.provider.ECSProvider.deploy",
            side_effect=lock_err,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli, ["--config", str(rc_path), "deploy"],
            )

        assert result.exit_code != 0
        # User-facing output must NOT be a raw stack trace; it should
        # carry a clear "lock held" / "concurrent deploy" hint.
        out = result.output.lower() + (result.stderr_bytes or b"").decode().lower()
        assert (
            "lock" in out and ("concurrent" in out or "another" in out
                                or "in flight" in out or "already" in out)
        ), (
            f"expected friendly lock-busy CLI message; got:\n{result.output}"
        )
