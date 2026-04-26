"""Tests for the framework presets registry (rc-e5u.47)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.frameworks import (
    DJANGO,
    PHOENIX,
    RAILS,
    Framework,
    all_frameworks,
    detect_framework,
    framework_by_name,
    register_framework,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_built_ins_present(self):
        names = {f.name for f in all_frameworks()}
        assert {"django", "rails", "phoenix"}.issubset(names)

    def test_framework_by_name_case_insensitive(self):
        assert framework_by_name("django") is DJANGO
        assert framework_by_name("DJANGO") is DJANGO
        assert framework_by_name("  Rails  ") is RAILS

    def test_framework_by_name_unknown(self):
        assert framework_by_name("not-a-framework") is None
        assert framework_by_name("") is None

    def test_register_framework_replaces_by_name(self):
        original = framework_by_name("django")
        replacement = Framework(
            name="django",
            dockerfile_markers=("custom-marker",),
            migrate_command=("noop",),
        )
        register_framework(replacement)
        try:
            assert framework_by_name("django") is replacement
        finally:
            # Restore so we don't pollute later tests in the same process.
            register_framework(original)
        assert framework_by_name("django") is original


# ---------------------------------------------------------------------------
# Built-in preset shapes
# ---------------------------------------------------------------------------


class TestDjangoPreset:
    def test_migrate_command(self):
        assert DJANGO.migrate_command == ("python", "manage.py", "migrate", "--noinput")

    def test_testing_defaults(self):
        assert DJANGO.testing_defaults_env["DJANGO_ALLOWED_HOSTS"] == "*"
        assert DJANGO.testing_defaults_env["CSRF_TRUSTED_ORIGINS"] == "*"
        assert DJANGO.testing_defaults_env["DJANGO_DEBUG"] == "False"

    def test_marker_keys(self):
        assert "DJANGO_ALLOWED_HOSTS" in DJANGO.testing_defaults_marker_keys

    def test_host_header_rewrite(self):
        # Django needs Host: localhost so ALLOWED_HOSTS lets the request through.
        assert DJANGO.host_header_rewrite == "localhost"


class TestRailsPreset:
    def test_migrate_command(self):
        assert RAILS.migrate_command == ("bundle", "exec", "rails", "db:migrate")

    def test_testing_defaults_relax_hosts(self):
        assert RAILS.testing_defaults_env["RAILS_HOSTS"] == "*"

    def test_no_default_host_rewrite(self):
        # Rails config.hosts is opt-in for users; we don't auto-rewrite Host.
        assert RAILS.host_header_rewrite is None


class TestPhoenixPreset:
    def test_migrate_command(self):
        assert PHOENIX.migrate_command == ("mix", "ecto.migrate")

    def test_testing_defaults_phx_host(self):
        assert PHOENIX.testing_defaults_env["PHX_HOST"] == "*"


# ---------------------------------------------------------------------------
# Detection from Dockerfile
# ---------------------------------------------------------------------------


def _make_compose_tree(tmp_path: Path, dockerfile_content: str, build: dict) -> Path:
    """Return a compose_path next to a synthetic Dockerfile tree."""
    df_path = tmp_path / "app" / "Dockerfile"
    df_path.parent.mkdir(parents=True, exist_ok=True)
    df_path.write_text(dockerfile_content)
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("version: '3'\n")
    return compose_path


class TestDetectFramework:
    def test_django_via_manage_py(self, tmp_path):
        compose_path = _make_compose_tree(
            tmp_path,
            "FROM python:3.11\nCOPY manage.py /app/\n",
            {"context": "./app"},
        )
        fw = detect_framework({"build": {"context": "./app"}}, compose_path)
        assert fw is not None and fw.name == "django"

    def test_django_via_pip_dep(self, tmp_path):
        compose_path = _make_compose_tree(
            tmp_path,
            "FROM python:3.11\nRUN pip install django==4.2\n",
            {"context": "./app"},
        )
        fw = detect_framework({"build": "./app"}, compose_path)
        assert fw is not None and fw.name == "django"

    def test_rails_via_gemfile(self, tmp_path):
        compose_path = _make_compose_tree(
            tmp_path,
            "FROM ruby:3.2\nCOPY Gemfile /app/\nRUN bundle install\n",
            {"context": "./app"},
        )
        fw = detect_framework({"build": "./app"}, compose_path)
        assert fw is not None and fw.name == "rails"

    def test_phoenix_via_mix_exs(self, tmp_path):
        compose_path = _make_compose_tree(
            tmp_path,
            "FROM elixir:1.15\nCOPY mix.exs /app/\nRUN mix deps.get\n",
            {"context": "./app"},
        )
        fw = detect_framework({"build": "./app"}, compose_path)
        assert fw is not None and fw.name == "phoenix"

    def test_unknown_dockerfile_returns_none(self, tmp_path):
        compose_path = _make_compose_tree(
            tmp_path,
            "FROM nginx:alpine\nCOPY ./conf /etc/nginx/\n",
            {"context": "./app"},
        )
        fw = detect_framework({"build": "./app"}, compose_path)
        assert fw is None

    def test_image_only_service_returns_none(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text("version: '3'\n")
        fw = detect_framework({"image": "postgres:16-alpine"}, compose_path)
        assert fw is None

    def test_missing_dockerfile_returns_none(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text("version: '3'\n")
        fw = detect_framework({"build": "./does-not-exist"}, compose_path)
        assert fw is None


# ---------------------------------------------------------------------------
# Integration: scaffolder uses framework registry (smoke)
# ---------------------------------------------------------------------------


class TestScaffolderUsesRegistry:
    """Sanity-check that init_from_compose pulls the migrate command from the
    framework registry, not a hardcoded constant. Editing DJANGO.migrate_command
    in this test propagates to the generated rc.yml."""

    def test_django_migrate_command_comes_from_preset(self, tmp_path, monkeypatch):
        from remote_compose.init_from_compose import generate_v2_rc_yml

        df_path = tmp_path / "Dockerfile"
        df_path.write_text("FROM python:3.11\nCOPY manage.py /app/\n")
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(
            "version: '3'\n"
            "services:\n"
            "  api:\n"
            "    build: .\n"
            "    ports: ['8000:8000']\n"
        )

        # Swap DJANGO.migrate_command for a custom value via register_framework.
        custom = Framework(
            name="django",
            dockerfile_markers=DJANGO.dockerfile_markers,
            migrate_command=("python", "manage.py", "ZZZ_CUSTOM_MIGRATE"),
            testing_defaults_env=DJANGO.testing_defaults_env,
            testing_defaults_marker_keys=DJANGO.testing_defaults_marker_keys,
            host_header_rewrite=DJANGO.host_header_rewrite,
        )
        register_framework(custom)
        try:
            out = generate_v2_rc_yml(compose_path)
        finally:
            register_framework(DJANGO)

        assert "ZZZ_CUSTOM_MIGRATE" in out

    def test_rails_migrate_command_emitted_for_rails_service(self, tmp_path):
        from remote_compose.init_from_compose import generate_v2_rc_yml

        df_path = tmp_path / "Dockerfile"
        df_path.write_text("FROM ruby:3.2\nCOPY Gemfile /app/\n")
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(
            "version: '3'\n"
            "services:\n"
            "  api:\n"
            "    build: .\n"
            "    ports: ['3000:3000']\n"
        )
        out = generate_v2_rc_yml(compose_path)
        # Rails preset's migrate command lands in the lifecycle.migrate hook.
        assert "bundle" in out and "db:migrate" in out

    def test_rails_testing_defaults_emitted_when_rc_test_project(self, tmp_path):
        from remote_compose.init_from_compose import generate_v2_rc_yml

        df_path = tmp_path / "Dockerfile"
        df_path.write_text("FROM ruby:3.2\nCOPY Gemfile /app/\n")
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(
            "version: '3'\n"
            "services:\n"
            "  api:\n"
            "    build: .\n"
            "    ports: ['3000:3000']\n"
        )
        # testing_defaults=True forces injection regardless of project name.
        out = generate_v2_rc_yml(compose_path, testing_defaults=True)
        # Rails preset injects RAILS_HOSTS=* for ephemeral test stacks.
        assert "RAILS_HOSTS" in out
