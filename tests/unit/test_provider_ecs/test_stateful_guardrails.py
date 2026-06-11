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
