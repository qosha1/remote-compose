"""Unit tests for ECS provider dev_volumes emission (rc-e5u.45.8).

Mirrors test_efs_volumes.py patterns. Verifies:
- ``ctx.dev_mode`` False  → dev_volumes emit nothing (production unaffected).
- ``ctx.dev_mode`` True + dev_volumes → ONE shared EFS file system per
  project, one access point per dev_volumes entry, task-def mounts wired
  in at the declared mount path.
- Persistent ``volumes`` and ``dev_volumes`` coexist on the same service
  without trampling each other's tf state.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict, *, dev_mode: bool = False) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-1",
            "cluster": "test",
            "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
        dev_mode=dev_mode,
    )


# ---------------------------------------------------------------------------
# Production path: dev_volumes are inert when dev_mode is False
# ---------------------------------------------------------------------------

class TestDevModeOff:
    def test_dev_volumes_emit_nothing_when_dev_mode_false(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=False)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        # No dev EFS file system, no dev access points, no DevMode tag.
        efs_tf = (out / "efs.tf").read_text()
        assert "myapp-dev" not in efs_tf
        assert "DevMode" not in efs_tf
        services_tf = (out / "services.tf").read_text()
        assert "dev-django-src" not in services_tf
        # And the django task def has no mountPoints / volume blocks at all.
        assert "mountPoints" not in services_tf

    def test_no_dev_volumes_no_emission(self, tmp_path):
        """A dev-mode deploy with NO services declaring dev_volumes also
        emits no dev EFS — keeps the dev path opt-in per service."""
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert efs_tf.strip() == ""


# ---------------------------------------------------------------------------
# Dev path: shared EFS file system + per-entry access point + task-def mount
# ---------------------------------------------------------------------------

class TestDevModeOn:
    def test_emits_one_shared_efs_per_project(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
            "worker": ServiceSpec(
                name="worker", cpu=256, memory=512, type="worker",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        # Exactly one dev EFS file system, named after the project.
        assert efs_tf.count('aws_efs_file_system" "dev"') == 1
        # The dev FS name IS '<project>-dev' so no var.project doubling.
        assert "myapp-dev" in efs_tf
        assert "${var.project}-myapp-dev" not in efs_tf
        # DevMode tag set so out-of-band tag scans can identify it.
        assert 'DevMode = "true"' in efs_tf

    def test_per_entry_access_point(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[
                    {"name": "src", "source": "./backend", "mount": "/app"},
                    {"name": "tpl", "source": "./templates", "mount": "/app/templates"},
                ],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_access_point" "django__dev_src"' in efs_tf
        assert 'aws_efs_access_point" "django__dev_tpl"' in efs_tf
        # Each access point gets its own root directory under the shared FS.
        assert 'path = "/django__src"' in efs_tf
        assert 'path = "/django__tpl"' in efs_tf

    def test_access_point_has_generic_dev_uid_gid(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        # 1000:1000 is the generic dev default — tweak in a follow-up if a
        # framework-specific uid actually trips users.
        assert "uid = 1000" in efs_tf
        assert "gid = 1000" in efs_tf
        assert 'permissions = "0755"' in efs_tf

    def test_efs_uses_elastic_throughput(self, tmp_path):
        """Elastic suits intermittent rc dev push spikes — bursting would
        exhaust credits on a big initial seed of a Django project."""
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        # Find the dev EFS block (not any persistent one) and check throughput.
        dev_block = efs_tf.split('aws_efs_file_system" "dev"')[1].split("resource ")[0]
        assert 'throughput_mode  = "elastic"' in dev_block

    def test_task_def_mounts_dev_volume_at_declared_path(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        assert "mountPoints" in services_tf
        assert 'containerPath = "/app"' in services_tf
        assert 'sourceVolume  = "dev-django-src"' in services_tf
        # Task def references both the shared FS + the per-entry AP.
        assert "aws_efs_file_system.dev.id" in services_tf
        assert "aws_efs_access_point.django__dev_src.id" in services_tf

    def test_each_dev_mount_gets_unique_volume_name(self, tmp_path):
        """ECS rejects duplicate `volume name=` entries. Two dev mounts on
        one service therefore need distinct sourceVolume names even though
        they share the same EFS file system."""
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[
                    {"name": "src", "source": "./backend", "mount": "/app"},
                    {"name": "tpl", "source": "./templates", "mount": "/app/templates"},
                ],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        # Two distinct sourceVolume names + two volume{} blocks.
        django_block = services_tf.split('resource "aws_ecs_task_definition" "django"')[1]
        django_block = django_block.split("resource ")[0]
        assert django_block.count("name = ") >= 2
        assert 'sourceVolume  = "dev-django-src"' in django_block
        assert 'sourceVolume  = "dev-django-tpl"' in django_block

    def test_stateful_strategy_applied_to_dev_mounted_service(self, tmp_path):
        """Two tasks editing the same code dir on EFS = half-written .pyc
        files + import errors. Force stop-then-start for dev-mounted
        services, same as for stateful EFS volumes."""
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        django_block = services_tf.split('resource "aws_ecs_service" "django"')[1]
        django_block = django_block.split("resource ")[0]
        assert "deployment_minimum_healthy_percent = 0" in django_block
        assert "deployment_maximum_percent         = 100" in django_block


# ---------------------------------------------------------------------------
# Coexistence with persistent volumes: production volume still gets EFS
# regardless of dev mode; dev_volumes ride alongside it.
# ---------------------------------------------------------------------------

class TestPersistentAndDevTogether:
    def test_persistent_and_dev_volumes_coexist(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        # Persistent FS + dev FS coexist (two distinct file systems).
        assert 'aws_efs_file_system" "pgdata"' in efs_tf
        assert 'aws_efs_file_system" "dev"' in efs_tf
        # Persistent volume FS is NOT tagged DevMode — only the dev FS is.
        pg_block = efs_tf.split('aws_efs_file_system" "pgdata"')[1]
        pg_block = pg_block.split("resource ")[0]
        assert "DevMode" not in pg_block

    def test_persistent_volumes_emit_in_production_with_no_dev(self, tmp_path):
        """Production deploy (dev_mode=False): persistent volumes still
        provisioned, nothing emits DevMode artifacts."""
        ctx = _ctx(tmp_path, {
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                # dev_volumes declared but dev_mode is off — must be inert.
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=False)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        assert 'aws_efs_file_system" "pgdata"' in efs_tf
        assert 'aws_efs_file_system" "dev"' not in efs_tf
        assert "DevMode" not in efs_tf


# ---------------------------------------------------------------------------
# Multi-service: every service with dev_volumes gets its own access points
# under one shared file system.
# ---------------------------------------------------------------------------

class TestMultiServiceDevMounts:
    def test_two_services_share_one_dev_efs(self, tmp_path):
        ctx = _ctx(tmp_path, {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
            "celery": ServiceSpec(
                name="celery", cpu=256, memory=512, type="worker",
                dev_volumes=[{"name": "src", "source": "./backend", "mount": "/app"}],
            ),
        }, dev_mode=True)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        efs_tf = (out / "efs.tf").read_text()
        # Exactly ONE dev FS shared by both services (cost optimization).
        assert efs_tf.count('aws_efs_file_system" "dev"') == 1
        # But each service gets its own AP rooted at /<service>__src.
        assert 'aws_efs_access_point" "django__dev_src"' in efs_tf
        assert 'aws_efs_access_point" "celery__dev_src"' in efs_tf
        assert 'path = "/django__src"' in efs_tf
        assert 'path = "/celery__src"' in efs_tf
