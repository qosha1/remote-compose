"""--no-state deploy must rebuild + roll image-group SIBLINGS, not just the
owner (the django/celery-* staleness bug).

Two behaviours:
  1. owner->repo fallback: when the image-group owner's own ECR repo name
     doesn't exist (the live repo was named after a different group member
     at cutover), the owner resolves to a sibling's existing repo so the
     shared image still rebuilds + pushes where the task defs reference it.
  2. sibling roll: when an owner's image is pushed, EVERY member of its
     image group is force-rolled (so siblings pick up the new :latest).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx() -> DeployContext:
    # django + celery-worker + celery-beat share one build identity (one
    # image). nginx is its own group. django is declared first -> owner.
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
                name="celery-worker", cpu=1, memory=1, type="worker", **app_build
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


def _ecr_session():
    """A session whose ECR paginator lists repos for celery-beat + nginx
    only — NOT django (so the owner must fall back to celery-beat)."""
    ecr = MagicMock()
    page = {
        "repositories": [
            {
                "repositoryName": "test-proj/celery-beat",
                "repositoryUri": "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/celery-beat",
            },
            {
                "repositoryName": "test-proj/nginx",
                "repositoryUri": "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/nginx",
            },
        ]
    }
    paginator = MagicMock()
    paginator.paginate.return_value = [page]
    ecr.get_paginator.return_value = paginator
    session = MagicMock()
    session.client.return_value = ecr
    return session


def test_owner_repo_fallback_and_sibling_roll():
    provider = ECSProvider()
    captured: dict = {}

    def fake_build_and_push(ctx, outputs, warnings, **kw):
        captured["outputs"] = outputs
        # owners that "pushed" successfully
        return ["django", "nginx"]

    rolled: dict = {}

    def fake_force_roll(ctx, services):
        rolled["services"] = list(services)

    with (
        patch.object(provider, "session_factory", lambda c: _ecr_session()),
        patch.object(
            provider, "_build_and_push_images", side_effect=fake_build_and_push
        ),
        patch.object(provider, "_force_new_deployments", side_effect=fake_force_roll),
    ):
        provider._deploy_no_state(_ctx(), None, None)

    # 1. owner->repo fallback: django (owner, no own repo) resolved to the
    #    live celery-beat repo.
    repo_urls = captured["outputs"]["ecr_repositories"]["value"]
    assert repo_urls["django"].endswith("/test-proj/celery-beat")

    # 2. sibling roll: pushing django (owner) rolls ALL members of its group
    #    + nginx (its own group). Not partial.
    assert rolled["services"] == ["celery-beat", "celery-worker", "django", "nginx"]


def test_owner_with_own_repo_still_resolves_directly():
    """Regression: when the owner DOES have its own repo, it's used (the
    fallback only kicks in when the own repo is absent)."""
    provider = ECSProvider()
    captured: dict = {}

    ecr = MagicMock()
    ecr.get_paginator.return_value.paginate.return_value = [
        {
            "repositories": [
                {
                    "repositoryName": "test-proj/django",
                    "repositoryUri": "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/django",
                },
                {
                    "repositoryName": "test-proj/nginx",
                    "repositoryUri": "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/nginx",
                },
            ]
        }
    ]
    session = MagicMock()
    session.client.return_value = ecr

    def fake_build_and_push(ctx, outputs, warnings, **kw):
        captured["outputs"] = outputs
        return []

    with (
        patch.object(provider, "session_factory", lambda c: session),
        patch.object(
            provider, "_build_and_push_images", side_effect=fake_build_and_push
        ),
        patch.object(provider, "_force_new_deployments"),
    ):
        provider._deploy_no_state(_ctx(), None, None)

    assert captured["outputs"]["ecr_repositories"]["value"]["django"].endswith(
        "/test-proj/django"
    )
