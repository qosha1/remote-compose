"""Moto-backed integration tests for ECSProvider lifecycle methods.

Moto simulates the AWS API at full response-shape fidelity. These tests
verify the ECS provider's status/redeploy/logs/exec methods work against
realistic boto3 responses — tighter than the MagicMock-based unit tests
in test_lifecycle.py.

No real AWS credentials are used. Moto intercepts boto3 calls and keeps
state in-memory.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


pytestmark = pytest.mark.integration


def _ctx(tmp_path: Path) -> DeployContext:
    return DeployContext(
        project="moto-test",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-2",
            "cluster": "moto-cluster",
            "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(name="web", cpu=256, memory=512, type="proxy",
                               public=True, port=80, replicas=2),
            "api": ServiceSpec(name="api", cpu=512, memory=1024,
                               type="application", replicas=1),
        },
        secrets=[],
    )


def _session_factory():
    """Session factory that works inside mock_aws scope."""
    return lambda ctx: boto3.Session(region_name="us-west-2")


def _seed_cluster_and_services(cluster: str, services: dict[str, int]) -> None:
    """Create a cluster and populate services with desired task counts."""
    ecs = boto3.client("ecs", region_name="us-west-2")
    ecs.create_cluster(clusterName=cluster)
    for svc_name, running in services.items():
        task_def = ecs.register_task_definition(
            family=f"moto-test-{svc_name}",
            containerDefinitions=[{
                "name": svc_name,
                "image": "alpine:latest",
                "essential": True,
                "cpu": 256,
                "memory": 512,
            }],
            networkMode="bridge",
            requiresCompatibilities=["EC2"],
        )["taskDefinition"]
        ecs.create_service(
            cluster=cluster,
            serviceName=svc_name,
            taskDefinition=task_def["taskDefinitionArn"],
            desiredCount=running,
            launchType="EC2",
        )


class TestStatusAgainstMoto:
    @mock_aws
    def test_status_reports_services_from_live_api(self, tmp_path):
        _seed_cluster_and_services("moto-cluster", {"web": 2, "api": 1})
        provider = ECSProvider(session_factory=_session_factory())
        report = provider.status(_ctx(tmp_path))

        by_name = {s.name for s in report.services}
        assert by_name == {"web", "api"}
        web = next(s for s in report.services if s.name == "web")
        assert web.desired == 2

    @mock_aws
    def test_status_handles_missing_services(self, tmp_path):
        ecs = boto3.client("ecs", region_name="us-west-2")
        ecs.create_cluster(clusterName="moto-cluster")
        # only 'web' seeded; 'api' is in the rc.yml but not in the cluster
        _seed_cluster_and_services("moto-cluster", {"web": 1})

        provider = ECSProvider(session_factory=_session_factory())
        report = provider.status(_ctx(tmp_path))

        by_name = {s.name: s for s in report.services}
        assert "api" in by_name
        assert by_name["api"].health == "unknown"
        assert by_name["api"].desired == 0


class TestRedeployAgainstMoto:
    @mock_aws
    def test_redeploy_calls_update_service_per_service(self, tmp_path):
        _seed_cluster_and_services("moto-cluster", {"web": 1, "api": 1})
        provider = ECSProvider(session_factory=_session_factory())
        result = provider.redeploy(_ctx(tmp_path))

        assert set(result.services) == {"web", "api"}
        # moto doesn't actually track forceNewDeployment, but the call
        # succeeds (no exception) — that's the contract we care about.

    @mock_aws
    def test_redeploy_subset(self, tmp_path):
        _seed_cluster_and_services("moto-cluster", {"web": 1, "api": 1})
        provider = ECSProvider(session_factory=_session_factory())
        result = provider.redeploy(_ctx(tmp_path), services=["api"])
        assert result.services == ["api"]


class TestLogsAgainstMoto:
    @mock_aws
    def test_logs_describe_streams_does_not_raise(self, tmp_path):
        """Regression for the bug moto caught: describe_log_streams cannot
        combine logStreamNamePrefix with orderBy. The fixed implementation
        sorts client-side and must complete without raising."""
        logs = boto3.client("logs", region_name="us-west-2")
        logs.create_log_group(logGroupName="/ecs/moto-test")
        logs.create_log_stream(
            logGroupName="/ecs/moto-test",
            logStreamName="web/main/abc123",
        )

        provider = ECSProvider(session_factory=_session_factory())
        # The API call must not raise InvalidParameterException.
        out = list(provider.logs(_ctx(tmp_path), "web", tail=10))
        assert isinstance(out, list)  # may be empty; moto's event store is flaky

    @mock_aws
    def test_logs_empty_when_no_streams(self, tmp_path):
        logs = boto3.client("logs", region_name="us-west-2")
        logs.create_log_group(logGroupName="/ecs/moto-test")
        provider = ECSProvider(session_factory=_session_factory())
        out = list(provider.logs(_ctx(tmp_path), "web"))
        assert out == []


class TestExecAgainstMoto:
    @mock_aws
    def test_exec_no_tasks_returns_error(self, tmp_path, monkeypatch):
        # See ECSProvider.exec: poll budget defaults to 5 min via
        # RC_EXEC_WAIT_TIMEOUT_S. Tests that mock empty taskArns MUST
        # zero this out or the test hangs for 5 min.
        monkeypatch.setenv("RC_EXEC_WAIT_TIMEOUT_S", "0")
        monkeypatch.setenv("RC_EXEC_WAIT_INTERVAL_S", "0")
        _seed_cluster_and_services("moto-cluster", {"web": 0})
        provider = ECSProvider(session_factory=_session_factory())
        result = provider.exec(_ctx(tmp_path), "web", ["ls"])
        assert result.exit_code == 1
        assert "no running tasks" in result.stderr
