"""rc-05q: ECS aws_ecs_service.health_check_grace_period_seconds emission.

When ALB-fronted services have a slow start script (Django migrate +
collectstatic + uvicorn boot, npm build, etc.), the AWS default of 0s
causes ALB to start health-checking before the container is ready.
With 30s interval × 3 unhealthy threshold the task is killed at ~90s,
but real boot can take 60+ seconds — so EVERY task fails enough checks
to be killed before uvicorn binds. Stack flaps indefinitely.

rc must set health_check_grace_period_seconds whenever the service has
a load_balancer block (public=true). Default 60s base, 180s when the
service has any auto_on_deploy lifecycle hook (e.g. migrate).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict[str, ServiceSpec]) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "myapp-prod",
                "aws_profile": "default",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _services_tf(tmp_path: Path, services: dict[str, ServiceSpec]) -> str:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services), out)
    return (out / "services.tf").read_text()


def _grace_for(hcl: str, service_name: str) -> str | None:
    """Extract health_check_grace_period_seconds from the named service's
    aws_ecs_service block. Returns None when not set."""
    import re
    pattern = re.compile(
        rf'resource "aws_ecs_service" "{service_name}" \{{(.*?)^}}',
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(hcl)
    if not m:
        return None
    block = m.group(1)
    g = re.search(r'health_check_grace_period_seconds\s*=\s*(\d+)', block)
    return g.group(1) if g else None


class TestPublicServiceGetsGracePeriod:
    def test_public_with_health_check_path_gets_default_60s(self, tmp_path):
        services = {
            "web": ServiceSpec(
                name="web", cpu=256, memory=512,
                public=True, port=8080, health_check_path="/health",
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "web") == "60"

    def test_public_without_health_check_path_still_gets_default(self, tmp_path):
        # public=true alone means a load_balancer block exists; AWS still
        # runs target group health checks (default path '/'). Grace period
        # is needed regardless of whether health_check_path is set.
        services = {
            "web": ServiceSpec(
                name="web", cpu=256, memory=512,
                public=True, port=8080,
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "web") == "60"


class TestNonPublicServiceNoGracePeriod:
    def test_internal_service_omits_grace_period(self, tmp_path):
        # AWS rejects health_check_grace_period_seconds on services with no
        # load_balancer block — the field is meaningless without one. The
        # template must NOT emit the line for non-public services.
        services = {
            "worker": ServiceSpec(
                name="worker", cpu=256, memory=512, type="worker",
                public=False,
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "worker") is None


class TestAutoOnDeployBumpsToSlowBootDefault:
    def test_service_with_auto_on_deploy_gets_180s(self, tmp_path):
        # rc-3kq lifecycle hook with auto_on_deploy=True (typical: migrate)
        # runs BEFORE the main container becomes ready, so ALB grace must
        # cover the hook duration.
        services = {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024,
                public=True, port=8000, health_check_path="/health/",
                lifecycle={
                    "migrate": {
                        "command": ["python", "manage.py", "migrate"],
                        "auto_on_deploy": True,
                    },
                },
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "django") == "180"

    def test_explicit_lifecycle_without_auto_on_deploy_uses_60s(self, tmp_path):
        # Lifecycle hook present but auto_on_deploy=False (manual via
        # `rc lifecycle`) means the service does NOT block on it during
        # rollout, so the default 60s grace applies.
        services = {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024,
                public=True, port=8000, health_check_path="/health/",
                lifecycle={
                    "createsuperuser": {
                        "command": ["python", "manage.py", "createsuperuser"],
                        "auto_on_deploy": False,
                    },
                },
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "django") == "60"


class TestExplicitOverride:
    def test_explicit_value_wins_over_default(self, tmp_path):
        services = {
            "web": ServiceSpec(
                name="web", cpu=256, memory=512,
                public=True, port=8080, health_check_path="/health",
                health_check_grace_period=300,
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "web") == "300"

    def test_explicit_zero_disables(self, tmp_path):
        # User who genuinely wants AWS default 0s (e.g. instant-boot static
        # nginx) should be able to opt out. 0 still emits the line so the
        # intent is durable in state.
        services = {
            "web": ServiceSpec(
                name="web", cpu=256, memory=512,
                public=True, port=8080, health_check_path="/health",
                health_check_grace_period=0,
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "web") == "0"

    def test_explicit_overrides_auto_on_deploy_default(self, tmp_path):
        # Even with auto_on_deploy lifecycle, an explicit value wins.
        services = {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024,
                public=True, port=8000, health_check_path="/health/",
                health_check_grace_period=600,
                lifecycle={
                    "migrate": {
                        "command": ["python", "manage.py", "migrate"],
                        "auto_on_deploy": True,
                    },
                },
            ),
        }
        hcl = _services_tf(tmp_path, services)
        assert _grace_for(hcl, "django") == "600"
