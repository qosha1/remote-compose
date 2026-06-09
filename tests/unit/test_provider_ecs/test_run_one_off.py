"""Provider.run_one_off — one-off ECS task that gets the task's secrets
(the right primitive for secret-dependent mgmt commands), plus the exec
profile-resolution guard and lifecycle hook `mode` validation.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.config._schema_types import ConfigError, LifecycleHookV2
from remote_compose.provider.base import DeployContext, ProviderError, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider, _profile_is_resolvable


def _ctx() -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-2", "cluster": "myapp-prod"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services={
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application"
            ),
        },
        secrets=[],
    )


def _ecs_session(*, exit_code, container_defs=None, stopped_reason="done"):
    """Mock boto3 session for the run_one_off happy path."""
    ecs = mock.MagicMock()
    ecs.describe_services.return_value = {
        "services": [
            {
                "taskDefinition": "arn:aws:ecs:us-west-2:111:task-definition/myapp-django:7",
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": ["subnet-a"],
                        "securityGroups": ["sg-a"],
                        "assignPublicIp": "ENABLED",
                    }
                },
                "launchType": "FARGATE",
            }
        ]
    }
    ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": container_defs or [{"name": "django"}]
        }
    }
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:111:task/myapp-prod/abc123"}],
        "failures": [],
    }
    ecs.get_waiter.return_value = mock.MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "containers": [{"name": "django", "exitCode": exit_code}],
                "stoppedReason": stopped_reason,
            }
        ]
    }
    logs = mock.MagicMock()
    logs.get_log_events.return_value = {"events": [{"message": "synced 5 templates"}]}

    session = mock.MagicMock()
    session.client.side_effect = lambda svc, **k: logs if svc == "logs" else ecs
    return session, ecs


class TestRunOneOff:
    def test_success_runs_task_with_overrides_and_returns_exit_and_logs(self):
        session, ecs = _ecs_session(exit_code=0)
        provider = ECSProvider(session_factory=lambda c: session)

        result = provider.run_one_off(_ctx(), "django", ["python", "manage.py", "x"])

        assert result.exit_code == 0
        assert "synced 5 templates" in result.stdout
        # run_task used the live service's task def + network + a command override
        kw = ecs.run_task.call_args.kwargs
        assert kw["taskDefinition"].endswith("myapp-django:7")
        assert kw["launchType"] == "FARGATE"
        assert "networkConfiguration" in kw
        ov = kw["overrides"]["containerOverrides"][0]
        assert ov["name"] == "django"
        assert ov["command"] == ["python", "manage.py", "x"]

    def test_nonzero_exit_is_propagated_with_reason(self):
        session, _ = _ecs_session(exit_code=3, stopped_reason="task failed")
        provider = ECSProvider(session_factory=lambda c: session)
        result = provider.run_one_off(_ctx(), "django", ["false"])
        assert result.exit_code == 3
        assert "task failed" in result.stderr

    def test_missing_exit_code_is_treated_as_failure(self):
        session, ecs = _ecs_session(exit_code=None, stopped_reason="OutOfMemory")
        ecs.describe_tasks.return_value = {
            "tasks": [
                {"containers": [{"name": "django"}], "stoppedReason": "OutOfMemory"}
            ]
        }
        provider = ECSProvider(session_factory=lambda c: session)
        result = provider.run_one_off(_ctx(), "django", ["x"])
        assert result.exit_code == 1
        assert "OutOfMemory" in result.stderr

    def test_no_wait_returns_task_arn_immediately(self):
        session, ecs = _ecs_session(exit_code=0)
        provider = ECSProvider(session_factory=lambda c: session)
        result = provider.run_one_off(_ctx(), "django", ["x"], wait=False)
        assert result.exit_code == 0
        assert "abc123" in result.stdout
        ecs.get_waiter.assert_not_called()

    def test_unknown_service_raises(self):
        session, ecs = _ecs_session(exit_code=0)
        ecs.describe_services.return_value = {"services": []}
        provider = ECSProvider(session_factory=lambda c: session)
        with pytest.raises(ProviderError, match="not found"):
            provider.run_one_off(_ctx(), "django", ["x"])

    def test_run_task_failure_raises(self):
        session, ecs = _ecs_session(exit_code=0)
        ecs.run_task.return_value = {"tasks": [], "failures": [{"reason": "RESOURCE"}]}
        provider = ECSProvider(session_factory=lambda c: session)
        with pytest.raises(ProviderError, match="run_task failed"):
            provider.run_one_off(_ctx(), "django", ["x"])


class TestProfileResolvable:
    def test_none_and_bogus_profiles_are_unresolvable(self):
        assert _profile_is_resolvable(None) is False
        assert _profile_is_resolvable("") is False
        # A profile name that won't exist in any shared config on the runner.
        assert _profile_is_resolvable("rc-definitely-not-a-real-profile-xyz") is False


class TestLifecycleHookModeValidation:
    def test_default_mode_is_exec(self):
        h = LifecycleHookV2(name="migrate", command=["python", "manage.py", "migrate"])
        h.validate()
        assert h.mode == "exec"

    def test_task_mode_valid(self):
        h = LifecycleHookV2(
            name="sync", command=["python", "manage.py", "sync"], mode="task"
        )
        h.validate()

    def test_bad_mode_rejected(self):
        h = LifecycleHookV2(name="x", command=["true"], mode="ssh")
        with pytest.raises(ConfigError, match="mode must be"):
            h.validate()

    def test_task_mode_cannot_be_interactive(self):
        h = LifecycleHookV2(name="x", command=["bash"], mode="task", interactive=True)
        with pytest.raises(ConfigError, match="cannot be interactive"):
            h.validate()
