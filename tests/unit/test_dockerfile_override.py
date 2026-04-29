"""Tests for services[*].dockerfile override (rc-e5u.46.1).

Lets users point rc at an ECS-aware Dockerfile WITHOUT modifying their
docker-compose.yml. Path is relative to the build context (mirrors
compose's build.dockerfile semantics).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from remote_compose.config.v2_schema import ServiceV2, parse as parse_v2
from remote_compose.cli_v2 import build_deploy_context, load_rc_yml


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

def _v2_yaml_with(services_yaml: str) -> dict:
    return yaml.safe_load(textwrap.dedent(f"""
        version: 2
        project: testp
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs: {{region: us-west-1}}
        terraform:
          backend: {{type: local}}
        services:
          {services_yaml}
    """))


def test_service_without_dockerfile_field_defaults_to_none():
    cfg = parse_v2(_v2_yaml_with("api: {cpu: 256, memory: 512}"))
    assert cfg.services["api"].dockerfile is None


def test_service_with_dockerfile_override_parses():
    raw = yaml.safe_load(textwrap.dedent("""
        version: 2
        project: testp
        compose_file: docker-compose.yml
        provider: ecs
        provider_config: {ecs: {region: us-west-1}}
        terraform: {backend: {type: local}}
        services:
          nginx:
            cpu: 256
            memory: 512
            dockerfile: ./compose/ecs/nginx/Dockerfile
    """))
    cfg = parse_v2(raw)
    assert cfg.services["nginx"].dockerfile == "./compose/ecs/nginx/Dockerfile"


# ---------------------------------------------------------------------------
# build_deploy_context: rc.yml dockerfile WINS over compose's build.dockerfile
# ---------------------------------------------------------------------------

class TestBuildDeployContextOverride:
    def _setup(self, tmp_path, compose_yaml: str, rc_yaml: str):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(textwrap.dedent(compose_yaml))
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text(textwrap.dedent(rc_yaml))
        return rc_path

    def test_no_override_falls_back_to_compose_dockerfile(self, tmp_path):
        rc_path = self._setup(
            tmp_path,
            """\
            services:
              api:
                build:
                  context: .
                  dockerfile: ./Dockerfile.compose
            """,
            """\
            version: 2
            project: testp
            compose_file: docker-compose.yml
            provider: ecs
            provider_config: {ecs: {region: us-west-1}}
            terraform: {backend: {type: local}}
            services:
              api:
                cpu: 256
                memory: 512
            """,
        )
        _, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        assert ctx.services["api"].dockerfile == "./Dockerfile.compose"

    def test_override_replaces_compose_dockerfile(self, tmp_path):
        rc_path = self._setup(
            tmp_path,
            """\
            services:
              nginx:
                build:
                  context: .
                  dockerfile: ./compose/local/nginx/Dockerfile
            """,
            """\
            version: 2
            project: testp
            compose_file: docker-compose.yml
            provider: ecs
            provider_config: {ecs: {region: us-west-1}}
            terraform: {backend: {type: local}}
            services:
              nginx:
                cpu: 256
                memory: 512
                dockerfile: ./compose/ecs/nginx/Dockerfile
            """,
        )
        _, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        # rc.yml override replaces compose's build.dockerfile
        assert ctx.services["nginx"].dockerfile == "./compose/ecs/nginx/Dockerfile"
        # build_context still inherits from compose
        assert ctx.services["nginx"].build_context is not None

    def test_override_works_when_compose_has_no_dockerfile_field(self, tmp_path):
        # compose with `build: {context: .}` (no dockerfile — defaults to ./Dockerfile)
        rc_path = self._setup(
            tmp_path,
            """\
            services:
              api:
                build:
                  context: .
            """,
            """\
            version: 2
            project: testp
            compose_file: docker-compose.yml
            provider: ecs
            provider_config: {ecs: {region: us-west-1}}
            terraform: {backend: {type: local}}
            services:
              api:
                cpu: 256
                memory: 512
                dockerfile: ./Dockerfile.alt
            """,
        )
        _, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        assert ctx.services["api"].dockerfile == "./Dockerfile.alt"

    def test_compose_only_service_has_no_override_path(self, tmp_path):
        # When a service is only in compose (no rc.yml entry), the override
        # field doesn't exist — fall back to compose's dockerfile.
        rc_path = self._setup(
            tmp_path,
            """\
            services:
              extras:
                build:
                  context: .
                  dockerfile: ./Dockerfile.extras
            """,
            """\
            version: 2
            project: testp
            compose_file: docker-compose.yml
            provider: ecs
            provider_config: {ecs: {region: us-west-1}}
            terraform: {backend: {type: local}}
            """,
        )
        _, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        assert ctx.services["extras"].dockerfile == "./Dockerfile.extras"


# ---------------------------------------------------------------------------
# Integration: ServiceSpec.dockerfile makes it to ImageBuildSpec
# ---------------------------------------------------------------------------

def test_image_build_spec_uses_overridden_dockerfile_path(tmp_path):
    """End-to-end: rc.yml dockerfile flows through ServiceSpec into the
    ImageBuildSpec that ImageBuilder consumes. ImageBuilder.build joins
    the dockerfile path to the build context — that's tested in
    test_image/. Here we just verify the override SURVIVES the cli_v2
    pipeline and reaches ServiceSpec.dockerfile in the final ctx.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    compose = proj / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          nginx:
            build:
              context: .
              dockerfile: ./compose/local/nginx/Dockerfile
    """))
    rc_yml = proj / "rc.yml"
    rc_yml.write_text(textwrap.dedent("""
        version: 2
        project: testp
        compose_file: docker-compose.yml
        provider: ecs
        provider_config: {ecs: {region: us-west-1}}
        terraform: {backend: {type: local}}
        services:
          nginx:
            cpu: 256
            memory: 512
            dockerfile: ./compose/ecs/nginx/Dockerfile
    """))
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)
    spec = ctx.services["nginx"]
    # The provider's _build_and_push_images does
    # `dockerfile=Path(spec.dockerfile) if spec.dockerfile else None`
    # and ImageBuilder joins to spec.context. Verify the relevant fields:
    assert spec.dockerfile == "./compose/ecs/nginx/Dockerfile"
    assert spec.build_context is not None
    # Sanity: the override path resolves under the compose project root
    # (not under /tmp or somewhere weird).
    full = Path(spec.build_context) / spec.dockerfile
    assert "compose/ecs/nginx/Dockerfile" in str(full)
