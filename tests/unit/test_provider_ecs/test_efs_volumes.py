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
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "test",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


class TestNoVolumes:
    def test_efs_tf_empty_when_no_volumes(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "web": ServiceSpec(name="web", cpu=256, memory=512, type="application"),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert (out / "efs.tf").read_text().strip() == ""

    def test_task_def_has_no_volume_block_when_no_mounts(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "web": ServiceSpec(name="web", cpu=256, memory=512, type="application"),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "efs_volume_configuration" not in services
        assert "mountPoints" not in services


class TestSingleVolume:
    def test_efs_file_system_rendered(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_file_system" "pgdata"' in efs_tf
        # encrypted defaults to false (rc-e5u.26); production overrides.
        assert "encrypted      = false" in efs_tf

    def test_mount_targets_match_task_subnets(self, tmp_path):
        """Mount targets must live in the same subnets as tasks.
        Tasks currently run in public subnets (rc-e5u.25)."""
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_mount_target" "pgdata"' in efs_tf
        # rc-e5u.46.9: switched from length(aws_subnet.public) to a static
        # local so terraform import can validate the module on a fresh-
        # state machine (length-of-managed-resource isn't known then).
        assert "count           = local.public_subnet_count" in efs_tf

    def test_access_point_per_service_volume_pair(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_access_point" "postgres__pgdata"' in efs_tf

    def test_efs_security_group_allows_nfs_from_tasks(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_security_group" "efs"' in efs_tf
        assert "from_port       = 2049" in efs_tf
        assert "security_groups = [aws_security_group.tasks.id]" in efs_tf


class TestTaskDefIntegration:
    def test_task_def_includes_volume_block(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "efs_volume_configuration" in services
        assert "aws_efs_file_system.pgdata.id" in services
        assert "aws_efs_access_point.postgres__pgdata.id" in services

    def test_task_def_mountpoint_matches_volume(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "mountPoints" in services
        assert 'containerPath = "/var/lib/postgresql/data"' in services
        assert 'sourceVolume  = "pgdata"' in services

    def test_transit_encryption_enabled(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[{"name": "pgdata", "mount": "/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert 'transit_encryption = "ENABLED"' in services


class TestEfsIamAuth:
    """efs_iam_auth toggles IAM authorization on the mount. Default DISABLED
    (rc-created EFS); set true for an adopted EFS whose file-system policy
    requires IAM (e.g. Copilot's CopilotEFSPolicy)."""

    def _ctx_iam(self, tmp_path, iam_auth):
        vol = {"name": "pgdata", "mount": "/data", "uid": 999, "gid": 999}
        if iam_auth is not None:
            vol["efs_iam_auth"] = iam_auth
        return _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[vol],
                ),
            },
        )

    def test_default_is_disabled(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._ctx_iam(tmp_path, None), out)
        assert 'iam             = "DISABLED"' in (out / "services.tf").read_text()

    def test_enabled_when_set(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._ctx_iam(tmp_path, True), out)
        assert 'iam             = "ENABLED"' in (out / "services.tf").read_text()


class TestExistingEfsReuse:
    """Adopt-in-place: a volume that names an existing efs_id + access_point_id
    must NOT create a new EFS / access point — the task-def mount references the
    existing ids verbatim (so 1.6 TB of live data isn't orphaned)."""

    def _existing_ctx(self, tmp_path):
        return _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[
                        {
                            "name": "pgdata",
                            "mount": "/var/lib/postgresql/data",
                            "uid": 999,
                            "gid": 999,
                            "efs_id": "fs-06640134a4cdcb8ba",
                            "access_point_id": "fsap-0a06a6e435f0e31ad",
                        }
                    ],
                ),
            },
        )

    def test_no_efs_resources_created_for_existing(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._existing_ctx(tmp_path), out)
        efs = (out / "efs.tf").read_text()
        # No created file system / mount target / access point / efs SG.
        assert "aws_efs_file_system" not in efs
        assert "aws_efs_mount_target" not in efs
        assert "aws_efs_access_point" not in efs
        assert 'resource "aws_security_group" "efs"' not in efs
        # The existing ids are referenced in the explanatory comment.
        assert "fs-06640134a4cdcb8ba" in efs

    def test_task_def_mount_references_existing_ids(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._existing_ctx(tmp_path), out)
        services = (out / "services.tf").read_text()
        assert 'file_system_id     = "fs-06640134a4cdcb8ba"' in services
        assert 'access_point_id = "fsap-0a06a6e435f0e31ad"' in services
        assert 'transit_encryption = "ENABLED"' in services
        # No terraform resource refs for this volume.
        assert "aws_efs_file_system.pgdata.id" not in services
        assert "aws_efs_access_point.postgres__pgdata.id" not in services

    def test_mixed_existing_and_created_volumes(self, tmp_path):
        """One existing volume + one rc-created volume coexist: rc creates the
        SG + the created volume's resources, skips the existing one."""
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[
                        {
                            "name": "pgdata",
                            "mount": "/data",
                            "uid": 999,
                            "gid": 999,
                            "efs_id": "fs-existing",
                            "access_point_id": "fsap-existing",
                        }
                    ],
                ),
                "cache": ServiceSpec(
                    name="cache",
                    cpu=256,
                    memory=512,
                    type="infrastructure",
                    volumes=[{"name": "scratch", "mount": "/scratch"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs = (out / "efs.tf").read_text()
        # created volume gets a file system + the shared efs SG; existing one doesn't.
        assert 'resource "aws_efs_file_system" "scratch"' in efs
        assert 'resource "aws_efs_file_system" "pgdata"' not in efs
        assert 'resource "aws_security_group" "efs"' in efs

    def test_access_point_id_without_efs_id_rejected(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[
                        {
                            "name": "pgdata",
                            "mount": "/data",
                            "access_point_id": "fsap-orphan",  # no efs_id
                        }
                    ],
                ),
            },
        )
        with pytest.raises(
            ProviderConfigError, match="access_point_id requires efs_id"
        ):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestSharedVolume:
    def test_two_services_mount_same_volume_share_file_system(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "writer": ServiceSpec(
                    name="writer",
                    cpu=256,
                    memory=512,
                    type="worker",
                    volumes=[{"name": "shared", "mount": "/data"}],
                ),
                "reader": ServiceSpec(
                    name="reader",
                    cpu=256,
                    memory=512,
                    type="worker",
                    volumes=[{"name": "shared", "mount": "/data-ro"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert efs_tf.count('aws_efs_file_system" "shared"') == 1
        # one access point per (service, volume) pair
        assert 'aws_efs_access_point" "writer__shared"' in efs_tf
        assert 'aws_efs_access_point" "reader__shared"' in efs_tf


class TestMultipleVolumesPerService:
    def test_service_can_mount_multiple_distinct_volumes(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "app": ServiceSpec(
                    name="app",
                    cpu=512,
                    memory=1024,
                    type="application",
                    volumes=[
                        {"name": "data", "mount": "/var/data"},
                        {"name": "cache", "mount": "/var/cache"},
                    ],
                ),
            },
        )
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
        ctx = _ctx(
            tmp_path,
            {
                "x": ServiceSpec(
                    name="x",
                    cpu=256,
                    memory=512,
                    type="application",
                    volumes=[{"mount": "/data"}],
                ),
            },
        )
        with pytest.raises(ProviderConfigError, match="name"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_volume_missing_mount_rejected(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "x": ServiceSpec(
                    name="x",
                    cpu=256,
                    memory=512,
                    type="application",
                    volumes=[{"name": "data"}],
                ),
            },
        )
        with pytest.raises(ProviderConfigError, match="mount"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestPosixUserOverride:
    """EFS access point posix_user is uid=1000/gid=1000 by default; the
    uid/gid keys on a volume entry override so containers running as a
    non-standard user (postgres=70, redis=999, etc.) can actually use
    the mount. See rc-e5u.27."""

    def test_default_is_1000(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "app": ServiceSpec(
                    name="app",
                    cpu=256,
                    memory=512,
                    type="application",
                    volumes=[{"name": "data", "mount": "/data"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs = (out / "efs.tf").read_text()
        assert "uid = 1000" in efs
        assert "gid = 1000" in efs
        assert 'permissions = "0755"' in efs

    def test_uid_gid_override(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=256,
                    memory=512,
                    type="infrastructure",
                    volumes=[
                        {
                            "name": "pgdata",
                            "mount": "/var/lib/postgresql/data",
                            "uid": 70,
                            "gid": 70,
                        }
                    ],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs = (out / "efs.tf").read_text()
        assert "uid = 70" in efs
        assert "gid = 70" in efs

    def test_mode_override(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "app": ServiceSpec(
                    name="app",
                    cpu=256,
                    memory=512,
                    type="application",
                    volumes=[{"name": "data", "mount": "/data", "mode": "0700"}],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert 'permissions = "0700"' in (out / "efs.tf").read_text()

    def test_non_integer_uid_rejected(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "x": ServiceSpec(
                    name="x",
                    cpu=256,
                    memory=512,
                    type="application",
                    volumes=[{"name": "d", "mount": "/d", "uid": "seventy"}],
                ),
            },
        )
        with pytest.raises(ProviderConfigError, match="uid/gid must be integers"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_invalid_mode_rejected(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "x": ServiceSpec(
                    name="x",
                    cpu=256,
                    memory=512,
                    type="application",
                    volumes=[{"name": "d", "mount": "/d", "mode": "755"}],
                ),
            },
        )
        with pytest.raises(ProviderConfigError, match="POSIX octal"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestStatefulDeploymentStrategy:
    """EFS-mounting services must stop-then-start to avoid two tasks
    concurrently mounting the same data dir during forceNewDeployment."""

    def test_stateful_service_has_min_0_max_100(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=256,
                    memory=512,
                    type="infrastructure",
                    volumes=[
                        {
                            "name": "pgdata",
                            "mount": "/var/lib/postgresql/data",
                            "uid": 70,
                            "gid": 70,
                        }
                    ],
                ),
                "stateless": ServiceSpec(
                    name="stateless", cpu=256, memory=512, type="application"
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()

        postgres_block = services.split('resource "aws_ecs_service" "postgres"')[1]
        postgres_block = postgres_block.split("resource ")[0]
        assert "deployment_minimum_healthy_percent = 0" in postgres_block
        assert "deployment_maximum_percent         = 100" in postgres_block

        stateless_block = services.split('resource "aws_ecs_service" "stateless"')[1]
        stateless_block = (
            stateless_block.split("resource ")[0]
            if "resource " in stateless_block
            else stateless_block
        )
        # Non-stateful services get the zero-downtime rollout config: keep
        # 100% of old tasks until new are healthy, up to 200%, + circuit
        # breaker. (Stateful stays 0/100 for the single-task EFS case above.)
        assert "deployment_minimum_healthy_percent = 100" in stateless_block
        assert "deployment_maximum_percent         = 200" in stateless_block
        assert "deployment_circuit_breaker {" in stateless_block

    def test_stateful_service_disables_availability_zone_rebalancing(self, tmp_path):
        """rc-e5u.45.11: ECS API rejects deploy_max_pct<=100 combined with
        availability_zone_rebalancing=ENABLED (the new ECS default). AZ
        rebalancing actively redistributes tasks across AZs, which is the
        OPPOSITE of what a stateful EFS-mounting workload wants — so
        emit availability_zone_rebalancing=DISABLED on stateful services
        and leave it at the AWS default (ENABLED) on stateless ones."""
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=256,
                    memory=512,
                    type="infrastructure",
                    volumes=[
                        {
                            "name": "pgdata",
                            "mount": "/var/lib/postgresql/data",
                            "uid": 70,
                            "gid": 70,
                        }
                    ],
                ),
                "stateless": ServiceSpec(
                    name="stateless", cpu=256, memory=512, type="application"
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()

        postgres_block = services.split('resource "aws_ecs_service" "postgres"')[1]
        postgres_block = postgres_block.split("resource ")[0]
        assert 'availability_zone_rebalancing = "DISABLED"' in postgres_block

        stateless_block = services.split('resource "aws_ecs_service" "stateless"')[1]
        stateless_block = (
            stateless_block.split("resource ")[0]
            if "resource " in stateless_block
            else stateless_block
        )
        # Stateless services keep the AWS default (ENABLED) — we don't
        # emit the field at all so terraform doesn't fight the default.
        assert "availability_zone_rebalancing" not in stateless_block


class TestEfsBackupPolicy:
    """rc-56bq.1 / startsim-36qr.

    aws_efs_file_system has no backup argument, so an EFS can look completely
    declared and still have zero recovery points -- and nothing surfaces that.
    startsimpli-prod's postgres ran that way for months, discovered only when
    someone tried to take a backup before a risky migration.

    Opt-in for now, NOT because the feature is risky but because
    elasticfilesystem:PutBackupPolicy is granted nowhere -- see rc-56bq.2.
    """

    def _ctx_vol(self, tmp_path, volume):
        return _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[volume],
                ),
            },
        )

    def test_off_by_default(self, tmp_path):
        """Default-on would AccessDenied at apply on every existing stack."""
        ctx = self._ctx_vol(tmp_path, {"name": "pgdata", "mount": "/data"})
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert "aws_efs_backup_policy" not in efs_tf
        assert 'aws_efs_file_system" "pgdata"' in efs_tf

    def test_rendered_when_the_volume_opts_in(self, tmp_path):
        ctx = self._ctx_vol(
            tmp_path, {"name": "pgdata", "mount": "/data", "efs_backups": True}
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_backup_policy" "pgdata"' in efs_tf
        assert 'status = "ENABLED"' in efs_tf
        assert "file_system_id = aws_efs_file_system.pgdata.id" in efs_tf

    def test_opt_in_is_per_volume(self, tmp_path):
        """A stateful volume can be backed up while scratch is not."""
        ctx = _ctx(
            tmp_path,
            {
                "postgres": ServiceSpec(
                    name="postgres",
                    cpu=512,
                    memory=1024,
                    type="infrastructure",
                    volumes=[
                        {"name": "pgdata", "mount": "/data", "efs_backups": True},
                        {"name": "scratch", "mount": "/scratch"},
                    ],
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_backup_policy" "pgdata"' in efs_tf
        assert 'aws_efs_backup_policy" "scratch"' not in efs_tf
