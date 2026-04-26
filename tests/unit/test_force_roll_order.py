"""Tests for service-type-ordered force-roll (rc-e5u.46.5).

Cold-start failure: when ALL services force-roll at once, celery workers
race postgres/redis health + django migrations. Workers fail to connect,
ECS exponential backoff stalls them. Solution: order force_new_deployments
by service type — infrastructure → application → worker → proxy.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx(services: dict[str, str]):
    """services maps name -> type (e.g. {'django': 'application'})."""
    return DeployContext(
        project="testp",
        compose_path=Path("/tmp/dc.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-1"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services={
            name: ServiceSpec(name=name, cpu=256, memory=512, type=type_)
            for name, type_ in services.items()
        },
        secrets=[],
    )


def test_infrastructure_rolls_before_application():
    provider = ECSProvider()
    ctx = _ctx({"django": "application", "postgres": "infrastructure"})
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    provider._force_new_deployments(ctx, ["django", "postgres"])

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    # Infrastructure first, then application
    assert calls == ["postgres", "django"]


def test_application_rolls_before_worker():
    provider = ECSProvider()
    ctx = _ctx({"django": "application", "celery-worker": "worker"})
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    provider._force_new_deployments(ctx, ["celery-worker", "django"])

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    assert calls == ["django", "celery-worker"]


def test_worker_rolls_before_proxy():
    provider = ECSProvider()
    ctx = _ctx({"nginx": "proxy", "celery-worker": "worker"})
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    provider._force_new_deployments(ctx, ["nginx", "celery-worker"])

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    assert calls == ["celery-worker", "nginx"]


def test_full_start_simpli_order():
    """The end-to-end test from the start-simpli stack.

    Six services in mixed order; output must be: infra (postgres+redis,
    alpha-sorted within tier) → app (django) → workers (celery-beat,
    celery-worker, alpha-sorted) → proxy (nginx).
    """
    provider = ECSProvider()
    ctx = _ctx({
        "celery-beat": "worker",
        "celery-worker": "worker",
        "django": "application",
        "nginx": "proxy",
        "postgres": "infrastructure",
        "redis": "infrastructure",
    })
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    # Pass services in deliberately-shuffled order to confirm ordering is
    # done by the function, not by caller.
    provider._force_new_deployments(
        ctx,
        ["nginx", "celery-worker", "django", "redis", "celery-beat", "postgres"],
    )

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    assert calls == [
        "postgres", "redis",          # infrastructure
        "django",                     # application
        "celery-beat", "celery-worker",  # worker
        "nginx",                      # proxy
    ]


def test_unknown_type_treated_as_application():
    """A service with an exotic type label still gets a sensible position
    (between infra and worker) — won't break the deploy."""
    provider = ECSProvider()
    ctx = _ctx({
        "weird": "experimental",
        "postgres": "infrastructure",
        "celery": "worker",
    })
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    provider._force_new_deployments(ctx, ["weird", "postgres", "celery"])

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    # postgres → weird (default app priority) → celery
    assert calls == ["postgres", "weird", "celery"]


def test_alpha_sort_within_tier_is_stable():
    """Within a single type tier, alpha order — stable for golden-file
    diffs + reproducible logs."""
    provider = ECSProvider()
    ctx = _ctx({
        "z-worker": "worker",
        "a-worker": "worker",
        "m-worker": "worker",
    })
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    provider._force_new_deployments(
        ctx, ["z-worker", "m-worker", "a-worker"],
    )

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    assert calls == ["a-worker", "m-worker", "z-worker"]


def test_unknown_service_in_pushed_list_uses_default_priority():
    """If the caller passes a service name that ISN'T in ctx.services
    (shouldn't happen but defensive), don't crash — treat as application."""
    provider = ECSProvider()
    ctx = _ctx({"postgres": "infrastructure"})
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    # mystery isn't in ctx.services — should still be force-rolled, just
    # at default-application priority.
    provider._force_new_deployments(ctx, ["mystery", "postgres"])

    calls = [c.kwargs["service"] for c in ecs.update_service.call_args_list]
    assert calls == ["postgres", "mystery"]
