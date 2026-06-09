"""Zero-downtime rolling deploys: container health check (readiness) +
deployment config / circuit breaker + the post-roll steady-state wait.

These close the worker-staleness/readiness gap: with min-healthy=100 and a
real container healthCheck, ECS keeps old worker tasks until the new ones
are actually ready (not just RUNNING), so a crawl never lands on a window
with no available worker.
"""

from __future__ import annotations

from unittest import mock

import pytest

from remote_compose.config._schema_types import ConfigError, HealthCheckV2
from remote_compose.provider.base import DeployContext, ProviderError, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx(tmp_path, services) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-2", "cluster": "myapp-prod"}},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


class TestHealthCheckEmit:
    def test_container_health_check_is_emitted(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "celery-browser": ServiceSpec(
                    name="celery-browser",
                    cpu=512,
                    memory=1024,
                    type="worker",
                    health_check={
                        "command": [
                            "CMD",
                            "celery",
                            "-A",
                            "config",
                            "inspect",
                            "ping",
                        ],
                        "interval": 30,
                        "timeout": 10,
                        "retries": 3,
                        "start_period": 90,
                    },
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        tf = (out / "services.tf").read_text()
        assert "healthCheck = {" in tf
        assert '"celery"' in tf and '"ping"' in tf
        assert "startPeriod = 90" in tf

    def test_no_health_check_emits_none(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": ServiceSpec(name="worker", cpu=256, memory=512, type="worker")},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert "healthCheck = {" not in (out / "services.tf").read_text()


class TestForceRollDeploymentConfig:
    def test_force_roll_sets_circuit_breaker_for_non_stateful(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RC_DEPLOY_WAIT_S", "0")  # skip the wait in this test
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "0")
        ecs = mock.MagicMock()
        session = mock.MagicMock()
        session.client.return_value = ecs
        provider = ECSProvider(session_factory=lambda c: session)
        ctx = _ctx(
            tmp_path,
            {"worker": ServiceSpec(name="worker", cpu=256, memory=512, type="worker")},
        )

        provider._force_new_deployments(ctx, ["worker"])

        kw = ecs.update_service.call_args.kwargs
        assert kw["forceNewDeployment"] is True
        dc = kw["deploymentConfiguration"]
        assert dc["minimumHealthyPercent"] == 100
        assert dc["maximumPercent"] == 200
        assert dc["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}

    def test_force_roll_no_circuit_breaker_for_stateful(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RC_DEPLOY_WAIT_S", "0")
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "0")
        ecs = mock.MagicMock()
        session = mock.MagicMock()
        session.client.return_value = ecs
        provider = ECSProvider(session_factory=lambda c: session)
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=256,
                    memory=512,
                    type="infrastructure",
                    volumes=[{"name": "pg", "mount": "/data"}],
                )
            },
        )

        provider._force_new_deployments(ctx, ["postgres"])

        kw = ecs.update_service.call_args.kwargs
        assert kw["forceNewDeployment"] is True
        # Stateful: no zero-downtime config override (single-task EFS roll).
        assert "deploymentConfiguration" not in kw


class TestWaitForStable:
    def _provider(self):
        return ECSProvider(session_factory=lambda c: mock.MagicMock())

    def _client(self, *, stable):
        ecs = mock.MagicMock()
        dep = {"status": "PRIMARY", "rolloutState": "COMPLETED"}
        ecs.describe_services.return_value = {
            "services": [
                {
                    "deployments": [dep] if stable else [dep, {"status": "ACTIVE"}],
                    "runningCount": 3,
                    "desiredCount": 3,
                }
            ]
        }
        return ecs

    def test_returns_when_stable(self, monkeypatch):
        monkeypatch.setenv("RC_DEPLOY_WAIT_S", "30")
        monkeypatch.setenv("RC_DEPLOY_WAIT_INTERVAL_S", "0")
        provider = self._provider()
        # Should return promptly (no raise) since the service is stable.
        provider._wait_for_services_stable(self._client(stable=True), "c", ["worker"])

    def test_raises_on_timeout(self, monkeypatch):
        monkeypatch.setenv("RC_DEPLOY_WAIT_S", "1")
        monkeypatch.setenv("RC_DEPLOY_WAIT_INTERVAL_S", "0")
        provider = self._provider()
        with pytest.raises(ProviderError, match="did not stabilize"):
            provider._wait_for_services_stable(
                self._client(stable=False), "c", ["worker"]
            )

    def test_zero_budget_skips(self, monkeypatch):
        monkeypatch.setenv("RC_DEPLOY_WAIT_S", "0")
        provider = self._provider()
        ecs = mock.MagicMock()
        provider._wait_for_services_stable(ecs, "c", ["worker"])
        ecs.describe_services.assert_not_called()


class TestHealthCheckValidation:
    def test_valid(self):
        HealthCheckV2(command=["CMD", "true"]).validate()

    def test_command_must_be_nonempty_list(self):
        with pytest.raises(ConfigError, match="non-empty list"):
            HealthCheckV2(command=None).validate()

    def test_command_must_start_with_cmd_form(self):
        with pytest.raises(ConfigError, match="CMD"):
            HealthCheckV2(command=["celery", "ping"]).validate()
