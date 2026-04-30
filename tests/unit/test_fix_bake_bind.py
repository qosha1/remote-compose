"""rc-bys: rc fix bake-bind-mount-source — append COPY directives to a
service's Dockerfile for each source bind mount in compose.

Sentinal repro: compose volumes ./backend:/app, but local Dockerfile
doesn't COPY ./backend. ECS deploys had no /app/manage.py until a manual
edit. This subcommand automates the fix.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from remote_compose.fix_bake_bind import bake_bind_mount_source


def _scaffold(tmp_path: Path, compose_yaml: str, dockerfile_content: str = None,
              dockerfile_rel: str = "compose/local/django/Dockerfile") -> Path:
    """Lay out a compose-style project with a Dockerfile."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(compose_yaml)
    df_path = tmp_path / dockerfile_rel
    df_path.parent.mkdir(parents=True, exist_ok=True)
    df_path.write_text(dockerfile_content or "FROM python:3.11\nCMD ['echo', 'hi']\n")
    return compose


class TestHappyPath:
    def test_appends_copy_for_relative_source_bind_mount(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              django:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
                volumes:
                  - ./backend:/app
        """).strip())
        result = bake_bind_mount_source(compose, "django")
        assert result.skipped_reason is None
        assert result.copies_added == [("./backend", "/app")]
        df = (tmp_path / "compose/local/django/Dockerfile").read_text()
        assert "COPY ./backend /app" in df
        assert "rc-bys" in df

    def test_appends_multiple_copies(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              django:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
                volumes:
                  - ./backend:/app
                  - ./scripts:/app/scripts
                  - ./test-fixtures:/app/test-fixtures
        """).strip())
        result = bake_bind_mount_source(compose, "django")
        assert len(result.copies_added) == 3
        df = (tmp_path / "compose/local/django/Dockerfile").read_text()
        for host, container in result.copies_added:
            assert f"COPY {host} {container}" in df


class TestSkipPaths:
    def test_skips_named_volume(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              postgres:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
                volumes:
                  - postgres_data:/var/lib/postgresql/data
            volumes:
              postgres_data: {}
        """).strip())
        result = bake_bind_mount_source(compose, "postgres")
        assert result.skipped_reason is not None
        assert "no source bind mounts" in result.skipped_reason

    def test_skips_x11_socket(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              celerybrowser:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
                volumes:
                  - /tmp/.X11-unix:/tmp/.X11-unix
        """).strip())
        result = bake_bind_mount_source(compose, "celerybrowser")
        assert result.skipped_reason is not None

    def test_skips_postgres_data(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              postgres:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
                volumes:
                  - ./pg_data:/var/lib/postgresql/data
        """).strip())
        result = bake_bind_mount_source(compose, "postgres")
        # /var/lib/postgresql/data is in _SKIP_CONTAINER_PATHS
        assert result.skipped_reason is not None


class TestErrorPaths:
    def test_unknown_service_raises(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              django:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
        """).strip())
        with pytest.raises(ValueError, match="not in compose"):
            bake_bind_mount_source(compose, "doesnotexist")

    def test_image_only_service_raises(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              redis:
                image: redis:6
        """).strip())
        with pytest.raises(ValueError, match="no `build:` stanza"):
            bake_bind_mount_source(compose, "redis")


class TestIdempotence:
    def test_already_present_skips(self, tmp_path):
        compose = _scaffold(
            tmp_path,
            textwrap.dedent("""
                services:
                  django:
                    build:
                      context: .
                      dockerfile: compose/local/django/Dockerfile
                    volumes:
                      - ./backend:/app
            """).strip(),
            dockerfile_content=(
                "FROM python:3.11\n"
                "WORKDIR /app\n"
                "COPY ./backend /app\n"
                "CMD ['echo', 'hi']\n"
            ),
        )
        result = bake_bind_mount_source(compose, "django")
        assert result.skipped_reason is not None
        assert "already present" in result.skipped_reason

    def test_force_appends_duplicate(self, tmp_path):
        compose = _scaffold(
            tmp_path,
            textwrap.dedent("""
                services:
                  django:
                    build:
                      context: .
                      dockerfile: compose/local/django/Dockerfile
                    volumes:
                      - ./backend:/app
            """).strip(),
            dockerfile_content=(
                "FROM python:3.11\n"
                "WORKDIR /app\n"
                "COPY ./backend /app\n"
                "CMD ['echo', 'hi']\n"
            ),
        )
        result = bake_bind_mount_source(compose, "django", force=True)
        assert result.skipped_reason is None
        df = (tmp_path / "compose/local/django/Dockerfile").read_text()
        # Two occurrences after force.
        assert df.count("COPY ./backend /app") == 2


class TestLongFormVolumes:
    def test_long_form_dict(self, tmp_path):
        compose = _scaffold(tmp_path, textwrap.dedent("""
            services:
              django:
                build:
                  context: .
                  dockerfile: compose/local/django/Dockerfile
                volumes:
                  - type: bind
                    source: ./backend
                    target: /app
        """).strip())
        result = bake_bind_mount_source(compose, "django")
        assert result.copies_added == [("./backend", "/app")]
