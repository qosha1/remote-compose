"""Tests for `rc up` auto-orchestrating `rc fix nginx-conf` (rc-e5u.46.2).

When the user's compose has an nginx.conf that trips the .44.18 detector
(``upstream { server X:port; }`` without a resolver directive) AND the
upstream resolves to a Django-shaped service (.44.19), `rc up` should
silently chain the same logic as `rc fix nginx-conf`:

  1. Generate compose/ecs/nginx/{Dockerfile,nginx.conf} in the user's
     project (write_ecs_nginx).
  2. Patch services.<nginx>.dockerfile in the just-written rc.yml so
     build_deploy_context (.46.1) builds the ECS-aware image.
  3. Print a short "auto-fixed" notice. Deploy continues.

These tests exercise the helper directly + the full click flow; the
deploy + secrets-push paths remain mocked (real-AWS verification is out
of scope for unit tests, same as test_cli_up.py).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.init_from_compose import (
    auto_fix_nginx_if_needed,
    detect_nginx_auto_fix_target,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_django_dockerfile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent("""\
        FROM python:3.11-slim
        RUN pip install django gunicorn
        COPY manage.py /app/
        CMD ["python", "manage.py", "runserver"]
    """))


def _write_nginx_dockerfile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent("""\
        FROM nginx:1.25-alpine
        COPY nginx.conf /etc/nginx/nginx.conf
    """))


def _write_nginx_conf_with_upstream(path: Path, upstream_host: str = "django",
                                    upstream_port: int = 8000) -> None:
    """nginx.conf that trips .44.18: upstream block without resolver."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(f"""\
        events {{ worker_connections 1024; }}
        http {{
            upstream backend {{
                server {upstream_host}:{upstream_port};
            }}
            server {{
                listen 80;
                location / {{
                    proxy_pass http://backend;
                }}
            }}
        }}
    """))


def _make_django_stack(tmp_path: Path, upstream_host: str = "django") -> Path:
    """Create a tmp_path layout: compose with nginx + Django service.

    Returns the docker-compose.yml path.
    """
    proj = tmp_path
    # Django service at ./compose/django
    _write_django_dockerfile(proj / "compose" / "django" / "Dockerfile")
    # nginx service at ./compose/local/nginx
    _write_nginx_dockerfile(proj / "compose" / "local" / "nginx" / "Dockerfile")
    _write_nginx_conf_with_upstream(
        proj / "compose" / "local" / "nginx" / "nginx.conf",
        upstream_host=upstream_host,
        upstream_port=8000,
    )
    compose_path = proj / "docker-compose.yml"
    compose_path.write_text(textwrap.dedent(f"""
        services:
          {upstream_host}:
            build:
              context: ./compose/django
            ports:
              - "8000:8000"
          nginx:
            build:
              context: ./compose/local/nginx
            ports:
              - "80:80"
    """))
    return compose_path


def _make_rc_yml(tmp_path: Path, compose_rel: str = "docker-compose.yml",
                 project: str = "myapp", vpc_cidr: str = "10.42.0.0/16") -> Path:
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text(textwrap.dedent(f"""\
        # rc.yml — generated header
        version: 2
        project: {project}
        compose_file: {compose_rel}
        provider: ecs
        provider_config:
          ecs:
            region: us-west-2
            vpc_cidr: {vpc_cidr}
        terraform:
          backend: {{type: local}}
        services:
          nginx:
            cpu: 256
            memory: 512
            public: true
            port: 80
          django:
            cpu: 512
            memory: 1024
    """))
    return rc_yml


# ---------------------------------------------------------------------------
# detect_nginx_auto_fix_target — pure detection logic
# ---------------------------------------------------------------------------


class TestDetectNginxAutoFixTarget:
    def test_returns_target_when_resolver_missing_and_django_upstream(self, tmp_path):
        compose = _make_django_stack(tmp_path)
        rc_yml = _make_rc_yml(tmp_path)
        rc_raw = yaml.safe_load(rc_yml.read_text())
        target = detect_nginx_auto_fix_target(compose, rc_raw)
        assert target is not None
        assert target["nginx_service"] == "nginx"
        assert target["project"] == "myapp"
        assert target["vpc_cidr"] == "10.42.0.0/16"
        assert len(target["upstreams"]) == 1
        u = target["upstreams"][0]
        assert u.name == "django"
        assert u.port == 8000
        assert u.django is True

    def test_returns_none_when_resolver_already_present(self, tmp_path):
        compose = _make_django_stack(tmp_path)
        # Add a resolver line to suppress the warning (user already fixed it).
        nginx_conf = tmp_path / "compose" / "local" / "nginx" / "nginx.conf"
        nginx_conf.write_text(
            "http {\n  resolver 10.42.0.2 valid=10s ipv6=off;\n"
            "  upstream backend { server django:8000; }\n}\n"
        )
        rc_yml = _make_rc_yml(tmp_path)
        rc_raw = yaml.safe_load(rc_yml.read_text())
        assert detect_nginx_auto_fix_target(compose, rc_raw) is None

    def test_returns_none_when_no_django_upstream(self, tmp_path):
        # Upstream is a non-Django service (plain Node/Rails-shaped).
        proj = tmp_path
        # No manage.py / wsgi / django markers in the upstream Dockerfile.
        (proj / "compose" / "api").mkdir(parents=True)
        (proj / "compose" / "api" / "Dockerfile").write_text(
            "FROM node:18\nCMD [\"node\", \"server.js\"]\n"
        )
        _write_nginx_dockerfile(proj / "compose" / "local" / "nginx" / "Dockerfile")
        _write_nginx_conf_with_upstream(
            proj / "compose" / "local" / "nginx" / "nginx.conf",
            upstream_host="api",
        )
        compose = proj / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              api:
                build: {context: ./compose/api}
                ports: ["8000:8000"]
              nginx:
                build: {context: ./compose/local/nginx}
                ports: ["80:80"]
        """))
        rc_yml = _make_rc_yml(tmp_path)
        rc_raw = yaml.safe_load(rc_yml.read_text())
        assert detect_nginx_auto_fix_target(compose, rc_raw) is None

    def test_monorepo_compose_picks_proxy_not_django(self, tmp_path):
        """Regression test for the .46.6 dogfood failure: when ALL services
        share build.context = '.' (monorepo style — start-simpli, sentinal,
        etc.), every service's context-glob finds nginx.conf at
        compose/local/nginx/nginx.conf. Detector MUST pick the proxy-shaped
        service, not whichever happens to come first in the compose dict.
        Verified the hard way: mis-targeting django caused django to get
        built FROM nginx:1.25-alpine + the migrate hook to die because
        there's no python in the resulting image."""
        proj = tmp_path
        _write_django_dockerfile(proj / "compose" / "django" / "Dockerfile")
        _write_nginx_dockerfile(proj / "compose" / "local" / "nginx" / "Dockerfile")
        _write_nginx_conf_with_upstream(
            proj / "compose" / "local" / "nginx" / "nginx.conf",
            upstream_host="django",
            upstream_port=8000,
        )
        compose = proj / "docker-compose.yml"
        # Monorepo layout: every service uses context: .
        compose.write_text(textwrap.dedent("""
            services:
              django:
                build:
                  context: .
                  dockerfile: ./compose/django/Dockerfile
                ports: ["8000:8000"]
              celery-worker:
                build:
                  context: .
                  dockerfile: ./compose/django/Dockerfile
                command: celery -A app worker
              nginx:
                build:
                  context: .
                  dockerfile: ./compose/local/nginx/Dockerfile
                ports: ["80:80"]
        """))
        rc_yml = _make_rc_yml(tmp_path)
        rc_raw = yaml.safe_load(rc_yml.read_text())
        target = detect_nginx_auto_fix_target(compose, rc_raw)
        assert target is not None
        # MUST pick nginx, not django (which appears first in the compose
        # services dict).
        assert target["nginx_service"] == "nginx"

    def test_returns_none_when_upstream_is_external_host(self, tmp_path):
        # `server api.external.com:443;` doesn't match a compose service →
        # detector skips. Even if it would have been Django.
        proj = tmp_path
        _write_django_dockerfile(proj / "compose" / "django" / "Dockerfile")
        _write_nginx_dockerfile(proj / "compose" / "local" / "nginx" / "Dockerfile")
        _write_nginx_conf_with_upstream(
            proj / "compose" / "local" / "nginx" / "nginx.conf",
            upstream_host="api.external.com",  # not a compose service name
            upstream_port=443,
        )
        compose = proj / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              django:
                build: {context: ./compose/django}
              nginx:
                build: {context: ./compose/local/nginx}
        """))
        rc_yml = _make_rc_yml(tmp_path)
        rc_raw = yaml.safe_load(rc_yml.read_text())
        assert detect_nginx_auto_fix_target(compose, rc_raw) is None


# ---------------------------------------------------------------------------
# auto_fix_nginx_if_needed — the orchestrator
# ---------------------------------------------------------------------------


class TestAutoFixNginxIfNeeded:
    def test_writes_ecs_nginx_files_in_user_project(self, tmp_path):
        compose = _make_django_stack(tmp_path)
        rc_yml = _make_rc_yml(tmp_path)
        result = auto_fix_nginx_if_needed(rc_yml, compose)
        assert result is not None
        # Files generated in the user's project (NOT /tmp), under the
        # compose/ecs/nginx convention next to compose/local/nginx.
        ecs_dir = tmp_path / "compose" / "ecs" / "nginx"
        assert (ecs_dir / "Dockerfile").is_file()
        assert (ecs_dir / "nginx.conf").is_file()
        assert result["nginx_path"] == ecs_dir / "nginx.conf"
        assert result["dockerfile_path"] == ecs_dir / "Dockerfile"
        # nginx.conf has the resolver IP derived from rc.yml's vpc_cidr.
        conf = (ecs_dir / "nginx.conf").read_text()
        assert "resolver 10.42.0.2 valid=10s ipv6=off" in conf
        # Django upstream → Host header rewrite.
        assert "proxy_set_header Host localhost;" in conf
        # FQDN form using project name.
        assert 'set $u "django.myapp.local:8000"' in conf

    def test_patches_rc_yml_with_dockerfile_override(self, tmp_path):
        compose = _make_django_stack(tmp_path)
        rc_yml = _make_rc_yml(tmp_path)
        auto_fix_nginx_if_needed(rc_yml, compose)
        raw = yaml.safe_load(rc_yml.read_text())
        assert raw["services"]["nginx"]["dockerfile"] == \
            "./compose/ecs/nginx/Dockerfile"
        # Other fields preserved.
        assert raw["services"]["nginx"]["public"] is True
        assert raw["services"]["nginx"]["port"] == 80
        assert raw["services"]["django"]["cpu"] == 512
        assert raw["project"] == "myapp"
        # Header comments survive the rewrite (we split on first non-comment line).
        assert rc_yml.read_text().startswith("# rc.yml")

    def test_returns_none_and_no_files_when_resolver_present(self, tmp_path):
        compose = _make_django_stack(tmp_path)
        nginx_conf = tmp_path / "compose" / "local" / "nginx" / "nginx.conf"
        nginx_conf.write_text(
            "http {\n  resolver 10.0.0.2;\n"
            "  upstream backend { server django:8000; }\n}\n"
        )
        rc_yml = _make_rc_yml(tmp_path)
        result = auto_fix_nginx_if_needed(rc_yml, compose)
        assert result is None
        assert not (tmp_path / "compose" / "ecs").exists()
        # rc.yml unchanged.
        raw = yaml.safe_load(rc_yml.read_text())
        assert "dockerfile" not in raw["services"]["nginx"]

    def test_returns_none_when_no_django_upstream(self, tmp_path):
        # Plain non-Django stack — auto-fix should NOT clobber existing
        # nginx config because the rc fix's Django Host header rewrite
        # doesn't apply and we'd rather punt to the .44.18 warning text.
        proj = tmp_path
        (proj / "compose" / "api").mkdir(parents=True)
        (proj / "compose" / "api" / "Dockerfile").write_text(
            "FROM node:18\nCMD [\"node\", \"server.js\"]\n"
        )
        _write_nginx_dockerfile(proj / "compose" / "local" / "nginx" / "Dockerfile")
        _write_nginx_conf_with_upstream(
            proj / "compose" / "local" / "nginx" / "nginx.conf",
            upstream_host="api",
        )
        compose = proj / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              api:
                build: {context: ./compose/api}
                ports: ["8000:8000"]
              nginx:
                build: {context: ./compose/local/nginx}
                ports: ["80:80"]
        """))
        rc_yml = _make_rc_yml(tmp_path)
        result = auto_fix_nginx_if_needed(rc_yml, compose)
        assert result is None
        assert not (tmp_path / "compose" / "ecs").exists()

    def test_idempotent_rerun_overwrites(self, tmp_path):
        # write_ecs_nginx is invoked with force=True so a re-run after the
        # user edits the upstream port still regenerates cleanly.
        compose = _make_django_stack(tmp_path)
        rc_yml = _make_rc_yml(tmp_path)
        first = auto_fix_nginx_if_needed(rc_yml, compose)
        assert first is not None
        # Second run on the same inputs is a no-op for content but must
        # not raise FileExistsError.
        second = auto_fix_nginx_if_needed(rc_yml, compose)
        assert second is not None
        # rc.yml dockerfile entry only present once.
        raw = yaml.safe_load(rc_yml.read_text())
        assert raw["services"]["nginx"]["dockerfile"] == \
            "./compose/ecs/nginx/Dockerfile"


# ---------------------------------------------------------------------------
# Full click flow: `rc up --from-compose <path>` triggers the auto-fix
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


def test_rc_up_from_compose_auto_fixes_nginx_for_django(runner, tmp_path):
    """End-to-end via Click: rc up scaffolds, detects, auto-fixes, deploys."""
    compose = _make_django_stack(tmp_path)
    rc_yml = tmp_path / "rc.yml"
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli._secrets_push_v2", return_value=True):
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "up", "--from-compose", str(compose),
             "--region", "us-west-1"],
        )
    assert result.exit_code == 0, result.output
    # The auto-fix notice fired.
    assert "auto-fixed nginx config for ECS" in result.output
    assert "compose/ecs/nginx" in result.output
    # Files exist in the user's project.
    ecs_dir = tmp_path / "compose" / "ecs" / "nginx"
    assert (ecs_dir / "Dockerfile").is_file()
    assert (ecs_dir / "nginx.conf").is_file()
    # rc.yml has the dockerfile override.
    raw = yaml.safe_load(rc_yml.read_text())
    assert raw["services"]["nginx"]["dockerfile"] == \
        "./compose/ecs/nginx/Dockerfile"


def test_rc_up_from_compose_skips_auto_fix_when_no_django(runner, tmp_path):
    proj = tmp_path
    (proj / "compose" / "api").mkdir(parents=True)
    (proj / "compose" / "api" / "Dockerfile").write_text(
        "FROM node:18\nCMD [\"node\", \"server.js\"]\n"
    )
    _write_nginx_dockerfile(proj / "compose" / "local" / "nginx" / "Dockerfile")
    _write_nginx_conf_with_upstream(
        proj / "compose" / "local" / "nginx" / "nginx.conf",
        upstream_host="api",
    )
    compose = proj / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          api:
            build: {context: ./compose/api}
            ports: ["8000:8000"]
          nginx:
            build: {context: ./compose/local/nginx}
            ports: ["80:80"]
    """))
    rc_yml = tmp_path / "rc.yml"
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli._secrets_push_v2", return_value=True):
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "up", "--from-compose", str(compose)],
        )
    assert result.exit_code == 0, result.output
    assert "auto-fixed nginx config" not in result.output
    assert not (tmp_path / "compose" / "ecs").exists()


def test_rc_up_from_compose_skips_auto_fix_when_resolver_present(runner, tmp_path):
    compose = _make_django_stack(tmp_path)
    nginx_conf = tmp_path / "compose" / "local" / "nginx" / "nginx.conf"
    nginx_conf.write_text(
        "http {\n  resolver 10.42.0.2;\n"
        "  upstream backend { server django:8000; }\n}\n"
    )
    rc_yml = tmp_path / "rc.yml"
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli._secrets_push_v2", return_value=True):
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "up", "--from-compose", str(compose)],
        )
    assert result.exit_code == 0, result.output
    assert "auto-fixed nginx config" not in result.output


def test_rc_up_existing_rcyml_resolves_compose_from_rc_yml(runner, tmp_path):
    """When rc.yml already exists (no --from-compose), the auto-fix still
    runs by resolving compose_file from the rc.yml. Self-healing on a
    re-run after the user edits their nginx.conf.
    """
    compose = _make_django_stack(tmp_path)
    rc_yml = _make_rc_yml(tmp_path, compose_rel="docker-compose.yml")
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli._secrets_push_v2", return_value=True):
        result = runner.invoke(cli, ["-c", str(rc_yml), "up"])
    assert result.exit_code == 0, result.output
    assert "auto-fixed nginx config" in result.output
    raw = yaml.safe_load(rc_yml.read_text())
    assert raw["services"]["nginx"]["dockerfile"] == \
        "./compose/ecs/nginx/Dockerfile"


def test_rc_up_auto_fix_failure_does_not_abort_deploy(runner, tmp_path):
    """If the auto-fix raises (e.g. write_ecs_nginx blows up on a
    permissions error), `rc up` should warn but still proceed with the
    deploy. The ECS deploy itself might still fail on the .44.18 issue
    but the user gets a clear warning instead of a hard abort.
    """
    compose = _make_django_stack(tmp_path)
    rc_yml = tmp_path / "rc.yml"
    with patch("remote_compose.init_from_compose.auto_fix_nginx_if_needed",
               side_effect=RuntimeError("disk full")), \
         patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli._secrets_push_v2", return_value=True):
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "up", "--from-compose", str(compose)],
        )
    assert result.exit_code == 0, result.output
    assert "auto-fix skipped" in result.output.lower() or \
        "warn" in result.output.lower()
