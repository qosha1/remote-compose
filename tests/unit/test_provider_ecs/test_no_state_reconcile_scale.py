"""--no-state deploy must reconcile ECS desiredCount to rc.yml replicas
(rc-wji.2).

The terraform path already sets desired_count = svc.replicas (services.tf.j2);
--no-state skips terraform, so without this it force-rolls images but leaves the
running task count alone — rc.yml `replicas: 3` is a lie until someone runs
`aws ecs update-service --desired-count` by hand (sentinal celery-worker /
celery-browser). Decision (rc-wji.2.1): --no-state ALWAYS reconciles.

The terraform deploy() path is unchanged — terraform still owns desired_count
there (covered in test_force_roll_order.py, which calls _force_new_deployments
with no reconcile and asserts no desiredCount).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx() -> DeployContext:
    # django (owner, replicas=1) + celery-worker (sibling, replicas=3) +
    # celery-beat (sibling, replicas=1) share one image; nginx own group.
    app_build = dict(build_context=Path("/app"), dockerfile="compose/Dockerfile")
    return DeployContext(
        project="test-proj",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-1", "cluster": "test-proj-prod"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services={
            "django": ServiceSpec(
                name="django", cpu=1, memory=1, type="application", **app_build
            ),
            "celery-worker": ServiceSpec(
                name="celery-worker",
                cpu=1,
                memory=1,
                type="worker",
                replicas=3,
                **app_build,
            ),
            "celery-beat": ServiceSpec(
                name="celery-beat", cpu=1, memory=1, type="worker", **app_build
            ),
            "nginx": ServiceSpec(
                name="nginx",
                cpu=1,
                memory=1,
                type="proxy",
                build_context=Path("/nginx"),
                dockerfile="nginx/Dockerfile",
            ),
        },
        secrets=[],
    )


def _session_with_repos() -> MagicMock:
    """One mock backing both the ECR repo-discovery paginator and the ECS
    update_service call (session.client returns the same mock for any arg)."""
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "repositories": [
                {
                    "repositoryName": f"test-proj/{name}",
                    "repositoryUri": (
                        f"111.dkr.ecr.us-west-1.amazonaws.com/test-proj/{name}"
                    ),
                }
                for name in ("django", "celery-worker", "celery-beat", "nginx")
            ]
        }
    ]
    session = MagicMock()
    session.client.return_value = client
    return session


def test_no_state_roll_reconciles_desired_count_to_replicas():
    provider = ECSProvider()
    session = _session_with_repos()

    def fake_build_and_push(ctx, outputs, warnings, **kw):
        return ["django", "nginx"]  # owners

    with (
        patch.object(provider, "session_factory", lambda c: session),
        patch.object(
            provider, "_build_and_push_images", side_effect=fake_build_and_push
        ),
    ):
        provider._deploy_no_state(_ctx(), None, None)

    ecs = session.client.return_value
    counts = {
        c.kwargs["service"]: c.kwargs.get("desiredCount")
        for c in ecs.update_service.call_args_list
    }
    # rc.yml replicas applied: celery-worker -> 3; the rest default to 1.
    assert counts["celery-worker"] == 3
    assert counts["django"] == 1
    assert counts["celery-beat"] == 1
    assert counts["nginx"] == 1


def test_default_roll_leaves_desired_count_alone():
    """Regression: without reconcile_scale (the terraform deploy() path),
    update_service omits desiredCount — terraform owns the count there."""
    provider = ECSProvider()
    ecs = MagicMock()
    session = MagicMock()
    session.client.return_value = ecs
    provider.session_factory = lambda c: session

    provider._force_new_deployments(_ctx(), ["celery-worker", "django"])

    assert ecs.update_service.call_args_list  # sanity: it did roll
    for c in ecs.update_service.call_args_list:
        assert "desiredCount" not in c.kwargs
