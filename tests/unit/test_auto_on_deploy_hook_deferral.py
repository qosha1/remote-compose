"""Tests for deferred auto_on_deploy hooks (rc-3q9 / e5u.46.12).

The earlier behavior fired lifecycle.migrate during dispatch_if_v2's deploy
path, BEFORE the outer rc up had pushed file-sourced secrets and force-
rolled the services. That made the django.migrate hook land on a task
still running with placeholder env vars from terraform's first-deploy SM
blob → 'manage.py migrate' couldn't reach Postgres → exit 254 noise on
every fresh `rc up`.

Fix: dispatch_if_v2('deploy', defer_lifecycle_hooks=True) skips the hook
step; rc up runs run_auto_on_deploy_hooks_for_path AFTER its
_secrets_push_v2 + force-roll, when tasks have real env. The helper also
waits for ECS deployment stability before exec-ing so hooks always land
on the latest task definition.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from remote_compose.cli_v2 import (
    dispatch_if_v2,
    run_auto_on_deploy_hooks_for_path,
)


@pytest.fixture
def rc_yml_with_migrate_hook(tmp_path: Path) -> Path:
    rc = {
        "version": 2,
        "project": "test-3q9",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {"region": "us-west-1"}},
        "terraform": {"backend": {"type": "local"}},
        "services": {
            "django": {
                "cpu": 256,
                "memory": 512,
                "type": "application",
                "lifecycle": {
                    "migrate": {
                        "command": ["python", "manage.py", "migrate", "--noinput"],
                        "auto_on_deploy": True,
                    },
                },
            },
        },
    }
    (tmp_path / "docker-compose.yml").write_text(
        "version: '3'\nservices: {django: {image: 'app:latest'}}\n"
    )
    rc_path = tmp_path / "rc.yml"
    rc_path.write_text(yaml.safe_dump(rc))
    return rc_path


# ---------------------------------------------------------------------------
# defer_lifecycle_hooks suppresses the in-dispatch hook run
# ---------------------------------------------------------------------------


class TestDeferLifecycleHooks:
    def test_default_path_runs_hooks_in_dispatch(self, rc_yml_with_migrate_hook):
        """rc deploy (no defer flag) runs hooks during dispatch — backward
        compatible with single-command `rc deploy`."""
        from remote_compose.provider.base import DeployResult
        with patch("remote_compose.cli_v2._run_auto_on_deploy_hooks") as run_hooks, \
             patch("remote_compose.cli_v2.resolve_provider") as rp, \
             patch("remote_compose.cli_v2._auto_push_empty_secrets_if_any"):
            provider = MagicMock()
            provider.deploy.return_value = DeployResult(
                revision_id="rev-test", services=["django"], duration_s=1.0,
            )
            rp.return_value = provider
            ok = dispatch_if_v2(rc_yml_with_migrate_hook, "deploy")
        assert ok is True
        run_hooks.assert_called_once()

    def test_defer_flag_skips_dispatch_hooks(self, rc_yml_with_migrate_hook):
        """rc up sets defer_lifecycle_hooks=True; dispatcher must not run
        hooks itself — caller will run them later via
        run_auto_on_deploy_hooks_for_path."""
        from remote_compose.provider.base import DeployResult
        with patch("remote_compose.cli_v2._run_auto_on_deploy_hooks") as run_hooks, \
             patch("remote_compose.cli_v2.resolve_provider") as rp, \
             patch("remote_compose.cli_v2._auto_push_empty_secrets_if_any"):
            provider = MagicMock()
            provider.deploy.return_value = DeployResult(
                revision_id="rev-test", services=["django"], duration_s=1.0,
            )
            rp.return_value = provider
            ok = dispatch_if_v2(
                rc_yml_with_migrate_hook, "deploy",
                defer_lifecycle_hooks=True,
            )
        assert ok is True
        run_hooks.assert_not_called()


# ---------------------------------------------------------------------------
# run_auto_on_deploy_hooks_for_path
# ---------------------------------------------------------------------------


class TestRunAutoOnDeployHooksForPath:
    def test_silent_no_op_for_v1_rc_yml(self, tmp_path):
        """v1 rc.yml has no lifecycle hooks; helper must not blow up."""
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text("project_name: legacy\ncluster: foo\n")
        # Should not raise.
        run_auto_on_deploy_hooks_for_path(rc_path, wait_for_stable=False)

    def test_silent_no_op_when_no_auto_hooks_declared(self, tmp_path):
        """A v2 rc.yml with no auto_on_deploy hooks → fast no-op (skips
        even the ECS describe-services round trip)."""
        rc = {
            "version": 2,
            "project": "no-hooks",
            "compose_file": "docker-compose.yml",
            "provider": "ecs",
            "provider_config": {"ecs": {"region": "us-west-1"}},
            "terraform": {"backend": {"type": "local"}},
            "services": {"app": {"cpu": 256, "memory": 512, "type": "application"}},
        }
        (tmp_path / "docker-compose.yml").write_text(
            "version: '3'\nservices: {app: {image: 'a:1'}}\n"
        )
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text(yaml.safe_dump(rc))

        with patch("remote_compose.cli_v2.resolve_provider") as rp:
            run_auto_on_deploy_hooks_for_path(rc_path)
        # No provider, no exec — early return because no auto_on_deploy hook.
        rp.assert_not_called()

    def test_runs_hooks_when_auto_on_deploy_declared(
        self, rc_yml_with_migrate_hook, monkeypatch,
    ):
        """End-to-end: helper resolves provider, waits for stability,
        then runs hooks."""
        monkeypatch.setenv("RC_HOOK_WAIT_TIMEOUT_S", "5")
        monkeypatch.setenv("RC_HOOK_WAIT_INTERVAL_S", "1")

        # Mock provider with a stable ECS service: 1 PRIMARY deployment,
        # rolloutState=COMPLETED.
        provider = MagicMock()
        ecs_client = MagicMock()
        ecs_client.describe_services.return_value = {
            "services": [{
                "serviceName": "django",
                "deployments": [{
                    "rolloutState": "COMPLETED",
                    "runningCount": 1,
                    "desiredCount": 1,
                }],
            }],
        }
        session = MagicMock()
        session.client.return_value = ecs_client
        provider.session_factory.return_value = session

        # rc-e5u.36.6: wait moved INSIDE _run_auto_on_deploy_hooks. The
        # outer helper now just delegates with wait_for_stable=True.
        with patch("remote_compose.cli_v2.resolve_provider", return_value=provider), \
             patch("remote_compose.cli_v2._run_auto_on_deploy_hooks") as run_hooks:
            run_auto_on_deploy_hooks_for_path(rc_yml_with_migrate_hook)

        run_hooks.assert_called_once()
        # The wait is now part of _run_auto_on_deploy_hooks's contract,
        # threaded via wait_for_stable=True.
        kwargs = run_hooks.call_args.kwargs
        assert kwargs.get("wait_for_stable", True) is True

    def test_proceeds_anyway_after_stability_timeout(
        self, rc_yml_with_migrate_hook, monkeypatch,
    ):
        """If services don't stabilize within budget, run hooks anyway
        with a warning — better noisy than stuck. The wait now happens
        inside _run_auto_on_deploy_hooks so this test exercises the
        end-to-end path without patching out the inner function."""
        monkeypatch.setenv("RC_HOOK_WAIT_TIMEOUT_S", "1")
        monkeypatch.setenv("RC_HOOK_WAIT_INTERVAL_S", "0.1")

        provider = MagicMock()
        ecs_client = MagicMock()
        # Stuck rolling — never COMPLETED.
        ecs_client.describe_services.return_value = {
            "services": [{
                "serviceName": "django",
                "deployments": [
                    {"rolloutState": "IN_PROGRESS", "runningCount": 0, "desiredCount": 1},
                    {"rolloutState": "COMPLETED", "runningCount": 1, "desiredCount": 1},
                ],
            }],
        }
        session = MagicMock()
        session.client.return_value = ecs_client
        provider.session_factory.return_value = session
        provider.exec.return_value = MagicMock(exit_code=0, stdout="", stderr="")

        with patch("remote_compose.cli_v2.resolve_provider", return_value=provider):
            run_auto_on_deploy_hooks_for_path(rc_yml_with_migrate_hook)

        # Wait was attempted; the service never went COMPLETED so the
        # wait timed out. Hook still fired (provider.exec called).
        assert ecs_client.describe_services.called
        provider.exec.assert_called()

    def test_skip_wait_for_stable_when_disabled(self, rc_yml_with_migrate_hook):
        """wait_for_stable=False bypasses the ECS describe-services poll.
        Used by tests; production callers (rc up) leave it True."""
        provider = MagicMock()
        provider.exec.return_value = MagicMock(exit_code=0, stdout="", stderr="")
        with patch("remote_compose.cli_v2.resolve_provider", return_value=provider):
            run_auto_on_deploy_hooks_for_path(
                rc_yml_with_migrate_hook, wait_for_stable=False,
            )

        # Wait disabled → session_factory was never called for ECS describe.
        provider.session_factory.assert_not_called()
        # But hooks DID run.
        provider.exec.assert_called()
