"""Tests for `rc deploy --services <name>` filter (rc-e5u.45.1).

The filter must:
  - validate names against ctx.services (typo = clear error, not silent skip)
  - only build/push selected services' images
  - only force-roll selected services
  - skip auto_on_deploy hooks for non-selected services
  - leave terraform-apply intact (idempotent; rc.yml-driven infra still updates)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from remote_compose.provider.base import (
    DeployContext,
    DeployResult,
    SecretRef,
    ServiceSpec,
)
from remote_compose.provider.fake import FakeProvider


# ---------------------------------------------------------------------------
# FakeProvider behavior
# ---------------------------------------------------------------------------

def _ctx(services=None):
    services = services or {
        "django": ServiceSpec(name="django", cpu=1024, memory=2048, type="application"),
        "nginx": ServiceSpec(name="nginx", cpu=256, memory=512, type="proxy", public=True, port=80),
        "postgres": ServiceSpec(name="postgres", cpu=512, memory=1024, type="infrastructure"),
    }
    return DeployContext(
        project="test-proj",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-1"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services=services,
        secrets=[],
    )


class TestFakeProviderServicesFilter:
    def test_no_filter_returns_all_services(self):
        p = FakeProvider()
        result = p.deploy(_ctx())
        assert sorted(result.services) == ["django", "nginx", "postgres"]

    def test_filter_returns_only_selected(self):
        p = FakeProvider()
        result = p.deploy(_ctx(), services_filter=["django"])
        assert result.services == ["django"]

    def test_filter_with_unknown_service_raises(self):
        p = FakeProvider()
        with pytest.raises(ValueError, match="not in this stack"):
            p.deploy(_ctx(), services_filter=["nonexistent"])

    def test_filter_typo_lists_known_options(self):
        p = FakeProvider()
        with pytest.raises(ValueError, match="nonexistent"):
            p.deploy(_ctx(), services_filter=["nonexistent"])

    def test_filter_on_already_deployed_state_returns_filtered(self):
        """Even when no rebuild is needed (config_hash unchanged), the
        filter selection is honored in the result so callers can record
        which services they intended to roll."""
        p = FakeProvider()
        ctx = _ctx()
        p.deploy(ctx)  # initial deploy
        result = p.deploy(ctx, services_filter=["django"])
        assert result.services == ["django"]

    def test_filter_with_multiple_services(self):
        p = FakeProvider()
        result = p.deploy(_ctx(), services_filter=["django", "nginx"])
        assert sorted(result.services) == ["django", "nginx"]


# ---------------------------------------------------------------------------
# ECSProvider._build_and_push_images filter (no real AWS / docker)
# ---------------------------------------------------------------------------

class TestEcsBuildFilter:
    def test_build_loop_filters_to_named_services(self):
        from remote_compose.provider.ecs.provider import ECSProvider

        # Two services with build_context, one without (nginx uses image)
        services = {
            "django": ServiceSpec(
                name="django", cpu=1024, memory=2048, type="application",
                build_context=Path("/tmp/django"),
            ),
            "celery": ServiceSpec(
                name="celery", cpu=512, memory=1024, type="worker",
                build_context=Path("/tmp/celery"),
            ),
            "nginx": ServiceSpec(
                name="nginx", cpu=256, memory=512, type="proxy",
                image="nginx:alpine",  # no build_context
            ),
        }
        ctx = _ctx(services=services)
        provider = ECSProvider()
        outputs = {
            "ecr_repositories": {
                "value": {
                    "django": "111.dkr.ecr.us-west-1.amazonaws.com/django",
                    "celery": "111.dkr.ecr.us-west-1.amazonaws.com/celery",
                }
            }
        }

        # Patch the actual builder/pusher to record what would be built
        # without running docker.
        # _build_and_push_images does `from ...image import ImageBuilder, ...`
        # at call time. Patch the package-level re-exports + the auth module.
        import remote_compose.image as _image
        import remote_compose.provider.ecs.ecr_auth as _auth

        built = []
        class StubBuilder:
            def __init__(self, **_): pass
            def build(self, spec):
                built.append(spec.service)
                return spec.tags
        class StubPusher:
            def __init__(self, **_): pass
            def push(self, tags): pass
        class StubAuth:
            def __init__(self, **_): pass

        from unittest.mock import patch as _patch
        with _patch.object(_image, "ImageBuilder", StubBuilder), \
             _patch.object(_image, "ImagePusher", StubPusher), \
             _patch.object(_auth, "ECRAuthenticator", StubAuth), \
             _patch.object(provider, "session_factory",
                           lambda c: MagicMock()):
            warnings = []
            pushed = provider._build_and_push_images(
                ctx, outputs, warnings, services_filter=["django"],
            )

        # Only django built; celery skipped despite having a build_context
        assert built == ["django"]
        assert pushed == ["django"]

    def test_build_loop_no_filter_builds_all_with_context(self):
        from remote_compose.provider.ecs.provider import ECSProvider

        services = {
            "django": ServiceSpec(
                name="django", cpu=1024, memory=2048, type="application",
                build_context=Path("/tmp/django"),
            ),
            "celery": ServiceSpec(
                name="celery", cpu=512, memory=1024, type="worker",
                build_context=Path("/tmp/celery"),
            ),
        }
        ctx = _ctx(services=services)
        provider = ECSProvider()
        outputs = {
            "ecr_repositories": {
                "value": {
                    "django": "111.dkr.ecr.us-west-1.amazonaws.com/django",
                    "celery": "111.dkr.ecr.us-west-1.amazonaws.com/celery",
                }
            }
        }

        import remote_compose.image as _image
        import remote_compose.provider.ecs.ecr_auth as _auth
        built = []
        class StubBuilder:
            def __init__(self, **_): pass
            def build(self, spec):
                built.append(spec.service)
                return spec.tags
        class StubPusher:
            def __init__(self, **_): pass
            def push(self, tags): pass
        class StubAuth:
            def __init__(self, **_): pass

        from unittest.mock import patch as _patch
        with _patch.object(_image, "ImageBuilder", StubBuilder), \
             _patch.object(_image, "ImagePusher", StubPusher), \
             _patch.object(_auth, "ECRAuthenticator", StubAuth), \
             _patch.object(provider, "session_factory",
                           lambda c: MagicMock()):
            warnings = []
            pushed = provider._build_and_push_images(ctx, outputs, warnings)

        assert sorted(built) == ["celery", "django"]
        assert sorted(pushed) == ["celery", "django"]


# ---------------------------------------------------------------------------
# auto_on_deploy hook scoping with services_filter
# ---------------------------------------------------------------------------

class TestHookFilter:
    def test_hooks_run_only_for_filtered_services(self):
        from remote_compose.cli_v2 import _run_auto_on_deploy_hooks
        from remote_compose.config.v2_schema import LifecycleHookV2, ServiceV2

        # Two services, each with an auto_on_deploy hook
        django = ServiceV2(name="django", cpu=1024, memory=2048)
        django.lifecycle = {
            "migrate": LifecycleHookV2(
                name="migrate", command=["python", "manage.py", "migrate"],
                auto_on_deploy=True,
            )
        }
        nginx = ServiceV2(name="nginx", cpu=256, memory=512, type="proxy",
                          public=True, port=80)
        nginx.lifecycle = {
            "reload": LifecycleHookV2(
                name="reload", command=["nginx", "-s", "reload"],
                auto_on_deploy=True,
            )
        }

        v2 = MagicMock()
        v2.services = {"django": django, "nginx": nginx}
        ctx = _ctx()

        provider = MagicMock()
        provider.exec.return_value = MagicMock(exit_code=0, stdout="", stderr="")

        # No filter: both hooks run
        _run_auto_on_deploy_hooks(provider, ctx, v2)
        called = [c.args[1] for c in provider.exec.call_args_list]
        assert sorted(called) == ["django", "nginx"]

        # Filter to django: only django.migrate runs
        provider.exec.reset_mock()
        _run_auto_on_deploy_hooks(provider, ctx, v2, services_filter=["django"])
        called = [c.args[1] for c in provider.exec.call_args_list]
        assert called == ["django"]

    def test_hooks_with_no_match_runs_nothing(self):
        from remote_compose.cli_v2 import _run_auto_on_deploy_hooks
        from remote_compose.config.v2_schema import LifecycleHookV2, ServiceV2

        django = ServiceV2(name="django", cpu=1024, memory=2048)
        django.lifecycle = {
            "migrate": LifecycleHookV2(
                name="migrate", command=["python", "manage.py", "migrate"],
                auto_on_deploy=True,
            )
        }
        v2 = MagicMock()
        v2.services = {"django": django}
        ctx = _ctx()
        provider = MagicMock()

        # Filter to a service WITHOUT hooks — provider.exec never called
        _run_auto_on_deploy_hooks(provider, ctx, v2, services_filter=["postgres"])
        provider.exec.assert_not_called()
