"""rc-ife: detect_python_pyc_in_build_context warns when a Python
service's build context contains __pycache__ / .pyc files but the
.dockerignore doesn't exclude them.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from remote_compose.compose_warnings import detect_python_pyc_in_build_context


def _scaffold(
    tmp_path: Path,
    compose_yaml: str,
    *,
    has_pyc: bool = True,
    dockerignore: str = None,
) -> tuple[dict, Path]:
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(compose_yaml)
    # Build context root = tmp_path. Stage the Dockerfile so build resolves.
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    if has_pyc:
        cache_dir = tmp_path / "src" / "__pycache__"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "module.cpython-311.pyc").write_bytes(b"\x00\x00")
    if dockerignore is not None:
        (tmp_path / ".dockerignore").write_text(dockerignore)
    compose = yaml.safe_load(compose_yaml) or {}
    return compose, compose_path


class TestDetectsPycInPythonContext:
    def test_warns_for_python_image_without_dockerignore(self, tmp_path):
        compose, path = _scaffold(tmp_path, textwrap.dedent("""
            services:
              api:
                build: .
                image: my-py:latest
                command: python manage.py runserver
        """).strip())
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert len(warnings) == 1
        assert "rc-ife" in warnings[0]
        assert "__pycache__" in warnings[0]

    def test_warns_for_uvicorn_command(self, tmp_path):
        compose, path = _scaffold(tmp_path, textwrap.dedent("""
            services:
              api:
                build: .
                image: anything
                command: uvicorn config.asgi:app --host 0.0.0.0
        """).strip())
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert len(warnings) == 1

    def test_warns_for_python_base_image(self, tmp_path):
        compose, path = _scaffold(tmp_path, textwrap.dedent("""
            services:
              api:
                build: .
                image: python:3.11-slim
        """).strip())
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert len(warnings) == 1


class TestDoesNotWarn:
    def test_silent_when_dockerignore_excludes_pyc(self, tmp_path):
        compose, path = _scaffold(
            tmp_path,
            textwrap.dedent("""
                services:
                  api:
                    build: .
                    command: python manage.py runserver
            """).strip(),
            dockerignore="**/__pycache__\n**/*.pyc\n",
        )
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert warnings == []

    def test_silent_when_no_pyc_in_context(self, tmp_path):
        compose, path = _scaffold(
            tmp_path,
            textwrap.dedent("""
                services:
                  api:
                    build: .
                    command: python manage.py runserver
            """).strip(),
            has_pyc=False,
        )
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert warnings == []

    def test_silent_for_non_python_service(self, tmp_path):
        compose, path = _scaffold(tmp_path, textwrap.dedent("""
            services:
              web:
                build: .
                image: nginx:alpine
        """).strip())
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert warnings == []

    def test_silent_when_no_build_context(self, tmp_path):
        compose, path = _scaffold(tmp_path, textwrap.dedent("""
            services:
              api:
                image: python:3.11
                command: python -m mymodule
        """).strip())
        # No build: stanza, so no context to scan.
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert warnings == []

    def test_partial_dockerignore_still_warns(self, tmp_path):
        # Has __pycache__ but not *.pyc → still warn.
        compose, path = _scaffold(
            tmp_path,
            textwrap.dedent("""
                services:
                  api:
                    build: .
                    command: python manage.py runserver
            """).strip(),
            dockerignore="**/__pycache__\n",
        )
        warnings = detect_python_pyc_in_build_context(compose, path)
        assert len(warnings) == 1
