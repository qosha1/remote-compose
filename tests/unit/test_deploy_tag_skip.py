"""Tests for `rc deploy --tag <existing>` skip-build path (rc-e5u.45.3).

When user passes --tag <X> (and X != 'latest'):
  - If <repo>:<X> exists in ECR, skip docker build entirely; re-tag to :latest.
  - If <repo>:<X> doesn't exist, build with both tags [<X>, latest] + push.

When --tag is unset (default), behavior is unchanged from before .45.3.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx_with_django():
    return DeployContext(
        project="test-proj",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-1"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services={
            "django": ServiceSpec(
                name="django", cpu=1024, memory=2048, type="application",
                build_context=Path("/tmp/django"),
            ),
        },
        secrets=[],
    )


_OUTPUTS = {
    "ecr_repositories": {
        "value": {
            "django": "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/django",
        }
    }
}


@pytest.fixture
def stub_image_modules():
    """Stub ImageBuilder/ImagePusher/ECRAuthenticator + return spies."""
    import remote_compose.image as _image
    import remote_compose.provider.ecs.ecr_auth as _auth

    built = []
    pushed_tags = []

    class StubBuilder:
        def __init__(self, **_): pass
        def build(self, spec):
            built.append(spec)
            return spec.tags

    class StubPusher:
        def __init__(self, **_): pass
        def push(self, tags):
            pushed_tags.extend(tags)

    class StubAuth:
        def __init__(self, **_): pass
        def __call__(self, *_, **__): pass

    with patch.object(_image, "ImageBuilder", StubBuilder), \
         patch.object(_image, "ImagePusher", StubPusher), \
         patch.object(_auth, "ECRAuthenticator", StubAuth):
        yield built, pushed_tags


# ---------------------------------------------------------------------------
# Tag exists -> skip build, re-tag in ECR
# ---------------------------------------------------------------------------

class TestTagExistsSkipsBuild:
    def test_existing_tag_skips_docker_and_retags(self, stub_image_modules):
        built, pushed = stub_image_modules
        provider = ECSProvider()
        ecr = MagicMock()
        ecr.batch_get_image.return_value = {
            "images": [{
                "imageManifest": '{"schemaVersion":2,"mediaType":"x"}',
            }]
        }
        session = MagicMock()
        session.client.return_value = ecr

        with patch.object(provider, "session_factory", lambda c: session):
            warnings: list = []
            pushed_services = provider._build_and_push_images(
                _ctx_with_django(), _OUTPUTS, warnings,
                requested_tag="v1.2",
            )

        assert pushed_services == ["django"]
        # No docker build invocation
        assert built == []
        assert pushed == []
        # ECR was queried + re-tagged
        ecr.batch_get_image.assert_called_once_with(
            repositoryName="test-proj/django",
            imageIds=[{"imageTag": "v1.2"}],
        )
        ecr.put_image.assert_called_once()
        kwargs = ecr.put_image.call_args.kwargs
        assert kwargs["repositoryName"] == "test-proj/django"
        assert kwargs["imageTag"] == "latest"

    def test_already_existing_latest_tag_succeeds_silently(self, stub_image_modules):
        # AWS raises ImageAlreadyExistsException when manifest+tag combo
        # is already present. Idempotent re-deploys must still succeed.
        built, pushed = stub_image_modules
        provider = ECSProvider()
        ecr = MagicMock()
        ecr.batch_get_image.return_value = {
            "images": [{"imageManifest": '{"x":1}'}]
        }
        ecr.put_image.side_effect = Exception(
            "An error occurred (ImageAlreadyExistsException) ..."
        )
        session = MagicMock()
        session.client.return_value = ecr

        with patch.object(provider, "session_factory", lambda c: session):
            warnings: list = []
            pushed_services = provider._build_and_push_images(
                _ctx_with_django(), _OUTPUTS, warnings,
                requested_tag="v1.2",
            )

        assert pushed_services == ["django"]
        assert built == []


# ---------------------------------------------------------------------------
# Tag doesn't exist -> normal build with both tags
# ---------------------------------------------------------------------------

class TestTagMissingFallsThroughToBuild:
    def test_missing_tag_builds_with_both_tags(self, stub_image_modules):
        built, pushed = stub_image_modules
        provider = ECSProvider()
        ecr = MagicMock()
        ecr.batch_get_image.return_value = {"images": []}  # tag not found
        session = MagicMock()
        session.client.return_value = ecr

        with patch.object(provider, "session_factory", lambda c: session):
            warnings: list = []
            pushed_services = provider._build_and_push_images(
                _ctx_with_django(), _OUTPUTS, warnings,
                requested_tag="v1.2",
            )

        assert pushed_services == ["django"]
        # Build invoked with BOTH the requested tag AND :latest
        assert len(built) == 1
        spec = built[0]
        repo = "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/django"
        assert f"{repo}:v1.2" in spec.tags
        assert f"{repo}:latest" in spec.tags
        # put_image was NOT called (we built fresh, no re-tag dance needed)
        ecr.put_image.assert_not_called()


# ---------------------------------------------------------------------------
# No --tag at all -> backward compat (build :latest, no ECR pre-check)
# ---------------------------------------------------------------------------

class TestNoTagBackwardCompat:
    def test_no_tag_skips_ecr_check_entirely(self, stub_image_modules):
        built, pushed = stub_image_modules
        provider = ECSProvider()
        # If ECR check runs, this MagicMock will record it. We assert it doesn't.
        ecr = MagicMock()
        session = MagicMock()
        session.client.return_value = ecr

        with patch.object(provider, "session_factory", lambda c: session):
            warnings: list = []
            pushed_services = provider._build_and_push_images(
                _ctx_with_django(), _OUTPUTS, warnings,
            )  # requested_tag not passed

        assert pushed_services == ["django"]
        ecr.batch_get_image.assert_not_called()
        ecr.put_image.assert_not_called()
        # Built with :latest only
        assert len(built) == 1
        repo = "111.dkr.ecr.us-west-1.amazonaws.com/test-proj/django"
        assert built[0].tags == [f"{repo}:latest"]

    def test_explicit_tag_latest_skips_ecr_check(self, stub_image_modules):
        # `--tag latest` is explicitly the existing default → should NOT
        # short-circuit (otherwise we'd skip every redeploy when ECR already
        # has :latest, which is always true after first deploy).
        built, _ = stub_image_modules
        provider = ECSProvider()
        ecr = MagicMock()
        session = MagicMock()
        session.client.return_value = ecr

        with patch.object(provider, "session_factory", lambda c: session):
            warnings: list = []
            provider._build_and_push_images(
                _ctx_with_django(), _OUTPUTS, warnings,
                requested_tag="latest",
            )

        ecr.batch_get_image.assert_not_called()
        assert len(built) == 1


# ---------------------------------------------------------------------------
# ECR errors propagate (don't silently mask perms problems)
# ---------------------------------------------------------------------------

class TestEcrErrorPropagation:
    def test_image_not_found_falls_through_to_build(self, stub_image_modules):
        # ECR returning empty images list = tag not present = build.
        built, _ = stub_image_modules
        provider = ECSProvider()
        ecr = MagicMock()
        ecr.batch_get_image.return_value = {"images": []}
        session = MagicMock()
        session.client.return_value = ecr

        with patch.object(provider, "session_factory", lambda c: session):
            warnings: list = []
            provider._build_and_push_images(
                _ctx_with_django(), _OUTPUTS, warnings,
                requested_tag="newtag",
            )

        assert len(built) == 1  # fell through to build

    def test_image_not_found_exception_via_batch_get_returns_none(self):
        # When boto3 raises ImageNotFoundException directly (rather than
        # returning empty images list), our helper treats it the same.
        provider = ECSProvider()
        ecr = MagicMock()
        ecr.batch_get_image.side_effect = Exception(
            "An error occurred (ImageNotFoundException): tag not found"
        )
        result = provider._ecr_image_manifest(ecr, "repo", "v1")
        assert result is None

    def test_other_ecr_error_propagates(self):
        # Permissions / throttling errors must NOT be silently swallowed.
        provider = ECSProvider()
        ecr = MagicMock()
        ecr.batch_get_image.side_effect = Exception(
            "An error occurred (AccessDeniedException): access denied"
        )
        with pytest.raises(Exception, match="AccessDeniedException"):
            provider._ecr_image_manifest(ecr, "repo", "v1")
