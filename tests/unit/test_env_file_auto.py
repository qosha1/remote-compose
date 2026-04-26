"""Unit tests for the secrets: source: env_file_auto path (rc-e5u.44.12)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import (
    _expand_env_file_auto,
    build_deploy_context,
    load_rc_yml,
)
from remote_compose.config.v2_schema import (
    SecretRefV2,
    VALID_SECRET_SOURCES,
    parse as parse_v2,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_env_file_auto_is_a_valid_secret_source():
    assert "env_file_auto" in VALID_SECRET_SOURCES


def test_env_file_auto_requires_no_extra_fields():
    """Unlike `source: file` (needs path) or `source: aws_sm` (needs arn),
    env_file_auto is self-describing — it auto-discovers from compose."""
    sec = SecretRefV2(name="env", source="env_file_auto")
    sec.validate()  # should NOT raise


def test_env_file_auto_parses_from_yaml(tmp_path):
    raw = yaml.safe_load(textwrap.dedent("""
        version: 2
        project: x
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
        terraform:
          backend:
            type: local
        secrets:
          - name: env
            source: env_file_auto
    """))
    cfg = parse_v2(raw)
    assert len(cfg.secrets) == 1
    assert cfg.secrets[0].source == "env_file_auto"
    assert cfg.secrets[0].path is None


# ---------------------------------------------------------------------------
# _expand_env_file_auto
# ---------------------------------------------------------------------------

def _setup(tmp_path, compose_text, env_files):
    """Write compose + env_files; return (compose_path, env_paths)."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(compose_text)
    written = []
    for rel, body in env_files.items():
        ep = tmp_path / rel
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text(body)
        written.append(ep)
    return compose, written


class TestExpand:
    def test_no_auto_secret_returns_input_unchanged(self, tmp_path):
        compose, _ = _setup(tmp_path, "services: {api: {image: x}}", {})
        secrets = [SecretRefV2(name="manual", source="file", path=str(tmp_path / "x"))]
        out, suppressed = _expand_env_file_auto(secrets, {"api": {}}, compose)
        assert out == secrets
        assert suppressed == set()

    def test_auto_with_no_env_files_drops_auto_entry(self, tmp_path):
        compose, _ = _setup(tmp_path, "services: {api: {image: x}}", {})
        secrets = [SecretRefV2(name="env", source="env_file_auto")]
        out, suppressed = _expand_env_file_auto(secrets, {"api": {}}, compose)
        assert out == []
        assert suppressed == set()

    def test_auto_expands_one_file_per_unique_env_file(self, tmp_path):
        compose, _ = _setup(
            tmp_path,
            "services: {api: {image: x}, worker: {image: x}}",
            {".envs/.local/.django": "AWS_KEY=foo\nDB_URL=bar\n",
             ".envs/.local/.postgres": "POSTGRES_PASSWORD=p\n"},
        )
        compose_services = {
            "api": {"env_file": [".envs/.local/.django", ".envs/.local/.postgres"]},
            # worker shares one of the same env_files — should dedupe
            "worker": {"env_file": ".envs/.local/.django"},
        }
        secrets = [SecretRefV2(name="env", source="env_file_auto")]
        out, suppressed = _expand_env_file_auto(secrets, compose_services, compose)
        # 2 unique env_files, no original entries other than the dropped auto
        assert len(out) == 2
        names = {s.name for s in out}
        assert names == {"local-django", "local-postgres"}
        # All AWS-safe + absolute paths
        for s in out:
            assert s.source == "file"
            assert Path(s.path).is_absolute()
        # All keys from both env_files end up in the suppression set
        assert suppressed == {"AWS_KEY", "DB_URL", "POSTGRES_PASSWORD"}

    def test_auto_preserves_existing_file_secrets(self, tmp_path):
        compose, _ = _setup(
            tmp_path,
            "services: {api: {image: x}}",
            {".envs/.local/.django": "K=v\n"},
        )
        # User has a manually-declared `source: file` AND an auto entry
        manual = SecretRefV2(name="extra", source="file",
                             path=str(tmp_path / ".envs/.local/.django"))
        secrets = [manual, SecretRefV2(name="env", source="env_file_auto")]
        compose_services = {"api": {"env_file": ".envs/.local/.django"}}
        out, suppressed = _expand_env_file_auto(secrets, compose_services, compose)
        # Manual entry kept; auto entry replaced by 1 file entry
        assert manual in out
        assert any(s.source == "file" and s.name == "local-django" for s in out)
        assert "K" in suppressed

    def test_missing_env_file_does_not_error_but_skips_key_suppression(self, tmp_path):
        compose, _ = _setup(tmp_path, "services: {api: {image: x}}", {})
        compose_services = {"api": {"env_file": ".envs/.local/.django"}}  # not on disk
        secrets = [SecretRefV2(name="env", source="env_file_auto")]
        out, suppressed = _expand_env_file_auto(secrets, compose_services, compose)
        # We still emit a SecretRef so terraform creates the SM placeholder
        assert len(out) == 1
        assert out[0].source == "file"
        # But no keys suppressed since we couldn't read the file
        assert suppressed == set()


# ---------------------------------------------------------------------------
# End-to-end via build_deploy_context — env keys in the file disappear from
# the per-service env dict (the bead's whole point)
# ---------------------------------------------------------------------------

def test_build_deploy_context_strips_env_file_keys_when_auto_present(tmp_path):
    """Acceptance: a key in env_file lands in secrets, NOT in env."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".envs").mkdir()
    (proj / ".envs/.django").write_text("AWS_KEY=secret-value\nDB_URL=postgres://...\n")
    compose = proj / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          api:
            image: nginx:alpine
            ports: ['80:80']
            env_file:
              - .envs/.django
            environment:
              # An override (NOT in env_file) — should remain in env[]
              LOG_LEVEL: debug
    """))
    rc_yml = proj / "rc.yml"
    rc_yml.write_text(textwrap.dedent("""
        version: 2
        project: p
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
        terraform:
          backend:
            type: local
        secrets:
          - name: env
            source: env_file_auto
    """))
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    api = ctx.services["api"]
    # Suppressed: env_file keys do NOT appear in plaintext env
    assert "AWS_KEY" not in api.env
    assert "DB_URL" not in api.env
    # Preserved: inline `environment:` overrides DO appear
    assert api.env["LOG_LEVEL"] == "debug"

    # And the SecretRef expansion happened: provider sees source=file
    assert len(ctx.secrets) == 1
    assert ctx.secrets[0].source == "file"
    assert ctx.secrets[0].name == "django"  # from .envs/.django -> 'django'


def test_build_deploy_context_without_env_file_auto_keeps_old_behavior(tmp_path):
    """Without the auto declaration, env_file values DO inline (for backwards
    compat). Users who explicitly want the leak get the leak."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".envs").mkdir()
    (proj / ".envs/.django").write_text("AWS_KEY=secret-value\n")
    compose = proj / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          api:
            image: nginx:alpine
            ports: ['80:80']
            env_file:
              - .envs/.django
    """))
    rc_yml = proj / "rc.yml"
    rc_yml.write_text(textwrap.dedent("""
        version: 2
        project: p
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
        terraform:
          backend:
            type: local
    """))
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    api = ctx.services["api"]
    # Old behavior: env_file values inline as plaintext env
    assert api.env.get("AWS_KEY") == "secret-value"
    assert ctx.secrets == []
