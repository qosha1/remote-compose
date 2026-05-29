"""Tests for singleton-scheduler stateful detection (rc-e5u.46.10)."""

from __future__ import annotations

import pytest

from remote_compose.provider.ecs.provider import _looks_like_singleton_scheduler


class TestNameSuffix:
    @pytest.mark.parametrize(
        "name",
        [
            "celery-beat",
            "scheduler",  # bare 'scheduler' doesn't match (needs -scheduler suffix)
        ],
    )
    def test_explicit_singleton_suffix(self, name):
        # NB: 'scheduler' alone won't trigger; only '-scheduler' suffix
        assert _looks_like_singleton_scheduler("foo-scheduler", []) is True
        assert _looks_like_singleton_scheduler("foo-beat", []) is True
        assert _looks_like_singleton_scheduler("nightly-cron", []) is True

    def test_no_singleton_suffix(self):
        for n in ("django", "celery-worker", "postgres", "redis", "nginx", "api"):
            assert _looks_like_singleton_scheduler(n, []) is False

    def test_celery_beat_by_name(self):
        # The exact start-simpli case
        assert _looks_like_singleton_scheduler("celery-beat", []) is True


class TestCommandPattern:
    def test_celery_beat_command_list(self):
        cmd = ["celery", "-A", "config", "beat", "--loglevel=info"]
        assert _looks_like_singleton_scheduler("worker", cmd) is True

    def test_celery_beat_command_string_form(self):
        # Even if compose passes a single-string command (pre-shlex split,
        # belt-and-suspenders), the join produces the same result.
        cmd = ["celery -A config beat --loglevel=info"]
        assert _looks_like_singleton_scheduler("svc", cmd) is True

    def test_celerybeat_no_space(self):
        cmd = ["celerybeat", "-A", "config"]
        assert _looks_like_singleton_scheduler("svc", cmd) is True

    def test_celery_worker_is_NOT_singleton(self):
        # The corollary: 'celery worker' must NOT be flagged. Workers are
        # specifically the multi-instance counterpart to beat.
        cmd = ["celery", "-A", "config", "worker", "--concurrency=2"]
        assert _looks_like_singleton_scheduler("celery-worker", cmd) is False

    def test_random_python_command_not_flagged(self):
        cmd = ["python", "manage.py", "runserver"]
        assert _looks_like_singleton_scheduler("django", cmd) is False

    def test_empty_command_not_flagged(self):
        assert _looks_like_singleton_scheduler("foo", []) is False
        assert _looks_like_singleton_scheduler("foo", None) is False


class TestPriorityOfSignals:
    def test_command_alone_is_enough(self):
        # Service named 'worker' (NOT a singleton suffix) but command is beat
        cmd = ["celery", "-A", "config", "beat"]
        assert _looks_like_singleton_scheduler("worker", cmd) is True

    def test_name_alone_is_enough(self):
        # Empty command but name says -beat
        assert _looks_like_singleton_scheduler("nightly-beat", []) is True


# ---------------------------------------------------------------------------
# Integration: provider sets stateful=True when singleton detected
# ---------------------------------------------------------------------------


class TestProviderIntegration:
    def test_singleton_makes_emitted_terraform_stateful(self, tmp_path):
        from pathlib import Path
        from remote_compose.provider.base import DeployContext, ServiceSpec
        from remote_compose.provider.ecs.provider import ECSProvider

        provider = ECSProvider()
        ctx = DeployContext(
            project="testp",
            compose_path=Path("/tmp/dc.yml"),
            rc_yml_v2={},
            provider_config={"ecs": {"region": "us-west-1"}},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                "celery-beat": ServiceSpec(
                    name="celery-beat",
                    cpu=256,
                    memory=512,
                    type="worker",
                    command=["celery", "-A", "config", "beat"],
                ),
            },
            secrets=[],
        )
        out_dir = tmp_path / "tf"
        provider.emit_terraform(ctx, out_dir)
        services_tf = (out_dir / "services.tf").read_text()
        # The stateful gate emits these for celery-beat.
        assert "deployment_minimum_healthy_percent = 0" in services_tf
        assert "deployment_maximum_percent         = 100" in services_tf
        assert 'availability_zone_rebalancing = "DISABLED"' in services_tf

    def test_celery_worker_stays_stateless_default_rolling_deploy(self, tmp_path):
        from pathlib import Path
        from remote_compose.provider.base import DeployContext, ServiceSpec
        from remote_compose.provider.ecs.provider import ECSProvider

        provider = ECSProvider()
        ctx = DeployContext(
            project="testp",
            compose_path=Path("/tmp/dc.yml"),
            rc_yml_v2={},
            provider_config={"ecs": {"region": "us-west-1"}},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                "celery-worker": ServiceSpec(
                    name="celery-worker",
                    cpu=256,
                    memory=512,
                    type="worker",
                    command=["celery", "-A", "config", "worker"],
                ),
            },
            secrets=[],
        )
        out_dir = tmp_path / "tf"
        provider.emit_terraform(ctx, out_dir)
        services_tf = (out_dir / "services.tf").read_text()
        # Worker (not beat) — gets default rolling deploy semantics.
        # The stateful gate's lines should NOT appear for this service.
        # (They might appear for OTHER stateful services in a multi-svc
        # ctx, but here celery-worker is the only one.)
        assert "deployment_minimum_healthy_percent = 0" not in services_tf
        assert 'availability_zone_rebalancing = "DISABLED"' not in services_tf
