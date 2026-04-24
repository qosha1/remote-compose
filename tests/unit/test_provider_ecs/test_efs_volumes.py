"""Unit tests for ECS EFS volume support (Phase 6b.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-2",
            "cluster": "test",
            "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


class TestNoVolumes:
    def test_efs_tf_empty_when_no_volumes(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "web": ServiceSpec(name="web", cpu=256, memory=512, type="application"),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert (out / "efs.tf").read_text().strip() == ""

    def test_task_def_has_no_volume_block_when_no_mounts(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "web": ServiceSpec(name="web", cpu=256, memory=512, type="application"),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "efs_volume_configuration" not in services
        assert "mountPoints" not in services


class TestSingleVolume:
    def test_efs_file_system_rendered(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_file_system" "pgdata"' in efs_tf
        # encrypted defaults to false (rc-e5u.26); production overrides.
        assert "encrypted      = false" in efs_tf

    def test_mount_targets_match_task_subnets(self, tmp_path):
        """Mount targets must live in the same subnets as tasks.
        Tasks currently run in public subnets (rc-e5u.25)."""
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_mount_target" "pgdata"' in efs_tf
        assert "count           = length(aws_subnet.public)" in efs_tf

    def test_access_point_per_service_volume_pair(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_access_point" "postgres__pgdata"' in efs_tf

    def test_efs_security_group_allows_nfs_from_tasks(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_security_group" "efs"' in efs_tf
        assert "from_port       = 2049" in efs_tf
        assert "security_groups = [aws_security_group.tasks.id]" in efs_tf


class TestTaskDefIntegration:
    def test_task_def_includes_volume_block(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "efs_volume_configuration" in services
        assert "aws_efs_file_system.pgdata.id" in services
        assert "aws_efs_access_point.postgres__pgdata.id" in services

    def test_task_def_mountpoint_matches_volume(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "mountPoints" in services
        assert 'containerPath = "/var/lib/postgresql/data"' in services
        assert 'sourceVolume  = "pgdata"' in services

    def test_transit_encryption_enabled(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/data"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert 'transit_encryption = "ENABLED"' in services


class TestSharedVolume:
    def test_two_services_mount_same_volume_share_file_system(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "writer": ServiceSpec(
                name="writer", cpu=256, memory=512, type="worker",
                volumes=[{"name": "shared", "mount": "/data"}],
            ),
            "reader": ServiceSpec(
                name="reader", cpu=256, memory=512, type="worker",
                volumes=[{"name": "shared", "mount": "/data-ro"}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert efs_tf.count('aws_efs_file_system" "shared"') == 1
        # one access point per (service, volume) pair
        assert 'aws_efs_access_point" "writer__shared"' in efs_tf
        assert 'aws_efs_access_point" "reader__shared"' in efs_tf


class TestMultipleVolumesPerService:
    def test_service_can_mount_multiple_distinct_volumes(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "app": ServiceSpec(
                name="app", cpu=512, memory=1024, type="application",
                volumes=[
                    {"name": "data", "mount": "/var/data"},
                    {"name": "cache", "mount": "/var/cache"},
                ],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        services = (out / "services.tf").read_text()
        assert 'aws_efs_file_system" "data"' in efs_tf
        assert 'aws_efs_file_system" "cache"' in efs_tf
        assert services.count("mountPoints") == 1  # one block with 2 entries
        assert 'containerPath = "/var/data"' in services
        assert 'containerPath = "/var/cache"' in services


class TestValidation:
    def test_volume_missing_name_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "x": ServiceSpec(name="x", cpu=256, memory=512, type="application",
                             volumes=[{"mount": "/data"}]),
        })
        with pytest.raises(ProviderConfigError, match="name"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_volume_missing_mount_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "x": ServiceSpec(name="x", cpu=256, memory=512, type="application",
                             volumes=[{"name": "data"}]),
        })
        with pytest.raises(ProviderConfigError, match="mount"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestPosixUserOverride:
    """EFS access point posix_user is uid=1000/gid=1000 by default; the
    uid/gid keys on a volume entry override so containers running as a
    non-standard user (postgres=70, redis=999, etc.) can actually use
    the mount. See rc-e5u.27."""

    def test_default_is_1000(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "app": ServiceSpec(name="app", cpu=256, memory=512, type="application",
                               volumes=[{"name": "data", "mount": "/data"}]),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs = (out / "efs.tf").read_text()
        assert "uid = 1000" in efs
        assert "gid = 1000" in efs
        assert 'permissions = "0755"' in efs

    def test_uid_gid_override(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=256, memory=512, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data",
                          "uid": 70, "gid": 70}],
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs = (out / "efs.tf").read_text()
        assert "uid = 70" in efs
        assert "gid = 70" in efs

    def test_mode_override(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "app": ServiceSpec(name="app", cpu=256, memory=512, type="application",
                               volumes=[{"name": "data", "mount": "/data",
                                          "mode": "0700"}]),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert 'permissions = "0700"' in (out / "efs.tf").read_text()

    def test_non_integer_uid_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "x": ServiceSpec(name="x", cpu=256, memory=512, type="application",
                             volumes=[{"name": "d", "mount": "/d", "uid": "seventy"}]),
        })
        with pytest.raises(ProviderConfigError, match="uid/gid must be integers"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_invalid_mode_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "x": ServiceSpec(name="x", cpu=256, memory=512, type="application",
                             volumes=[{"name": "d", "mount": "/d", "mode": "755"}]),
        })
        with pytest.raises(ProviderConfigError, match="POSIX octal"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestStatefulDeploymentStrategy:
    """EFS-mounting services must stop-then-start to avoid two tasks
    concurrently mounting the same data dir during forceNewDeployment."""

    def test_stateful_service_has_min_0_max_100(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=256, memory=512, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data",
                          "uid": 70, "gid": 70}],
            ),
            "stateless": ServiceSpec(name="stateless", cpu=256, memory=512,
                                     type="application"),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()

        postgres_block = services.split('resource "aws_ecs_service" "postgres"')[1]
        postgres_block = postgres_block.split("resource ")[0]
        assert "deployment_minimum_healthy_percent = 0" in postgres_block
        assert "deployment_maximum_percent         = 100" in postgres_block

        stateless_block = services.split('resource "aws_ecs_service" "stateless"')[1]
        stateless_block = stateless_block.split("resource ")[0] \
            if "resource " in stateless_block else stateless_block
        assert "deployment_minimum_healthy_percent" not in stateless_block
