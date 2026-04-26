"""Unit tests for the rc init --from-compose scaffolder."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from remote_compose.init_from_compose import (
    collect_env_files,
    derive_project_name,
    generate_v2_rc_yml,
    infer_cpu_memory,
    infer_service_type,
    pick_public_service,
    secret_name_from_path,
    should_exclude,
)


# ---------------------------------------------------------------------------
# infer_service_type
# ---------------------------------------------------------------------------

class TestInferServiceType:
    def test_postgres_is_infrastructure(self):
        assert infer_service_type("db", {"image": "postgres:16-alpine"}) == "infrastructure"

    def test_redis_is_infrastructure(self):
        assert infer_service_type("cache", {"image": "redis:7-alpine"}) == "infrastructure"

    def test_mysql_is_infrastructure(self):
        assert infer_service_type("db", {"image": "mysql:8.0"}) == "infrastructure"

    def test_nginx_image_is_proxy(self):
        assert infer_service_type("anything", {"image": "nginx:alpine"}) == "proxy"

    def test_traefik_image_is_proxy(self):
        assert infer_service_type("edge", {"image": "traefik:v2.10"}) == "proxy"

    def test_proxy_name_is_proxy(self):
        assert infer_service_type("nginx", {"build": {"context": "."}}) == "proxy"

    def test_celery_command_is_worker(self):
        svc = {"build": {"context": "."}, "command": "celery -A app worker"}
        assert infer_service_type("worker", svc) == "worker"

    def test_celery_command_list_is_worker(self):
        svc = {"build": {"context": "."}, "command": ["celery", "-A", "app", "beat"]}
        assert infer_service_type("beat", svc) == "worker"

    def test_no_ports_with_command_is_worker(self):
        svc = {"build": {"context": "."}, "command": "/start-cron"}
        assert infer_service_type("cron", svc) == "worker"

    def test_built_service_with_ports_is_application(self):
        svc = {"build": {"context": "."}, "ports": ["8000:8000"], "command": "/start"}
        assert infer_service_type("api", svc) == "application"


# ---------------------------------------------------------------------------
# infer_cpu_memory
# ---------------------------------------------------------------------------

def test_infer_cpu_memory_application_largest():
    cpu, mem = infer_cpu_memory("application")
    assert cpu == 1024 and mem == 2048


def test_infer_cpu_memory_proxy_smallest():
    cpu, mem = infer_cpu_memory("proxy")
    assert cpu == 256 and mem == 512


def test_infer_cpu_memory_unknown_falls_back():
    assert infer_cpu_memory("totally-unknown") == (512, 1024)


# ---------------------------------------------------------------------------
# should_exclude
# ---------------------------------------------------------------------------

class TestShouldExclude:
    @pytest.mark.parametrize("name", [
        "celery-worker-linkedin",
        "linkedin-worker",
        "chrome-headed",
        "novnc-bridge",
        "playwright-headed-runner",
        "api-dev",
    ])
    def test_excluded(self, name):
        assert should_exclude(name) is True

    @pytest.mark.parametrize("name", [
        "django",
        "celery-worker",
        "celery-beat",
        "postgres",
        "redis",
        "nginx",
        "developer",  # contains 'dev' but does not END in '-dev'
    ])
    def test_not_excluded(self, name):
        assert should_exclude(name) is False


# ---------------------------------------------------------------------------
# pick_public_service
# ---------------------------------------------------------------------------

class TestPickPublicService:
    def test_picks_by_proxy_name(self):
        services = {
            "django": {"image": "x", "ports": ["8000:8000"]},
            "nginx": {"image": "x", "ports": ["80:80"]},
        }
        assert pick_public_service(services, set()) == "nginx"

    def test_picks_by_proxy_image(self):
        services = {
            "api": {"image": "myapp:1", "ports": ["3000:3000"]},
            "edge": {"image": "caddy:2-alpine"},
        }
        assert pick_public_service(services, set()) == "edge"

    def test_lone_port_80_publisher(self):
        services = {
            "api": {"image": "x", "ports": ["3000:3000"]},
            "frontdoor": {"image": "x", "ports": ["80:80"]},
        }
        assert pick_public_service(services, set()) == "frontdoor"

    def test_two_port_80_publishers_returns_none(self):
        services = {
            "a": {"image": "x", "ports": ["80:80"]},
            "b": {"image": "x", "ports": ["80:80"]},
        }
        assert pick_public_service(services, set()) is None

    def test_override_picks_named_service(self):
        services = {
            "django": {"image": "x", "ports": ["8000:8000"]},
            "nginx": {"image": "nginx:alpine"},
        }
        assert pick_public_service(services, set(), override="django") == "django"

    def test_override_for_unknown_service_returns_none(self):
        services = {"django": {"image": "x"}}
        assert pick_public_service(services, set(), override="nope") is None

    def test_excluded_service_not_picked(self):
        # Two proxy candidates: nginx (excluded) + gateway. Gateway wins.
        services = {
            "nginx": {"image": "nginx:alpine"},
            "gateway": {"image": "x", "ports": ["80:80"]},
        }
        assert pick_public_service(services, {"nginx"}) == "gateway"

    def test_no_candidate_returns_none(self):
        # No proxy name/image, no port 80, nothing to pick.
        services = {
            "api": {"image": "x", "ports": ["3000:3000"]},
            "worker": {"image": "x"},
        }
        assert pick_public_service(services, set()) is None


# ---------------------------------------------------------------------------
# derive_project_name + secret_name_from_path
# ---------------------------------------------------------------------------

class TestDeriveProjectName:
    def test_basic(self, tmp_path):
        p = tmp_path / "my-app" / "docker-compose.yml"
        p.parent.mkdir()
        p.write_text("services: {}")
        assert derive_project_name(p) == "my-app"

    def test_special_chars_slugified(self, tmp_path):
        p = tmp_path / "Some_App.v2" / "docker-compose.yml"
        p.parent.mkdir()
        p.write_text("services: {}")
        assert derive_project_name(p) == "some-app-v2"


class TestSecretNameFromPath:
    @pytest.mark.parametrize("path,expected", [
        (".envs/.local/.django", "local-django"),
        (".envs/.production/.postgres", "production-postgres"),
        ("secrets/api.env", "secrets-api-env"),
        (".env.production", "env-production"),
    ])
    def test_naming(self, path, expected):
        assert secret_name_from_path(path) == expected


# ---------------------------------------------------------------------------
# collect_env_files
# ---------------------------------------------------------------------------

def test_collect_env_files_dedupes_in_order():
    services = {
        "a": {"env_file": [".envs/.local/.django", ".envs/.local/.postgres"]},
        "b": {"env_file": ".envs/.local/.django"},
        "c": {},
    }
    assert collect_env_files(services) == [
        ".envs/.local/.django",
        ".envs/.local/.postgres",
    ]


# ---------------------------------------------------------------------------
# generate_v2_rc_yml — end-to-end on synthetic + real fixture
# ---------------------------------------------------------------------------

SYNTHETIC_COMPOSE = textwrap.dedent("""
services:
  postgres:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
  django:
    build:
      context: .
    ports:
      - "8000:8000"
    command: /start
    env_file:
      - .envs/.local/.django
  celery-worker:
    build:
      context: .
    command: celery -A app worker
  celery-worker-linkedin:
    build:
      context: .
    command: celery -A app worker -Q linkedin
    ports:
      - "6080:6080"
  nginx:
    build:
      context: .
    ports:
      - "80:80"
""")


class TestGenerate:
    def _setup(self, tmp_path):
        proj = tmp_path / "synth-app"
        proj.mkdir()
        compose = proj / "docker-compose.local.yml"
        compose.write_text(SYNTHETIC_COMPOSE)
        return compose

    def test_emits_v2_schema(self, tmp_path):
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose)
        # Drop comments before parsing
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        assert cfg["version"] == 2
        assert cfg["provider"] == "ecs"
        assert cfg["project"] == "synth-app"
        assert cfg["compose_file"] == "docker-compose.local.yml"
        assert cfg["provider_config"]["ecs"]["region"] == "us-west-2"
        assert cfg["provider_config"]["ecs"]["cluster"] == "synth-app-cluster"

    def test_excludes_linkedin_worker(self, tmp_path):
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        assert "celery-worker-linkedin" not in cfg["services"]
        assert "celery-worker-linkedin" in cfg["compose"]["exclude"]

    def test_picks_nginx_as_public(self, tmp_path):
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        assert cfg["services"]["nginx"]["public"] is True
        assert cfg["services"]["nginx"]["port"] == 80
        assert cfg["services"]["nginx"]["default_target"] is True
        # Other services are NOT public
        assert "public" not in cfg["services"]["django"]

    def test_service_types(self, tmp_path):
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        types = {n: s["type"] for n, s in cfg["services"].items()}
        assert types["postgres"] == "infrastructure"
        assert types["redis"] == "infrastructure"
        assert types["django"] == "application"
        assert types["celery-worker"] == "worker"
        assert types["nginx"] == "proxy"

    def test_secrets_block_uses_env_file_auto_when_env_files_present(self, tmp_path):
        # env_file_auto: one declaration covers every env_file across
        # all services. Expansion happens at deploy time in cli_v2.
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        secrets = cfg.get("secrets") or []
        assert len(secrets) == 1, secrets
        assert secrets[0]["source"] == "env_file_auto"
        assert secrets[0]["name"] == "env"
        # env_file_auto requires no path/arn/ref fields
        assert "path" not in secrets[0]

    def test_no_secrets_block_when_no_env_files(self, tmp_path):
        compose = tmp_path / "no-env" / "docker-compose.yml"
        compose.parent.mkdir()
        compose.write_text(
            "services:\n"
            "  api:\n"
            "    image: nginx:alpine\n"
            "    ports: ['80:80']\n"
        )
        text = generate_v2_rc_yml(compose)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        assert "secrets" not in cfg

    def test_aws_profile_included_when_set(self, tmp_path):
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose, aws_profile="default")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        assert cfg["provider_config"]["ecs"]["aws_profile"] == "default"

    def test_region_override(self, tmp_path):
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose, region="us-west-1")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = yaml.safe_load(body)
        assert cfg["provider_config"]["ecs"]["region"] == "us-west-1"

    def test_empty_compose_raises(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services: {}")
        with pytest.raises(ValueError, match="no services found"):
            generate_v2_rc_yml(compose)
