"""Unit tests for ECSProvider lifecycle methods (Phase 6b.1).

Uses ``RecordingTerraformRunner`` so no real terraform runs; mocks the boto3
session factory so no AWS calls are made. These tests verify the ECSProvider
method bodies execute the right sequence against their collaborators.

Real-terraform + real-AWS verification lives in tests/integration/.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import RecordingTerraformRunner


def _ctx(tmp_path: Path, **overrides) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": overrides.pop("region", "us-west-2"),
                "cluster": overrides.pop("cluster", "myapp-prod"),
                "vpc_cidr": "10.0.0.0/16",
                "aws_profile": "default",
            }
        },
        tf_backend_config=overrides.pop("tf_backend", {"type": "local"}),
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/",
            ),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=[],
    )


@pytest.fixture
def recorder():
    """A single recording runner reused across a provider's method calls."""
    holder: dict = {"runner": None}

    def factory(out_dir: Path) -> RecordingTerraformRunner:
        if holder["runner"] is None:
            holder["runner"] = RecordingTerraformRunner(out_dir)
        return holder["runner"]

    factory.holder = holder  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def mock_session():
    """A mock boto3 session whose .client(service) returns a MagicMock."""
    sess = mock.MagicMock()
    sess.client.return_value = mock.MagicMock()
    return sess


@pytest.fixture
def provider(recorder, mock_session):
    return ECSProvider(
        runner_factory=recorder,
        session_factory=lambda ctx: mock_session,
    )


class TestPlan:
    def test_plan_emits_terraform_inits_and_plans(self, provider, recorder, tmp_path):
        recorder.holder["runner"] = None  # ensure clean
        # pre-script a plan output so the parser returns meaningful counts
        ctx = _ctx(tmp_path)
        plan_text = "Plan: 5 to add, 2 to change, 1 to destroy.\n"
        # We need to create the recorder with the right out_dir before plan runs;
        # recorder factory does this lazily — so script AFTER plan starts is too late.
        # Instead: pre-seed by calling factory with the expected path.
        out_dir = ctx.working_dir / "terraform"
        runner = recorder(out_dir)
        runner.script("plan", plan_text)

        result = provider.plan(ctx)

        assert result.create == 5
        assert result.update == 2
        assert result.destroy == 1
        # verify the sequence: emit wrote files, runner got init then plan
        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "plan"]

    def test_plan_writes_terraform_files(self, provider, tmp_path):
        ctx = _ctx(tmp_path)
        provider.plan(ctx)
        tf_dir = ctx.working_dir / "terraform"
        assert (tf_dir / "main.tf").exists() or (tf_dir / "services.tf").exists()
        assert (tf_dir / "variables.tf").exists()


class TestDeploy:
    def test_deploy_runs_emit_init_apply(self, provider, recorder, tmp_path):
        ctx = _ctx(tmp_path)
        out_dir = ctx.working_dir / "terraform"
        runner = recorder(out_dir)
        result = provider.deploy(ctx)

        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "apply", "output"]
        assert result.revision_id
        assert set(result.services) == {"web", "api"}

    def test_deploy_idempotent_revision_id(self, recorder, mock_session, tmp_path):
        """Two deploys with identical inputs must yield identical revision_id."""
        ctx = _ctx(tmp_path)
        p1 = ECSProvider(
            runner_factory=lambda d: RecordingTerraformRunner(d),
            session_factory=lambda c: mock_session,
        )
        p2 = ECSProvider(
            runner_factory=lambda d: RecordingTerraformRunner(d),
            session_factory=lambda c: mock_session,
        )
        r1 = p1.deploy(ctx)
        r2 = p2.deploy(ctx)
        assert r1.revision_id == r2.revision_id

    def test_deploy_includes_terraform_outputs(self, provider, recorder, tmp_path):
        ctx = _ctx(tmp_path)
        out_dir = ctx.working_dir / "terraform"
        runner = recorder(out_dir)
        runner.script(
            "output", '{"alb_dns_name": {"value": "my-alb-1.elb.amazonaws.com"}}'
        )

        result = provider.deploy(ctx)
        assert "alb_dns_name" in result.terraform_outputs


class TestDestroy:
    def test_destroy_runs_init_then_destroy(self, provider, recorder, tmp_path):
        ctx = _ctx(tmp_path)
        # pre-emit so destroy has something to consume
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        # reset recorder call log
        runner = recorder(ctx.working_dir / "terraform")
        runner.calls.clear()

        provider.destroy(ctx)

        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "destroy"]

    def test_destroy_emits_if_no_module(self, provider, recorder, tmp_path):
        """If tf dir doesn't exist yet, destroy emits first (so terraform
        has something to init against)."""
        ctx = _ctx(tmp_path)
        assert not (ctx.working_dir / "terraform").exists()
        provider.destroy(ctx)
        runner = recorder(ctx.working_dir / "terraform")
        assert (ctx.working_dir / "terraform").exists()
        subcmds = [c.args[0] for c in runner.calls]
        assert "init" in subcmds and "destroy" in subcmds


class TestRedeploy:
    def test_redeploy_force_new_deployment_per_service(
        self, provider, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path)
        provider.redeploy(ctx)
        ecs_client = mock_session.client.return_value
        assert ecs_client.update_service.call_count == 2  # web + api
        for call in ecs_client.update_service.call_args_list:
            kwargs = call.kwargs
            assert kwargs["cluster"] == "myapp-prod"
            assert kwargs["forceNewDeployment"] is True

    def test_redeploy_subset(self, provider, mock_session, tmp_path):
        ctx = _ctx(tmp_path)
        provider.redeploy(ctx, services=["api"])
        ecs_client = mock_session.client.return_value
        assert ecs_client.update_service.call_count == 1
        kwargs = ecs_client.update_service.call_args.kwargs
        assert kwargs["service"] == "api"


class TestStatus:
    def test_status_maps_boto3_response_to_report(
        self, provider, mock_session, tmp_path
    ):
        ecs_client = mock_session.client.return_value
        ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "web",
                    "runningCount": 2,
                    "desiredCount": 2,
                    "events": [{"message": "steady state"}],
                },
                {
                    "serviceName": "api",
                    "runningCount": 0,
                    "desiredCount": 1,
                    "events": [{"message": "starting"}],
                },
            ],
        }
        ctx = _ctx(tmp_path)
        report = provider.status(ctx)
        by_name = {s.name: s for s in report.services}
        assert by_name["web"].health == "healthy"
        assert by_name["api"].health == "degraded"
        assert report.cluster_health == "degraded"

    def test_status_all_services_reported_even_if_missing(
        self, provider, mock_session, tmp_path
    ):
        ecs_client = mock_session.client.return_value
        ecs_client.describe_services.return_value = {"services": []}
        ctx = _ctx(tmp_path)
        report = provider.status(ctx)
        names = {s.name for s in report.services}
        assert names == {"web", "api"}
        assert all(s.health == "unknown" for s in report.services)

    def test_status_reports_ingress_url_when_tf_outputs_present(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path)
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        runner = recorder(ctx.working_dir / "terraform")
        runner.script(
            "output", '{"alb_dns_name": {"value": "alb-99.elb.amazonaws.com"}}'
        )

        mock_session.client.return_value.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "web",
                    "runningCount": 1,
                    "desiredCount": 1,
                    "events": [],
                },
                {
                    "serviceName": "api",
                    "runningCount": 1,
                    "desiredCount": 1,
                    "events": [],
                },
            ],
        }
        report = provider.status(ctx)
        assert report.ingress_url == "http://alb-99.elb.amazonaws.com"


class TestRollback:
    def test_local_backend_refused(self, provider, tmp_path):
        ctx = _ctx(tmp_path, tf_backend={"type": "local"})
        with pytest.raises(ProviderError, match="local terraform backend"):
            provider.rollback(ctx)

    def test_remote_backend_not_implemented_yet(self, provider, tmp_path):
        ctx = _ctx(
            tmp_path,
            tf_backend={
                "type": "s3",
                "bucket": "b",
                "key": "k.tfstate",
                "region": "us-west-2",
            },
        )
        with pytest.raises(NotImplementedError):
            provider.rollback(ctx)


class TestLogsAndExec:
    def test_logs_pulls_latest_stream_events(self, provider, mock_session, tmp_path):
        logs = mock_session.client.return_value
        logs.describe_log_streams.return_value = {
            "logStreams": [{"logStreamName": "web/main/abc"}],
        }
        logs.get_log_events.return_value = {
            "events": [
                {"message": "line 1"},
                {"message": "line 2"},
            ],
        }
        ctx = _ctx(tmp_path)
        out = list(provider.logs(ctx, "web", tail=50))
        assert out == ["line 1", "line 2"]

    def test_logs_empty_when_no_streams(self, provider, mock_session, tmp_path):
        mock_session.client.return_value.describe_log_streams.return_value = {
            "logStreams": []
        }
        ctx = _ctx(tmp_path)
        assert list(provider.logs(ctx, "web")) == []

    def test_exec_captures_stdout_via_sentinels(self, provider, mock_session, tmp_path):
        from unittest import mock as _mock

        ecs = mock_session.client.return_value
        ecs.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:...:task/abc"]}
        ctx = _ctx(tmp_path)
        # Simulate aws ecs execute-command stdout: session manager chrome
        # before, then our sentinel-bracketed payload, then chrome after.
        fake_stdout = (
            "Starting session...\n"
            "__RC_EXEC_BEGIN__\n"
            "hello world\n"
            "__RC_EXEC_EXIT__=0\n"
            "__RC_EXEC_END__\n"
            "Exiting session...\n"
        ).encode()
        with _mock.patch("subprocess.run") as run:
            run.return_value = _mock.Mock(returncode=0, stdout=fake_stdout, stderr=b"")
            result = provider.exec(ctx, "web", ["echo", "hello world"])
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        # Sentinels must NOT leak into the user-visible stdout.
        assert "__RC_EXEC_BEGIN__" not in result.stdout
        assert "__RC_EXEC_EXIT__" not in result.stdout
        # Verify aws CLI was invoked with the right cluster + task + container.
        cmd = run.call_args.args[0]
        assert "aws" in cmd[0]
        assert "execute-command" in cmd
        assert "--task" in cmd
        assert "arn:aws:ecs:...:task/abc" in cmd

    def test_exec_returns_real_exit_code_from_sentinel(
        self, provider, mock_session, tmp_path
    ):
        from unittest import mock as _mock

        mock_session.client.return_value.list_tasks.return_value = {
            "taskArns": ["arn:task/abc"]
        }
        ctx = _ctx(tmp_path)
        fake_stdout = (
            "__RC_EXEC_BEGIN__\noops\n__RC_EXEC_EXIT__=42\n__RC_EXEC_END__\n"
        ).encode()
        with _mock.patch("subprocess.run") as run:
            run.return_value = _mock.Mock(returncode=0, stdout=fake_stdout, stderr=b"")
            result = provider.exec(ctx, "web", ["false"])
        assert result.exit_code == 42

    def test_exec_handles_session_manager_failure(
        self, provider, mock_session, tmp_path
    ):
        from unittest import mock as _mock

        mock_session.client.return_value.list_tasks.return_value = {
            "taskArns": ["arn:task/abc"]
        }
        ctx = _ctx(tmp_path)
        # No sentinels — session died before our wrapper started.
        with _mock.patch("subprocess.run") as run:
            run.return_value = _mock.Mock(
                returncode=254,
                stdout=b"",
                stderr=b"InvalidParameterException",
            )
            result = provider.exec(ctx, "web", ["whoami"])
        assert result.exit_code == 254
        assert "InvalidParameterException" in result.stderr

    def test_exec_no_running_tasks_returns_error(
        self,
        provider,
        mock_session,
        tmp_path,
        monkeypatch,
    ):
        # ECSProvider.exec polls list_tasks for up to 5 min (rc-e5u.46.6)
        # waiting for a task that has its ExecuteCommandAgent ready. With
        # taskArns=[] forever (mocked) the loop would poll the full budget,
        # so tests MUST set RC_EXEC_WAIT_TIMEOUT_S=0 to short-circuit.
        monkeypatch.setenv("RC_EXEC_WAIT_TIMEOUT_S", "0")
        monkeypatch.setenv("RC_EXEC_WAIT_INTERVAL_S", "0")
        mock_session.client.return_value.list_tasks.return_value = {"taskArns": []}
        ctx = _ctx(tmp_path)
        result = provider.exec(ctx, "web", ["ls"])
        assert result.exit_code == 1
        assert "no running tasks" in result.stderr


class TestExecPicksCorrectTask:
    """rc-0ev: during a force-roll, two RUNNING task sets co-exist
    briefly. provider.exec must skip the OLD task whose
    enableExecuteCommand is False and prefer the NEW task on the
    current task definition revision. Without this filter we hit
    'execute command was not enabled when the task was run'."""

    def test_skips_task_with_enable_execute_command_false(
        self,
        provider,
        mock_session,
        tmp_path,
    ):
        from unittest import mock as _mock

        ecs = mock_session.client.return_value
        old_arn = "arn:aws:ecs:...:task/old"
        new_arn = "arn:aws:ecs:...:task/new"
        ecs.list_tasks.return_value = {"taskArns": [old_arn, new_arn]}
        ecs.describe_services.return_value = {
            "services": [
                {
                    "taskDefinition": "arn:aws:ecs:...:task-definition/web:42",
                }
            ],
        }
        ecs.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": old_arn,
                    "lastStatus": "RUNNING",
                    "enableExecuteCommand": False,
                    "taskDefinitionArn": "arn:aws:ecs:...:task-definition/web:41",
                    "containers": [
                        {
                            "managedAgents": [
                                {
                                    "name": "ExecuteCommandAgent",
                                    "lastStatus": "RUNNING",
                                }
                            ],
                        }
                    ],
                },
                {
                    "taskArn": new_arn,
                    "lastStatus": "RUNNING",
                    "enableExecuteCommand": True,
                    "taskDefinitionArn": "arn:aws:ecs:...:task-definition/web:42",
                    "containers": [
                        {
                            "managedAgents": [
                                {
                                    "name": "ExecuteCommandAgent",
                                    "lastStatus": "RUNNING",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        ctx = _ctx(tmp_path)
        with _mock.patch("subprocess.run") as run:
            run.return_value = _mock.Mock(
                returncode=0,
                stdout=(
                    b"__RC_EXEC_BEGIN__\nok\n__RC_EXEC_EXIT__=0\n__RC_EXEC_END__\n"
                ),
                stderr=b"",
            )
            provider.exec(ctx, "web", ["echo", "ok"])
        cmd = run.call_args.args[0]
        # Must pick the NEW task — not the old one with exec disabled.
        assert new_arn in cmd
        assert old_arn not in cmd

    def test_prefers_current_revision_over_older_exec_ready_task(
        self,
        provider,
        mock_session,
        tmp_path,
    ):
        from unittest import mock as _mock

        ecs = mock_session.client.return_value
        old_arn = "arn:aws:ecs:...:task/old"
        new_arn = "arn:aws:ecs:...:task/new"
        ecs.list_tasks.return_value = {"taskArns": [old_arn, new_arn]}
        ecs.describe_services.return_value = {
            "services": [
                {
                    "taskDefinition": "arn:aws:ecs:...:task-definition/web:42",
                }
            ],
        }
        # Both tasks are exec-ready (enableExecuteCommand=True, agent
        # RUNNING). Old is on rev 41; new is on rev 42 (current). With
        # the rc-0ev fix, current-revision task wins even though both
        # are technically exec-able.
        ecs.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": old_arn,
                    "lastStatus": "RUNNING",
                    "enableExecuteCommand": True,
                    "taskDefinitionArn": "arn:aws:ecs:...:task-definition/web:41",
                    "containers": [
                        {
                            "managedAgents": [
                                {
                                    "name": "ExecuteCommandAgent",
                                    "lastStatus": "RUNNING",
                                }
                            ],
                        }
                    ],
                },
                {
                    "taskArn": new_arn,
                    "lastStatus": "RUNNING",
                    "enableExecuteCommand": True,
                    "taskDefinitionArn": "arn:aws:ecs:...:task-definition/web:42",
                    "containers": [
                        {
                            "managedAgents": [
                                {
                                    "name": "ExecuteCommandAgent",
                                    "lastStatus": "RUNNING",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        ctx = _ctx(tmp_path)
        with _mock.patch("subprocess.run") as run:
            run.return_value = _mock.Mock(
                returncode=0,
                stdout=(
                    b"__RC_EXEC_BEGIN__\nok\n__RC_EXEC_EXIT__=0\n__RC_EXEC_END__\n"
                ),
                stderr=b"",
            )
            provider.exec(ctx, "web", ["echo", "ok"])
        cmd = run.call_args.args[0]
        assert new_arn in cmd
        assert old_arn not in cmd

    def test_falls_back_to_old_revision_when_new_not_ready(
        self,
        provider,
        mock_session,
        tmp_path,
    ):
        # Edge case: the current-revision task hasn't finished registering
        # its ExecuteCommandAgent yet. The old-revision task IS ready.
        # Rather than block lifecycle hooks indefinitely, exec falls
        # back to the old-revision task (which IS exec-able).
        from unittest import mock as _mock

        ecs = mock_session.client.return_value
        old_arn = "arn:aws:ecs:...:task/old-ready"
        new_arn = "arn:aws:ecs:...:task/new-not-ready"
        ecs.list_tasks.return_value = {"taskArns": [old_arn, new_arn]}
        ecs.describe_services.return_value = {
            "services": [
                {
                    "taskDefinition": "arn:aws:ecs:...:task-definition/web:42",
                }
            ],
        }
        ecs.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": old_arn,
                    "lastStatus": "RUNNING",
                    "enableExecuteCommand": True,
                    "taskDefinitionArn": "arn:aws:ecs:...:task-definition/web:41",
                    "containers": [
                        {
                            "managedAgents": [
                                {
                                    "name": "ExecuteCommandAgent",
                                    "lastStatus": "RUNNING",
                                }
                            ],
                        }
                    ],
                },
                {
                    "taskArn": new_arn,
                    "lastStatus": "RUNNING",
                    "enableExecuteCommand": True,
                    "taskDefinitionArn": "arn:aws:ecs:...:task-definition/web:42",
                    "containers": [
                        {
                            "managedAgents": [
                                {
                                    "name": "ExecuteCommandAgent",
                                    "lastStatus": "PENDING",  # Not ready yet
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        ctx = _ctx(tmp_path)
        with _mock.patch("subprocess.run") as run:
            run.return_value = _mock.Mock(
                returncode=0,
                stdout=(
                    b"__RC_EXEC_BEGIN__\nok\n__RC_EXEC_EXIT__=0\n__RC_EXEC_END__\n"
                ),
                stderr=b"",
            )
            provider.exec(ctx, "web", ["echo", "ok"])
        cmd = run.call_args.args[0]
        # Old-revision task is exec-able; pick it rather than waiting.
        assert old_arn in cmd
        assert new_arn not in cmd
