"""Framework preset lifecycle hooks (rc-e5u.35.7).

When a service declares ``framework: django`` (or `rails` / `phoenix` /
etc.), rc auto-injects framework-specific lifecycle hooks (Django:
createsuperuser/shell/collectstatic/dbshell; Rails: console/dbconsole/
seed/routes; Phoenix: console/seed/rollback) so the user can run
``rc lifecycle <hook> <svc>`` without spelling each one out in rc.yml.

User overrides always win — explicit lifecycle entries in rc.yml are
NOT shadowed by the framework's defaults.

Auto-detection from the Dockerfile also resolves the framework when no
explicit ``framework:`` field is set, so unmodified Django stacks still
get the hook surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
from remote_compose.frameworks import DJANGO, PHOENIX, RAILS


# ---------------------------------------------------------------------------
# Framework presets carry the expected hooks
# ---------------------------------------------------------------------------


class TestFrameworkLifecycleMaps:
    def test_django_has_canonical_hooks(self):
        assert "createsuperuser" in DJANGO.lifecycle_hooks
        assert "shell" in DJANGO.lifecycle_hooks
        assert "collectstatic" in DJANGO.lifecycle_hooks
        assert "dbshell" in DJANGO.lifecycle_hooks
        # createsuperuser uses --noinput so non-interactive execute-command
        # honors DJANGO_SUPERUSER_{EMAIL,USERNAME,PASSWORD} env.
        assert "--noinput" in DJANGO.lifecycle_hooks["createsuperuser"]

    def test_django_has_loaddata_hook(self):
        # rc-2kj: symmetric with Rails 'seed' for fixture seeding.
        assert "loaddata" in DJANGO.lifecycle_hooks
        assert DJANGO.lifecycle_hooks["loaddata"][:3] == (
            "python", "manage.py", "loaddata",
        )

    def test_rails_has_console_and_seed(self):
        assert "console" in RAILS.lifecycle_hooks
        assert "dbconsole" in RAILS.lifecycle_hooks
        assert "seed" in RAILS.lifecycle_hooks
        assert RAILS.lifecycle_hooks["console"][:2] == ("bundle", "exec")

    def test_phoenix_has_iex_console(self):
        assert "console" in PHOENIX.lifecycle_hooks
        # iex -S mix is THE Phoenix REPL.
        assert PHOENIX.lifecycle_hooks["console"][:1] == ("iex",)


# ---------------------------------------------------------------------------
# build_deploy_context merges framework hooks into ServiceSpec.lifecycle
# ---------------------------------------------------------------------------


def _write_v2(tmp_path: Path, *, rc_services: dict, compose_services: dict) -> Path:
    rc = {
        "version": 2,
        "project": "test-35-7",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {"region": "us-west-1"}},
        "terraform": {"backend": {"type": "local"}},
        "services": rc_services,
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump({
        "version": "3", "services": compose_services,
    }))
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(rc))
    return p


class TestExplicitFrameworkField:
    def test_django_field_injects_createsuperuser_hook(self, tmp_path):
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "framework": "django",
                },
            },
            compose_services={"api": {"image": "myapp:latest"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        spec = ctx.services["api"]

        assert "createsuperuser" in spec.lifecycle
        assert spec.lifecycle["createsuperuser"]["command"] == [
            "python", "manage.py", "createsuperuser", "--noinput",
        ]
        # Framework-injected hooks default to non-auto, non-run-once.
        assert spec.lifecycle["createsuperuser"]["auto_on_deploy"] is False

    def test_django_field_marks_shell_hooks_interactive(self, tmp_path):
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "framework": "django",
                },
            },
            compose_services={"api": {"image": "myapp:latest"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        spec = ctx.services["api"]
        # `shell`, `dbshell` need a TTY — framework merge sets that.
        assert spec.lifecycle["shell"]["interactive"] is True
        assert spec.lifecycle["dbshell"]["interactive"] is True
        # createsuperuser is non-interactive (uses --noinput env vars).
        assert spec.lifecycle["createsuperuser"]["interactive"] is False

    def test_rails_field_injects_console_hook(self, tmp_path):
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "web": {
                    "cpu": 512, "memory": 1024, "type": "application",
                    "framework": "rails",
                },
            },
            compose_services={"web": {"image": "rails-app:latest"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        spec = ctx.services["web"]
        assert "console" in spec.lifecycle
        assert spec.lifecycle["console"]["command"][:2] == ["bundle", "exec"]

    def test_unknown_framework_is_a_silent_noop(self, tmp_path):
        # Mistyped or community framework not in the registry — service
        # still parses, no extra hooks land. (Future: open an issue, etc.)
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "framework": "not-a-framework",
                },
            },
            compose_services={"api": {"image": "x:1"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        # No django hooks land — only what the user explicitly declared.
        assert ctx.services["api"].lifecycle == {}


# ---------------------------------------------------------------------------
# User overrides win over framework defaults
# ---------------------------------------------------------------------------


class TestUserOverrideWins:
    def test_user_lifecycle_shadows_framework_default(self, tmp_path):
        # User's createsuperuser uses a custom one-liner.
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "framework": "django",
                    "lifecycle": {
                        "createsuperuser": {
                            "command": ["python", "manage.py", "create_admin"],
                        },
                    },
                },
            },
            compose_services={"api": {"image": "myapp:latest"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        spec = ctx.services["api"]
        # User's command wins; framework default is not merged in.
        assert spec.lifecycle["createsuperuser"]["command"] == [
            "python", "manage.py", "create_admin",
        ]
        # Other framework hooks the user DIDN'T override still land.
        assert "shell" in spec.lifecycle


# ---------------------------------------------------------------------------
# Auto-detection (no explicit framework:) lights up the same hooks
# ---------------------------------------------------------------------------


class TestAutoDetection:
    def test_django_dockerfile_marker_injects_hooks(self, tmp_path):
        # Build dockerfile with manage.py marker; service has no
        # framework: field but build context points at it.
        df_dir = tmp_path / "ctx"
        df_dir.mkdir()
        (df_dir / "Dockerfile").write_text(
            "FROM python:3.11\nCOPY manage.py /app/\n"
        )
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "api": {"cpu": 256, "memory": 512, "type": "application"},
            },
            compose_services={
                "api": {"build": {"context": "./ctx"}},
            },
        )
        version, raw, v2 = load_rc_yml(rc_path)
        ctx = build_deploy_context(v2, raw, rc_path)
        spec = ctx.services["api"]
        # Auto-detection lit up the django hooks even without explicit field.
        assert "createsuperuser" in spec.lifecycle
        assert "shell" in spec.lifecycle


# ---------------------------------------------------------------------------
# v2 schema parses framework field
# ---------------------------------------------------------------------------


class TestSchemaParsesFrameworkField:
    def test_framework_attribute_set(self, tmp_path):
        rc_path = _write_v2(
            tmp_path,
            rc_services={
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "framework": "django",
                },
            },
            compose_services={"api": {"image": "x:1"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        assert v2.services["api"].framework == "django"

    def test_framework_default_is_none(self, tmp_path):
        rc_path = _write_v2(
            tmp_path,
            rc_services={"api": {"cpu": 256, "memory": 512, "type": "application"}},
            compose_services={"api": {"image": "x:1"}},
        )
        version, raw, v2 = load_rc_yml(rc_path)
        assert v2.services["api"].framework is None
