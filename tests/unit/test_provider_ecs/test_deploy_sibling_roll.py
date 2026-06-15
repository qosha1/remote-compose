"""rc deploy (terraform-apply path) must force-roll image-group SIBLINGS, not
just the build owner — parity with the --no-state path (rc-wji.1).

The Django pattern: django owns the shared image; celery-worker / celery-beat are
SIBLINGS (same build identity, different command). _build_and_push_images returns
only the OWNER of each image group, so without sibling expansion the workers keep
running the previous :latest after a deploy (stale code in prod).

The --no-state path already expands owners -> siblings before rolling
(test_no_state_sibling_roll.py); this is its terraform-path twin.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider, roll_targets_for_pushed


def _ctx() -> DeployContext:
    # django + celery-worker + celery-beat share one build identity (one image);
    # django is declared first -> owner. nginx is its own group.
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


def test_deploy_terraform_path_rolls_image_group_siblings():
    """deploy() (terraform path) force-rolls ALL members of every pushed image
    group, not just the owner."""
    provider = ECSProvider()
    rolled: dict = {}

    def fake_build_and_push(ctx, outputs, warnings, **kw):
        # _build_and_push_images returns ONE owner per image group.
        return ["django", "nginx"]

    def fake_force_roll(ctx, services):
        rolled["services"] = list(services)

    with (
        patch.object(provider, "preflight"),
        patch.object(provider, "emit_terraform"),
        patch.object(provider, "_tf_dir", return_value=Path("/tmp/tf")),
        patch.object(provider, "_check_local_state_lock"),
        patch.object(provider, "runner_factory", return_value=MagicMock()),
        patch.object(provider, "_reconcile_orphan_log_groups"),
        patch.object(provider, "_reconcile_orphan_backup_bucket"),
        patch.object(
            provider, "_build_and_push_images", side_effect=fake_build_and_push
        ),
        patch.object(provider, "_force_new_deployments", side_effect=fake_force_roll),
        patch(
            "remote_compose.provider.ecs.provider._revision_id_from_dir",
            return_value="rev-test",
        ),
    ):
        provider.deploy(_ctx())

    # Pushing django (owner) + nginx must roll ALL members of django's image
    # group (celery-beat, celery-worker, django) + nginx — not just the owners.
    assert rolled["services"] == ["celery-beat", "celery-worker", "django", "nginx"]


def test_roll_targets_for_pushed_expands_group_and_excludes_unpushed():
    """The shared helper: a pushed OWNER rolls its whole image group; a group
    whose owner was not pushed is excluded; a singleton rolls only itself."""
    services = _ctx().services
    # Only django (owner of the celery group) pushed -> whole group rolls;
    # nginx (its own group) was NOT pushed -> excluded.
    assert roll_targets_for_pushed(services, ["django"]) == [
        "celery-beat",
        "celery-worker",
        "django",
    ]
    # nginx pushed (its own group) -> only nginx.
    assert roll_targets_for_pushed(services, ["nginx"]) == ["nginx"]
    # nothing pushed -> nothing rolled.
    assert roll_targets_for_pushed(services, []) == []
