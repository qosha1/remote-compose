"""rc-6au: detect_dev_mode_command warns when a compose service's
``command:`` is a dev-mode runner that's unstable on ECS.

Sentinal repro: services.django.command = '/start' which exec'd
'uvicorn … --reload …'. WatchFiles flaked or briefly stopped accepting
during reloads — ALB saw connection refused, killed every task, stack
flapped indefinitely.

This detector catches the common case where the dev runner is in
compose ``command:`` directly (the easy half — Dockerfile-baked dev
commands need image-side detection, tracked separately).
"""

from __future__ import annotations

import textwrap

import yaml

from remote_compose.compose_warnings import detect_dev_mode_command


def _compose(yaml_text: str) -> dict:
    return yaml.safe_load(textwrap.dedent(yaml_text).strip()) or {}


class TestUvicornReload:
    def test_warns_on_uvicorn_reload_string_form(self):
        compose = _compose("""
            services:
              django:
                image: django:test
                command: uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1
        assert "django" in warnings[0]
        assert "--reload" in warnings[0]

    def test_warns_on_uvicorn_reload_list_form(self):
        compose = _compose("""
            services:
              django:
                image: django:test
                command:
                  - uvicorn
                  - config.asgi:application
                  - --host
                  - 0.0.0.0
                  - --reload
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1
        assert "--reload" in warnings[0]


class TestDjangoRunserver:
    def test_warns_on_manage_py_runserver(self):
        compose = _compose("""
            services:
              django:
                image: django:test
                command: python manage.py runserver 0.0.0.0:8000
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1
        assert "runserver" in warnings[0]


class TestFlaskDebug:
    def test_warns_on_flask_run_debug(self):
        compose = _compose("""
            services:
              api:
                image: flask:test
                command: flask run --debug --host 0.0.0.0
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1
        assert "flask" in warnings[0].lower()


class TestNodeNextDev:
    def test_warns_on_next_dev(self):
        compose = _compose("""
            services:
              web:
                image: node:20
                command: npm run dev
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1
        assert "dev" in warnings[0]

    def test_warns_on_next_dev_direct(self):
        compose = _compose("""
            services:
              web:
                image: node:20
                command: next dev
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1


class TestProductionCommandsSilent:
    def test_silent_for_gunicorn(self):
        compose = _compose("""
            services:
              api:
                image: x
                command: gunicorn config.wsgi --bind 0.0.0.0:8000 --workers 4
        """)
        assert detect_dev_mode_command(compose) == []

    def test_silent_for_uvicorn_workers(self):
        compose = _compose("""
            services:
              api:
                image: x
                command: uvicorn config.asgi:app --host 0.0.0.0 --port 8000 --workers 2
        """)
        assert detect_dev_mode_command(compose) == []

    def test_silent_for_no_command(self):
        # No command at all → nothing to flag (Dockerfile CMD covers boot).
        compose = _compose("""
            services:
              api:
                image: x
        """)
        assert detect_dev_mode_command(compose) == []

    def test_silent_for_npm_start(self):
        compose = _compose("""
            services:
              web:
                image: node:20
                command: npm start
        """)
        assert detect_dev_mode_command(compose) == []


class TestEdgeCases:
    def test_empty_compose(self):
        assert detect_dev_mode_command({}) == []

    def test_no_services_key(self):
        assert detect_dev_mode_command({"version": "3.9"}) == []

    def test_multiple_services_with_mixed_commands(self):
        compose = _compose("""
            services:
              django:
                image: django:test
                command: python manage.py runserver
              api:
                image: x
                command: gunicorn config.wsgi
              celery:
                image: celery:test
                command: celery -A app worker
        """)
        warnings = detect_dev_mode_command(compose)
        assert len(warnings) == 1
        assert "django" in warnings[0]
