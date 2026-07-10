"""Shared-image build dedup (rc-44i).

When N services share one build (context + dockerfile + target + args) — the
standard Django pattern where django + celery-worker/beat/flower all run the
SAME image, differing only by command — rc must build + push that image ONCE to
ONE ECR repo, not N times to N repos (ECR stores layer blobs per-repo, so N
repos = N full uploads; on a slow uplink that's hours).

Design: services are grouped by build identity; the alphabetically-first
service in each group OWNS the ECR repo; siblings' task defs reference the
owner's image. General + additive: a service whose build is unique (or which
uses a pre-built image) is its own owner, so single-build / image-only stacks
emit exactly as before (guarded by test_golden.py).
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import (
    image_group_owners,
    _services_to_build,
)


def _build_svc(name, ctx="/app", dockerfile=None, target=None, args=None):
    return ServiceSpec(
        name=name,
        cpu=256,
        memory=512,
        build_context=ctx,
        dockerfile=dockerfile,
        target=target,
        build_args=args or {},
    )


def _image_svc(name, image="redis:7"):
    return ServiceSpec(name=name, cpu=256, memory=512, image=image)


# ── grouping helper (rc-y7l) ────────────────────────────────────────────────
class TestImageGroupOwners:
    def test_identical_builds_share_one_owner(self):
        svcs = {
            "django": _build_svc("django", dockerfile="D"),
            "celery-worker": _build_svc("celery-worker", dockerfile="D"),
            "celery-beat": _build_svc("celery-beat", dockerfile="D"),
        }
        owners = image_group_owners(svcs)
        # owner = first-declared in the group = "django"
        assert owners == {
            "django": "django",
            "celery-worker": "django",
            "celery-beat": "django",
        }

    def test_distinct_dockerfile_separate_owners(self):
        svcs = {
            "django": _build_svc("django", dockerfile="D"),
            "browser": _build_svc("browser", dockerfile="D2"),
        }
        owners = image_group_owners(svcs)
        assert owners["django"] == "django"
        assert owners["browser"] == "browser"

    def test_build_args_distinguish_groups(self):
        svcs = {
            "a": _build_svc("a", args={"X": "1"}),
            "b": _build_svc("b", args={"X": "2"}),
        }
        owners = image_group_owners(svcs)
        assert owners["a"] == "a" and owners["b"] == "b"

    def test_target_distinguishes_groups(self):
        svcs = {
            "a": _build_svc("a", target="prod"),
            "b": _build_svc("b", target="dev"),
        }
        owners = image_group_owners(svcs)
        assert owners["a"] == "a" and owners["b"] == "b"

    def test_image_only_services_not_grouped(self):
        svcs = {"redis": _image_svc("redis")}
        owners = image_group_owners(svcs)
        assert "redis" not in owners  # no build_context -> no image repo group


# ── build-loop dedup: build only owners (rc-nw0) ────────────────────────────
class TestServicesToBuild:
    def _shared(self):
        return {
            "django": _build_svc("django", dockerfile="D"),
            "celery-worker": _build_svc("celery-worker", dockerfile="D"),
            "celery-beat": _build_svc("celery-beat", dockerfile="D"),
            "redis": _image_svc("redis"),  # no build -> never built
        }

    def test_only_owner_built_for_shared_group(self):
        names = [s.name for s in _services_to_build(self._shared())]
        assert names == ["django"]  # 1 build for the whole Django group

    def test_image_only_service_not_built(self):
        names = [s.name for s in _services_to_build(self._shared())]
        assert "redis" not in names

    def test_filter_on_sibling_builds_owner(self):
        names = [
            s.name
            for s in _services_to_build(
                self._shared(), services_filter=["celery-worker"]
            )
        ]
        assert names == ["django"]  # sibling filter -> owner rebuilds

    def test_distinct_builds_all_built(self):
        svcs = {
            "django": _build_svc("django", dockerfile="A"),
            "browser": _build_svc("browser", dockerfile="B"),
        }
        names = sorted(s.name for s in _services_to_build(svcs))
        assert names == ["browser", "django"]


# ── emission: one ECR repo per build group, siblings reference owner (rc-cvc) ─
def _ctx(tmp_path: Path, services) -> DeployContext:
    return DeployContext(
        project="mesh",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {"region": "us-east-2", "cluster": "mesh", "vpc_cidr": "10.0.0.0/16"}
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


class TestSharedImageEmission:
    def _shared_services(self):
        return {
            "django": _build_svc(
                "django", ctx="/app", dockerfile="compose/django/Dockerfile"
            ),
            "celery-worker": _build_svc(
                "celery-worker", ctx="/app", dockerfile="compose/django/Dockerfile"
            ),
            "celery-beat": _build_svc(
                "celery-beat", ctx="/app", dockerfile="compose/django/Dockerfile"
            ),
        }

    def test_one_ecr_repo_for_shared_build(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, self._shared_services()), out)
        services_tf = (out / "services.tf").read_text()
        # exactly one aws_ecr_repository (the owner = django, first-declared)
        assert services_tf.count('resource "aws_ecr_repository" "django"') == 1
        assert 'resource "aws_ecr_repository" "celery_beat"' not in services_tf
        assert 'resource "aws_ecr_repository" "celery_worker"' not in services_tf

    def test_siblings_reference_owner_image(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, self._shared_services()), out)
        services_tf = (out / "services.tf").read_text()
        # every service's task-def image points at the owner repo (django)
        assert services_tf.count("aws_ecr_repository.django.repository_url") == 3
        assert "aws_ecr_repository.celery_beat.repository_url" not in services_tf

    def test_distinct_builds_keep_own_repos(self, tmp_path):
        svcs = {
            "django": _build_svc("django", dockerfile="A"),
            "browser": _build_svc("browser", dockerfile="B"),
        }
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, svcs), out)
        services_tf = (out / "services.tf").read_text()
        assert 'resource "aws_ecr_repository" "django"' in services_tf
        assert 'resource "aws_ecr_repository" "browser"' in services_tf


# ── opt-out: share_image_repos=false keeps per-service repos ─────────────────
# For stacks whose live ECR layout predates rc-44i (per-service repos still
# referenced by the running task defs), grouping would emit fewer repos and a
# regen would DESTROY in-use repos. share_image_repos: false disables grouping.
def _shared3():
    return {
        "django": _build_svc("django", ctx="/app", dockerfile="D"),
        "celery-worker": _build_svc("celery-worker", ctx="/app", dockerfile="D"),
        "celery-beat": _build_svc("celery-beat", ctx="/app", dockerfile="D"),
        "redis": _image_svc("redis"),
    }


def _ctx_no_share(tmp_path: Path, services) -> DeployContext:
    return DeployContext(
        project="mesh",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-2",
                "cluster": "mesh",
                "vpc_cidr": "10.0.0.0/16",
                "share_image_repos": False,
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


class TestShareImageReposOptOut:
    def test_grouping_disabled_each_owns_itself(self):
        owners = image_group_owners(_shared3(), share_repos=False)
        assert owners == {
            "django": "django",
            "celery-worker": "celery-worker",
            "celery-beat": "celery-beat",
        }  # redis has no build_context -> absent

    def test_all_owners_built_when_not_sharing(self):
        names = sorted(
            s.name for s in _services_to_build(_shared3(), share_repos=False)
        )
        assert names == ["celery-beat", "celery-worker", "django"]

    def test_default_still_shares(self):
        # Backward-compat: absent/true -> unchanged rc-44i behavior.
        assert image_group_owners(_shared3())["celery-worker"] == "django"

    def test_emission_keeps_per_service_repos(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx_no_share(tmp_path, _shared3()), out
        )
        services_tf = (out / "services.tf").read_text()
        for svc in ("django", "celery_worker", "celery_beat"):
            assert f'resource "aws_ecr_repository" "{svc}"' in services_tf
        # each references its OWN repo, not a shared owner
        assert services_tf.count("aws_ecr_repository.celery_worker.repository_url") >= 1
