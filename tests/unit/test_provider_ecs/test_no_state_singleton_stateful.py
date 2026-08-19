"""The --no-state roll and the terraform emit path must agree on "stateful".

rc-usk0. They did not, and the roll path won on every deploy.

  provider.py, terraform emit:
      singleton = _looks_like_singleton_scheduler(name, spec.command)
      stateful  = (len(svc_mounts) > 0 or singleton or spec.stateful)

  provider.py, _force_new_deployments:
      stateful = bool(getattr(spec, "volumes", None)) if spec else False

The roll path checked VOLUMES ONLY, so a celery-beat with no EFS mount was
force-rolled as an ordinary service at minimumHealthyPercent=100 /
maximumPercent=200 — the exact overlap rc-e5u.46.10 introduced the singleton
heuristic to prevent. Two beat schedulers then run against one broker for the
duration of the roll and double-fire every periodic task.

Observed live on startsimpli-prod 2026-08-19: celery-beat had been corrected to
100/0 by a targeted terraform apply; the next CI deploy (rc deploy --no-state)
reset it to 200/100 AND ran two schedulers for 1m42s while doing so —
  12:36:15  new task started
  12:37:57  old task stopped
So the fix was not merely reverted; the reverting deploy reproduced the bug, and
would have done so on every subsequent deploy.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx(**specs) -> DeployContext:
    return DeployContext(
        project="test-proj",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-2", "cluster": "test-proj-prod"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services=specs,
        secrets=[],
    )


def _roll(ctx, services):
    """Run the --no-state force-roll and return {service: update_service kwargs}."""
    provider = ECSProvider()
    calls: dict = {}
    ecs = MagicMock()

    def update_service(**kwargs):
        calls[kwargs["service"]] = kwargs
        return {}

    ecs.update_service.side_effect = update_service
    session = MagicMock()
    session.client.return_value = ecs

    with (
        patch.object(provider, "session_factory", lambda c: session),
        patch.object(provider, "_wait_for_services_stable", lambda *a, **k: None),
    ):
        provider._force_new_deployments(ctx, services)
    return calls


def _is_stop_then_start(kwargs) -> bool:
    """True when the service was rolled one-at-a-time (no overlap window)."""
    cfg = kwargs.get("deploymentConfiguration")
    if cfg is None:
        return True  # rc omits the key entirely for stateful services
    return cfg.get("maximumPercent") == 100 and cfg.get("minimumHealthyPercent") == 0


def test_celery_beat_without_volumes_is_rolled_stop_then_start():
    """THE REGRESSION. A singleton scheduler has no EFS mount, so a
    volumes-only predicate calls it stateless and overlaps two schedulers."""
    ctx = _ctx(
        **{
            "celery-beat": ServiceSpec(
                name="celery-beat",
                cpu=1,
                memory=1,
                type="worker",
                command=["celery", "-A", "config", "beat"],
            ),
        }
    )

    calls = _roll(ctx, ["celery-beat"])

    assert _is_stop_then_start(calls["celery-beat"]), (
        "celery-beat was force-rolled with an overlap window — two beat "
        f"schedulers run concurrently. got: {calls['celery-beat'].get('deploymentConfiguration')}"
    )


def test_singleton_detected_by_name_alone():
    """The name suffix is enough — the command may be absent or opaque."""
    ctx = _ctx(
        **{
            "nightly-cron": ServiceSpec(
                name="nightly-cron", cpu=1, memory=1, type="worker"
            ),
            "app-scheduler": ServiceSpec(
                name="app-scheduler", cpu=1, memory=1, type="worker"
            ),
        }
    )

    calls = _roll(ctx, ["nightly-cron", "app-scheduler"])

    assert _is_stop_then_start(calls["nightly-cron"])
    assert _is_stop_then_start(calls["app-scheduler"])


def test_ordinary_worker_still_gets_zero_downtime_overlap():
    """The fix must not make everything stop-then-start — a plain worker
    should keep 100/200 + circuit breaker, or deploys get needlessly slower
    and lose their auto-rollback."""
    ctx = _ctx(
        **{
            "celery-worker": ServiceSpec(
                name="celery-worker",
                cpu=1,
                memory=1,
                type="worker",
                command=["celery", "-A", "config", "worker"],
            ),
        }
    )

    cfg = _roll(ctx, ["celery-worker"])["celery-worker"]["deploymentConfiguration"]

    assert cfg["maximumPercent"] == 200
    assert cfg["minimumHealthyPercent"] == 100
    assert cfg["deploymentCircuitBreaker"]["enable"] is True


def test_volume_backed_service_still_stop_then_start():
    """The original volumes-based behaviour must be preserved, not replaced."""
    ctx = _ctx(
        **{
            "postgres": ServiceSpec(
                name="postgres",
                cpu=1,
                memory=1,
                type="database",
                volumes=["pgdata:/var/lib/postgresql/data"],
            ),
        }
    )

    assert _is_stop_then_start(_roll(ctx, ["postgres"])["postgres"])


def test_roll_and_terraform_paths_agree():
    """The actual invariant. Whatever the terraform emit path calls stateful,
    the roll path must call stateful too — otherwise a stack that deploys
    --no-state silently gets different rollout semantics than its own
    terraform declares."""
    from remote_compose.provider.ecs.provider import _looks_like_singleton_scheduler

    cases = {
        "celery-beat": ServiceSpec(
            name="celery-beat",
            cpu=1,
            memory=1,
            type="worker",
            command=["celery", "-A", "config", "beat"],
        ),
        "celery-worker": ServiceSpec(
            name="celery-worker",
            cpu=1,
            memory=1,
            type="worker",
            command=["celery", "-A", "config", "worker"],
        ),
        "app-scheduler": ServiceSpec(
            name="app-scheduler", cpu=1, memory=1, type="worker"
        ),
        "postgres": ServiceSpec(
            name="postgres",
            cpu=1,
            memory=1,
            type="database",
            volumes=["pgdata:/var/lib/postgresql/data"],
        ),
        "nginx": ServiceSpec(name="nginx", cpu=1, memory=1, type="proxy"),
    }
    ctx = _ctx(**cases)
    calls = _roll(ctx, list(cases))

    for name, spec in cases.items():
        terraform_says_stateful = bool(
            (spec.volumes or [])
            or _looks_like_singleton_scheduler(name, spec.command)
            or getattr(spec, "stateful", False)
        )
        roll_says_stateful = _is_stop_then_start(calls[name])
        assert roll_says_stateful == terraform_says_stateful, (
            f"{name}: terraform stateful={terraform_says_stateful} but the "
            f"--no-state roll treated it as stateful={roll_says_stateful}"
        )
