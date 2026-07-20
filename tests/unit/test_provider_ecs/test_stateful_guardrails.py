"""Stateful volume guardrails (rc-kr7).

An EFS-backed service mounts a single access point. rc sets
deployment_minimum_healthy_percent=0 so a ROLL never overlaps two tasks on the
same data dir — but that doesn't stop a user from declaring replicas>1, which
runs N tasks concurrently against the same EFS dir (postgres initdb / sqlite /
any single-writer engine = corruption). Reject it at emit time with a clear
error instead of silently shipping a data-loss config.

Stateless services (no volumes) scale freely — unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider


def _emit(tmp_path: Path, *, replicas: int, volumes: list[dict]) -> None:
    ctx = DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-2",
                "cluster": "app-prod",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "db": ServiceSpec(
                name="db",
                cpu=512,
                memory=1024,
                type="infrastructure",
                replicas=replicas,
                volumes=volumes,
            ),
        },
        secrets=[],
    )
    ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestEfsReplicasGuard:
    def test_efs_volume_with_replicas_gt1_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="replicas|EFS|corrupt"):
            _emit(
                tmp_path,
                replicas=2,
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            )

    def test_efs_volume_with_replicas_1_ok(self, tmp_path):
        # single task on the data dir is the supported stateful shape
        _emit(
            tmp_path,
            replicas=1,
            volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
        )

    def test_stateless_service_scales_freely(self, tmp_path):
        _emit(tmp_path, replicas=3, volumes=[])

    def test_shared_volume_allows_replicas_gt1(self, tmp_path):
        # A shared-scratch EFS where each task writes its own subdir (e.g.
        # browser-mgr's recordings) is a valid multi-writer pattern — opt in
        # with shared:true so replicas>1 is allowed.
        _emit(
            tmp_path,
            replicas=2,
            volumes=[
                {"name": "recordings", "mount": "/app/recordings", "shared": True}
            ],
        )

    def test_mixed_shared_and_unshared_still_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="replicas|EFS|corrupt"):
            _emit(
                tmp_path,
                replicas=2,
                volumes=[
                    {"name": "recordings", "mount": "/app/recordings", "shared": True},
                    {"name": "pgdata", "mount": "/var/lib/postgresql/data"},
                ],
            )


# rc: explicit `stateful: true` opt-in for single-instance services rc's
# heuristics miss (a volume-less redis broker/cache — two overlapping tasks
# split-brain). Forces stop-before-start (min=0/max=100, AZ-rebalancing off).
def _emit_svc(tmp_path: Path, name: str, *, stateful: bool) -> str:
    ctx = DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-2",
                "cluster": "app-prod",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            name: ServiceSpec(
                name=name,
                cpu=256,
                memory=512,
                type="infrastructure",
                image="redis:7",
                stateful=stateful,
            )
        },
        secrets=[],
    )
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(ctx, out)
    return (out / "services.tf").read_text()


class TestExplicitStatefulOptIn:
    def test_stateful_true_forces_stop_before_start(self, tmp_path):
        tf = _emit_svc(tmp_path, "redis", stateful=True)
        assert "deployment_minimum_healthy_percent = 0" in tf
        assert "deployment_maximum_percent         = 100" in tf

    def test_volumeless_default_is_rolling(self, tmp_path):
        # Backward-compat: stateful=False (default) volume-less service is NOT
        # forced to stop-before-start.
        tf = _emit_svc(tmp_path, "redis", stateful=False)
        assert "deployment_minimum_healthy_percent = 0" not in tf
