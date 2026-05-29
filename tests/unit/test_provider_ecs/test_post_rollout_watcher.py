"""rc-8zz: post-rollout error watcher. After force-roll, polls ECS
service events for IAM/secret/ECR placement errors and surfaces them
clearly so the user doesn't learn about a stuck deploy hours later.

start-simpli case: new task defs referenced auto-discovered SM secrets
but ecsTaskExecutionRole lacked GetSecretValue → tasks failed with
ResourceInitializationError. Without this watcher, the user only
noticed when manually inspecting service events.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock


from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path) -> DeployContext:
    return DeployContext(
        project="watch-test",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-1",
                "cluster": "watch-test-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "django": ServiceSpec(
                name="django",
                cpu=256,
                memory=512,
                type="application",
                image="x:latest",
            ),
        },
        secrets=[],
    )


def _setup_client(*, pre_events=None, post_events=None):
    """Build a mock ECS client where describe_services returns one set
    of events the first call (pre-roll baseline) and a different set
    on subsequent calls (the watcher's polls).
    """
    client = mock.MagicMock()
    client.update_service.return_value = {}
    call_count = {"n": 0}

    def fake_describe(cluster, services):
        call_count["n"] += 1
        if call_count["n"] == 1:
            events = pre_events or []
        else:
            events = post_events or []
        return {
            "services": [
                {
                    "serviceName": services[0],
                    "events": events,
                }
            ],
        }

    client.describe_services.side_effect = fake_describe
    return client


class TestPostRolloutWatcher:
    def test_iam_error_surfaced(self, tmp_path, monkeypatch):
        # Tight budget so test is fast.
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "3")
        events: list[str] = []
        client = _setup_client(
            pre_events=[{"id": "old-1", "message": "task started"}],
            post_events=[
                {"id": "old-1", "message": "task started"},
                {
                    "id": "new-1",
                    "message": (
                        "ResourceInitializationError: unable to retrieve secret "
                        "from asm: User: arn:aws:sts::111:assumed-role/"
                        "ecsTaskExecutionRole is not authorized to perform: "
                        "secretsmanager:GetSecretValue on resource: "
                        "watch-test/django"
                    ),
                },
            ],
        )
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])

        joined = "\n".join(events)
        assert "post-rollout placement errors detected" in joined
        assert "django" in joined
        assert "ResourceInitializationError" in joined
        assert "secretsmanager:GetSecretValue" in joined
        assert "ecsTaskExecutionRole" in joined

    def test_clean_rollout_no_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "2")
        events: list[str] = []
        client = _setup_client(
            pre_events=[],
            post_events=[
                {"id": "new-1", "message": "service django has reached a steady state"},
            ],
        )
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        joined = "\n".join(events)
        assert "post-rollout placement errors" not in joined

    def test_can_be_disabled_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "0")
        events: list[str] = []
        client = _setup_client(
            pre_events=[],
            post_events=[
                {"id": "new-1", "message": "ResourceInitializationError: x"},
            ],
        )
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        # Watcher disabled → no warning even though error events exist.
        joined = "\n".join(events)
        assert "post-rollout placement errors" not in joined

    def test_flap_loop_detected_health_checks_failed(self, tmp_path, monkeypatch):
        """rc-8vb: a service whose new tasks fail ALB health checks
        immediately (3+ unhealthy events in watch window) is flapping —
        usually because grace period is too short OR the app crashes
        on startup. Surface a flap diagnosis distinct from the
        IAM/secret/ECR placement-error message."""
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "3")
        events: list[str] = []
        client = _setup_client(
            pre_events=[],
            post_events=[
                {
                    "id": "ev-1",
                    "message": (
                        "(service django) (task abc) (port 8000) is unhealthy in "
                        "(target-group arn:aws:...) due to (reason Health checks failed)."
                    ),
                },
                {
                    "id": "ev-2",
                    "message": (
                        "(service django) has stopped 1 running tasks: (task abc)."
                    ),
                },
                {
                    "id": "ev-3",
                    "message": (
                        "(service django) (task def) (port 8000) is unhealthy in "
                        "(target-group arn:aws:...) due to (reason Health checks failed)."
                    ),
                },
                {
                    "id": "ev-4",
                    "message": (
                        "(service django) has stopped 1 running tasks: (task def)."
                    ),
                },
                {
                    "id": "ev-5",
                    "message": (
                        "(service django) (task ghi) (port 8000) is unhealthy in "
                        "(target-group arn:aws:...) due to (reason Health checks failed)."
                    ),
                },
            ],
        )
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        joined = "\n".join(events)
        assert "flap" in joined.lower()
        assert "django" in joined
        assert "health_check_grace_period" in joined or "grace period" in joined

    def test_single_health_check_failure_not_flagged_as_flap(
        self, tmp_path, monkeypatch
    ):
        """One unhealthy event during a normal rolling deploy isn't a
        flap — could be a stale task draining. Need 3+ to declare flap."""
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "2")
        events: list[str] = []
        client = _setup_client(
            pre_events=[],
            post_events=[
                {
                    "id": "ev-1",
                    "message": (
                        "(service django) (task abc) is unhealthy in (target-group ...)."
                    ),
                },
                {
                    "id": "ev-2",
                    "message": (
                        "(service django) registered 1 targets in (target-group ...)."
                    ),
                },
            ],
        )
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        joined = "\n".join(events)
        assert "flap" not in joined.lower()

    def test_aws_error_at_baseline_warns_not_silent(self, tmp_path, monkeypatch):
        """rc-x19: previously, an AWS error during pre-roll baseline
        silently disabled the watcher (silent return). Now we emit a
        warning so the user knows post-rollout diagnostics won't run."""
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "2")
        events: list[str] = []
        client = mock.MagicMock()

        def boom(*args, **kwargs):
            raise RuntimeError("throttled")

        client.describe_services.side_effect = boom
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        joined = "\n".join(events)
        assert "post-rollout watcher disabled" in joined
        assert "throttled" in joined

    def test_aws_error_mid_watch_warns_not_silent(self, tmp_path, monkeypatch):
        """rc-x19: same for transient errors mid-watch loop. Don't go
        silent — explain why the watch ended early."""
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "5")
        events: list[str] = []
        client = mock.MagicMock()
        call_count = {"n": 0}

        def fake_describe(cluster, services):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Baseline succeeds.
                return {"services": [{"serviceName": services[0], "events": []}]}
            # Subsequent polls fail.
            raise RuntimeError("network blip")

        client.describe_services.side_effect = fake_describe
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        joined = "\n".join(events)
        assert "describe_services failed mid-poll" in joined
        assert "network blip" in joined

    def test_pre_existing_errors_not_re_surfaced(self, tmp_path, monkeypatch):
        # An IAM error event from a previous deploy that's still in the
        # service-events log shouldn't trigger the watcher.
        monkeypatch.setenv("RC_POST_ROLLOUT_WATCH_S", "2")
        events: list[str] = []
        client = _setup_client(
            pre_events=[
                {
                    "id": "stale-1",
                    "message": ("ResourceInitializationError: ancient failure"),
                },
            ],
            post_events=[
                {
                    "id": "stale-1",
                    "message": ("ResourceInitializationError: ancient failure"),
                },
                {"id": "new-1", "message": "task started ok"},
            ],
        )
        provider = ECSProvider(
            session_factory=lambda c: mock.Mock(client=lambda *_a, **_kw: client),
            progress=events.append,
        )
        provider._force_new_deployments(_ctx(tmp_path), ["django"])
        joined = "\n".join(events)
        # Only NEW events (post-rollout) are flagged. The pre-existing
        # stale-1 was in the baseline → ignored.
        assert "post-rollout placement errors" not in joined
