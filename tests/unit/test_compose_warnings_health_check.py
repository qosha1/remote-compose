"""rc-6jq: detect_unmatched_health_check_path warns when rc.yml's
health_check_path doesn't appear in any urls.py in the django build
context. Real-AWS-validated start-simpli pattern: rc.yml had
/api/health/ but real endpoint was /api/v1/health/ → ALB 404s.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


from remote_compose.compose_warnings import detect_unmatched_health_check_path


def _scaffold_django(tmp_path: Path, urls_py_content: str = None) -> Path:
    """Lay out a django-shaped build context with a single urls.py."""
    ctx = tmp_path / "django"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        "FROM python:3.11\nWORKDIR /app\nRUN pip install django\nCOPY manage.py /app/\n"
    )
    config = ctx / "config"
    config.mkdir()
    (config / "urls.py").write_text(urls_py_content or textwrap.dedent("""
        from django.urls import path
        from . import views
        urlpatterns = [
            path('api/v1/health/', views.health),
            path('admin/', views.admin),
        ]
    """).strip())
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          django:
            build:
              context: django
              dockerfile: Dockerfile
            ports: ['8000:8000']
    """).strip())
    return compose


def test_warns_when_path_not_in_urls_py(tmp_path):
    compose = _scaffold_django(tmp_path)
    import yaml as _y

    compose_obj = _y.safe_load(compose.read_text())
    rc_raw = {
        "services": {
            "django": {"health_check_path": "/api/health/"},
        },
    }
    warnings = detect_unmatched_health_check_path(compose_obj, compose, rc_raw)
    assert len(warnings) == 1
    w = warnings[0]
    assert "/api/health/" in w
    assert "urls.py" in w
    # Should mention the real path it found as a hint.
    assert "api/v1/health/" in w


def test_no_warning_when_path_matches(tmp_path):
    compose = _scaffold_django(tmp_path)
    import yaml as _y

    compose_obj = _y.safe_load(compose.read_text())
    rc_raw = {
        "services": {
            "django": {"health_check_path": "/api/v1/health/"},
        },
    }
    warnings = detect_unmatched_health_check_path(compose_obj, compose, rc_raw)
    assert warnings == []


def test_no_warning_for_root_default(tmp_path):
    compose = _scaffold_django(tmp_path)
    import yaml as _y

    compose_obj = _y.safe_load(compose.read_text())
    rc_raw = {
        "services": {
            "django": {"health_check_path": "/"},
        },
    }
    # '/' is the trivial default; nginx etc serve / by default — skip check.
    warnings = detect_unmatched_health_check_path(compose_obj, compose, rc_raw)
    assert warnings == []


def test_skips_non_django_services(tmp_path):
    # nginx-only service: not django-shaped, don't warn even if path mismatches
    nginx_ctx = tmp_path / "nginx"
    nginx_ctx.mkdir()
    (nginx_ctx / "Dockerfile").write_text("FROM nginx:alpine\n")
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          nginx:
            build:
              context: nginx
              dockerfile: Dockerfile
    """).strip())
    import yaml as _y

    compose_obj = _y.safe_load(compose.read_text())
    rc_raw = {
        "services": {
            "nginx": {"health_check_path": "/healthz"},
        },
    }
    warnings = detect_unmatched_health_check_path(compose_obj, compose, rc_raw)
    assert warnings == []


def test_no_warning_when_no_urls_py(tmp_path):
    # django-shaped but no urls.py to scan — silently skip (avoid false alarms).
    ctx = tmp_path / "django"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        "FROM python:3.11\nWORKDIR /app\nRUN pip install django\nCOPY manage.py /app/\n"
    )
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          django:
            build:
              context: django
              dockerfile: Dockerfile
    """).strip())
    import yaml as _y

    compose_obj = _y.safe_load(compose.read_text())
    rc_raw = {
        "services": {
            "django": {"health_check_path": "/api/health/"},
        },
    }
    warnings = detect_unmatched_health_check_path(compose_obj, compose, rc_raw)
    assert warnings == []
