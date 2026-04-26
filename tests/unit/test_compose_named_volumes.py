"""Tests for compose named-volume auto-EFS promotion (rc-e5u.46.11).

When a compose service has a named-volume mount (``foo:/bar``) AND the
service looks like a singleton scheduler (celery-beat, *-scheduler, ...),
build_deploy_context auto-promotes the mount into a ServiceSpec.volumes
entry. The provider already knows how to turn that into an EFS access
point, so the singleton boots with persistent /bar instead of crashing
on missing-directory errors (the start-simpli celery-beat failure mode
verified 2026-04-26).

Stateless multi-instance services (celery-worker, plain web servers) do
NOT get this treatment — losing state across replicas is fine for them
and we don't want to surprise users with EFS for every compose volume.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import (
    _compose_named_volume_mounts,
    build_deploy_context,
    load_rc_yml,
)


# ---------------------------------------------------------------------------
# _compose_named_volume_mounts
# ---------------------------------------------------------------------------


class TestComposeNamedVolumeMounts:
    def test_short_form_named_volume(self):
        out = _compose_named_volume_mounts(
            {"volumes": ["celery-beat-schedule:/celery-beat"]}
        )
        assert out == [{"name": "celery-beat-schedule", "mount": "/celery-beat"}]

    def test_short_form_with_mode_suffix(self):
        # ``foo:/path:ro`` — mode suffix is ignored, mount is the middle.
        out = _compose_named_volume_mounts({"volumes": ["data:/var/lib/db:ro"]})
        assert out == [{"name": "data", "mount": "/var/lib/db"}]

    def test_short_form_bind_mount_skipped(self):
        # leading ``./`` is a bind mount — never auto-EFS.
        out = _compose_named_volume_mounts({"volumes": ["./local:/app"]})
        assert out == []

    def test_short_form_absolute_path_bind_mount_skipped(self):
        out = _compose_named_volume_mounts({"volumes": ["/host/path:/app"]})
        assert out == []

    def test_short_form_home_bind_mount_skipped(self):
        out = _compose_named_volume_mounts({"volumes": ["~/data:/app"]})
        assert out == []

    def test_short_form_anonymous_volume_skipped(self):
        # Just ``/path`` (no source segment) → anonymous, not a named volume.
        out = _compose_named_volume_mounts({"volumes": ["/var/lib/data"]})
        assert out == []

    def test_long_form_volume(self):
        out = _compose_named_volume_mounts({"volumes": [
            {"type": "volume", "source": "data", "target": "/var/lib/data"},
        ]})
        assert out == [{"name": "data", "mount": "/var/lib/data"}]

    def test_long_form_default_type_treated_as_volume(self):
        # docker-compose treats missing 'type' as 'volume'.
        out = _compose_named_volume_mounts({"volumes": [
            {"source": "data", "target": "/var/lib/data"},
        ]})
        assert out == [{"name": "data", "mount": "/var/lib/data"}]

    def test_long_form_bind_skipped(self):
        out = _compose_named_volume_mounts({"volumes": [
            {"type": "bind", "source": "./local", "target": "/app"},
        ]})
        assert out == []

    def test_long_form_tmpfs_skipped(self):
        out = _compose_named_volume_mounts({"volumes": [
            {"type": "tmpfs", "target": "/tmp"},
        ]})
        assert out == []

    def test_no_volumes_key(self):
        assert _compose_named_volume_mounts({}) == []

    def test_volumes_not_list(self):
        assert _compose_named_volume_mounts({"volumes": None}) == []

    def test_mixed_named_and_bind(self):
        out = _compose_named_volume_mounts({"volumes": [
            "./local:/app",
            "data:/var/lib/data",
            "/host:/etc/host",
        ]})
        assert out == [{"name": "data", "mount": "/var/lib/data"}]


# ---------------------------------------------------------------------------
# build_deploy_context — auto-promotion behavior
# ---------------------------------------------------------------------------


def _write_rc_yml_v2(tmp_path: Path, services: dict, **overrides) -> Path:
    """Helper: write a minimal v2 rc.yml + matching compose.yml. Returns rc.yml path."""
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(yaml.safe_dump({
        "version": "3.9",
        "services": services,
    }))
    rc = {
        "version": 2,
        "project": "test-46-11",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {"region": "us-west-2"}},
        "terraform": {"backend": {"type": "local"}},
        "services": overrides.get("rc_services", {}),
    }
    rc_path = tmp_path / "rc.yml"
    rc_path.write_text(yaml.safe_dump(rc))
    return rc_path


class TestSingletonAutoEFSPromotion:
    def test_celery_beat_named_mount_auto_promoted(self, tmp_path):
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "celery-beat": {
                "image": "myapp:latest",
                "command": ["celery", "-A", "config", "beat"],
                "volumes": ["celery-beat-schedule:/celery-beat"],
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        spec = ctx.services["celery-beat"]
        assert spec.volumes == [
            {"name": "celery-beat-schedule", "mount": "/celery-beat"}
        ]

    def test_dash_scheduler_suffix_triggers_promotion(self, tmp_path):
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "nightly-scheduler": {
                "image": "scheduler:1",
                "volumes": ["sched-state:/state"],
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        assert ctx.services["nightly-scheduler"].volumes == [
            {"name": "sched-state", "mount": "/state"}
        ]

    def test_celery_worker_does_not_get_auto_efs(self, tmp_path):
        # The corollary: a non-singleton (celery WORKER) does NOT get
        # named-volume auto-promotion. Workers are multi-instance; EFS
        # would be wrong by default.
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "celery-worker": {
                "image": "myapp:latest",
                "command": ["celery", "-A", "config", "worker"],
                "volumes": ["worker-cache:/var/cache/celery"],
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        # No rc.yml-declared volumes + non-singleton → empty.
        assert ctx.services["celery-worker"].volumes == []

    def test_bind_mounts_never_promoted(self, tmp_path):
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "celery-beat": {
                "image": "myapp:latest",
                "command": ["celery", "-A", "config", "beat"],
                "volumes": ["./backend:/app"],  # bind mount
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        # Bind mount → silently dropped (existing rc behaviour); singleton
        # does NOT get one created from a non-named source.
        assert ctx.services["celery-beat"].volumes == []

    def test_explicit_rc_yml_volumes_take_precedence(self, tmp_path):
        # When rc.yml services.<name>.volumes is set, auto-EFS does NOT
        # second-guess it. User's escape hatch wins.
        rc_path = _write_rc_yml_v2(
            tmp_path,
            services={
                "celery-beat": {
                    "image": "myapp:latest",
                    "command": ["celery", "-A", "config", "beat"],
                    "volumes": ["compose-volume:/different-path"],
                },
            },
            rc_services={
                "celery-beat": {
                    "cpu": 256,
                    "memory": 512,
                    "type": "worker",
                    "volumes": [{"name": "rc-yml-volume", "mount": "/explicit"}],
                },
            },
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        # rc.yml volumes wins; the compose named volume is NOT merged in.
        assert ctx.services["celery-beat"].volumes == [
            {"name": "rc-yml-volume", "mount": "/explicit"}
        ]

    def test_compose_only_service_singleton_promoted(self, tmp_path):
        # Service NOT in rc.yml services{} → uses the compose-only fallback
        # branch. Same behavior: singleton + named mount → auto-promote.
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "celery-beat": {
                "image": "myapp:latest",
                "command": ["celery", "-A", "config", "beat"],
                "volumes": ["beat-state:/celery-beat"],
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        spec = ctx.services["celery-beat"]
        assert spec.volumes == [{"name": "beat-state", "mount": "/celery-beat"}]

    def test_singleton_with_no_volumes_unchanged(self, tmp_path):
        # Singleton without any compose volumes → no spurious volume.
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "celery-beat": {
                "image": "myapp:latest",
                "command": ["celery", "-A", "config", "beat"],
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        assert ctx.services["celery-beat"].volumes == []


class TestProviderEmitsEFSForAutoPromotedVolume:
    """End-to-end: a celery-beat compose service with a named-volume mount
    flows through cli_v2 → provider.emit_terraform and produces an EFS
    file system + access point + task-def mount, exactly the same shape as
    if the user had hand-declared services.celery-beat.volumes in rc.yml."""

    def test_celery_beat_named_volume_lands_in_terraform(self, tmp_path):
        rc_path = _write_rc_yml_v2(tmp_path, services={
            "celery-beat": {
                "image": "myapp:latest",
                "command": ["celery", "-A", "config", "beat"],
                "volumes": ["celery-beat-schedule:/celery-beat"],
            },
        })
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)

        from remote_compose.provider.ecs.provider import ECSProvider
        out_dir = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out_dir)
        efs_tf = (out_dir / "efs.tf").read_text()
        services_tf = (out_dir / "services.tf").read_text()

        # EFS file system + access point exist for the auto-promoted volume.
        assert "celery_beat_schedule" in efs_tf or "celery-beat-schedule" in efs_tf
        # Mount path lands in the task def.
        assert "/celery-beat" in services_tf
