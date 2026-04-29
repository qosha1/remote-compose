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

    def test_header_mentions_buildkit_cache_mount_tip(self, tmp_path):
        # rc-e5u.45.2: scaffolded rc.yml header points users at how to
        # add BuildKit `--mount=type=cache,...` directives to their
        # Dockerfile so pip / apt downloads survive layer invalidation.
        compose = self._setup(tmp_path)
        text = generate_v2_rc_yml(compose)
        # The tip lives inside the leading comment block (yaml-safe).
        head = "\n".join(text.splitlines()[:25])
        assert "buildx" in head.lower() or "BuildKit" in head
        assert "--mount=type=cache" in head
        assert "/root/.cache/pip" in head


# ---------------------------------------------------------------------------
# rc-e5u.46.3 — auto-emit lifecycle.migrate on Django services
# ---------------------------------------------------------------------------

class TestLifecycleMigrate:
    """Verify Django-shaped services get a lifecycle.migrate hook auto-emitted.

    The detection reuses ``compose_warnings._looks_like_django_service``,
    which scans a service's Dockerfile for Django markers (manage.py,
    wsgi.py, asgi.py, 'django' as a pip/apt dep). These tests build real
    Dockerfiles in tmp_path so the heuristic fires.
    """

    DJANGO_DOCKERFILE = textwrap.dedent("""
        FROM python:3.11-slim
        RUN pip install django==4.2
        COPY manage.py /app/manage.py
        COPY app/wsgi.py /app/app/wsgi.py
        WORKDIR /app
        CMD ["gunicorn", "app.wsgi:application"]
    """).strip()

    NON_DJANGO_DOCKERFILE = textwrap.dedent("""
        FROM python:3.11-slim
        RUN pip install flask
        COPY app.py /app/app.py
        WORKDIR /app
        CMD ["flask", "run"]
    """).strip()

    def _make_project(self, tmp_path, compose_yaml: str,
                      dockerfiles: dict[str, str]) -> Path:
        """Lay out a project at tmp_path/proj with compose + Dockerfiles.

        ``dockerfiles`` keys are subpath relative to the project root
        (e.g. 'django/Dockerfile' or 'Dockerfile'). The compose yaml
        body should reference the matching build contexts.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "docker-compose.yml").write_text(compose_yaml)
        for rel, content in dockerfiles.items():
            target = proj / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return proj / "docker-compose.yml"

    def _parse(self, text: str) -> dict:
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        return yaml.safe_load(body)

    def test_django_service_gets_lifecycle_migrate(self, tmp_path):
        compose = self._make_project(
            tmp_path,
            textwrap.dedent("""
                services:
                  django:
                    build:
                      context: ./django
                    ports:
                      - "8000:8000"
            """),
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        django = cfg["services"]["django"]
        assert "lifecycle" in django, django
        migrate = django["lifecycle"]["migrate"]
        assert migrate["command"] == [
            "python", "manage.py", "migrate", "--noinput",
        ]
        assert migrate["auto_on_deploy"] is True

    def test_non_django_service_has_no_lifecycle(self, tmp_path):
        # Flask app + a stock postgres image. Neither should get a
        # lifecycle.migrate hook — postgres has no Dockerfile (image-only,
        # build context absent → heuristic returns False), and Flask's
        # Dockerfile lacks Django markers.
        compose = self._make_project(
            tmp_path,
            textwrap.dedent("""
                services:
                  postgres:
                    image: postgres:16-alpine
                  api:
                    build:
                      context: ./api
                    ports:
                      - "5000:5000"
            """),
            {"api/Dockerfile": self.NON_DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        for svc_name, svc in cfg["services"].items():
            assert "lifecycle" not in svc, (svc_name, svc)

    def test_only_application_typed_django_service_gets_migrate(self, tmp_path):
        # rc-e5u.46.6 finding: when worker services share the Django Dockerfile
        # (celery-worker + celery-beat in start-simpli), emitting migrate on
        # ALL of them causes 3 redundant runs + log noise. Only the
        # application-typed service should run migrations — workers are
        # idempotent no-ops at best, racing on lock contention at worst.
        compose = self._make_project(
            tmp_path,
            textwrap.dedent("""
                services:
                  django-app:
                    build:
                      context: ./django
                    ports:
                      - "8000:8000"
                  celery-worker:
                    build:
                      context: ./django
                    command: celery -A app worker
                  celery-beat:
                    build:
                      context: ./django
                    command: celery -A app beat
            """),
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        # django-app: application type, Django-shaped → migrate hook
        assert "lifecycle" in cfg["services"]["django-app"]
        assert cfg["services"]["django-app"]["type"] == "application"
        # celery-worker / celery-beat: worker type, Django-shaped → NO hook
        # (django-app already runs the migrate)
        for worker in ("celery-worker", "celery-beat"):
            svc = cfg["services"][worker]
            assert svc["type"] == "worker"
            assert "lifecycle" not in svc, (worker, svc)

    def test_worker_only_django_stack_gets_one_migrate_hook(self, tmp_path):
        # Edge case: NO application-typed Django service (uncommon — e.g.,
        # an admin-CLI-only stack). The migrate hook still needs SOMEONE
        # to run on. Pick the alpha-first Django-shaped worker so the
        # behavior is deterministic.
        compose = self._make_project(
            tmp_path,
            textwrap.dedent("""
                services:
                  zeta-worker:
                    build:
                      context: ./django
                    command: celery -A app worker
                  alpha-worker:
                    build:
                      context: ./django
                    command: celery -A app beat
            """),
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        assert "lifecycle" in cfg["services"]["alpha-worker"]
        assert "lifecycle" not in cfg["services"]["zeta-worker"]

    def test_generated_yml_validates_against_lifecycle_schema(self, tmp_path):
        # End-to-end: the emitted yml must round-trip through the v2
        # parser without errors AND the LifecycleHookV2 must validate.
        from remote_compose.config.v2_schema import (
            LifecycleHookV2,
            _parse_lifecycle,
        )
        compose = self._make_project(
            tmp_path,
            textwrap.dedent("""
                services:
                  django:
                    build:
                      context: ./django
                    ports:
                      - "8000:8000"
            """),
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        raw_lifecycle = cfg["services"]["django"]["lifecycle"]
        parsed = _parse_lifecycle("django", raw_lifecycle)
        assert "migrate" in parsed
        hook = parsed["migrate"]
        assert isinstance(hook, LifecycleHookV2)
        assert hook.command == ["python", "manage.py", "migrate", "--noinput"]
        assert hook.auto_on_deploy is True
        assert hook.run_once is False
        assert hook.interactive is False
        # validate() should not raise — auto_on_deploy + non-interactive +
        # non-empty list[str] command satisfies every rule.
        hook.validate()

    def test_image_only_service_without_dockerfile_gets_no_hook(self, tmp_path):
        # A service that uses ``image:`` (no build context) cannot be
        # Django-detected because the heuristic reads the Dockerfile.
        # Even if the user names the service 'django', no hook is emitted
        # (matches the underlying _looks_like_django_service contract).
        compose = self._make_project(
            tmp_path,
            textwrap.dedent("""
                services:
                  django:
                    image: my-prebuilt-django:latest
                    ports:
                      - "8000:8000"
            """),
            {},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        assert "lifecycle" not in cfg["services"]["django"]

    def test_django_service_in_synthetic_fixture(self, tmp_path):
        # The SYNTHETIC_COMPOSE used in TestGenerate doesn't ship a
        # Dockerfile, so its 'django' service should NOT get a hook
        # (heuristic needs a real Dockerfile to inspect). This locks in
        # that behavior so future fixture changes are intentional.
        proj = tmp_path / "synth-app"
        proj.mkdir()
        compose = proj / "docker-compose.local.yml"
        compose.write_text(SYNTHETIC_COMPOSE)
        cfg = self._parse(generate_v2_rc_yml(compose))
        # No Dockerfile present → heuristic returns False → no hook.
        assert "lifecycle" not in cfg["services"]["django"]


# ---------------------------------------------------------------------------
# rc-e5u.46.4 — testing_defaults injection (DJANGO_ALLOWED_HOSTS=*, etc.)
# ---------------------------------------------------------------------------


class TestTestingDefaults:
    """Auto-injection of star-host env vars on Django services for ephemeral
    test stacks. Goal: plain `curl http://<ALB>/` returns 200 against an
    rc-test-* deploy without nginx Host: rewrites or hand-edited .envs/
    files. Unsafe for prod — gated on rc-test-* prefix or explicit opt-in.
    """

    DJANGO_DOCKERFILE = textwrap.dedent("""
        FROM python:3.11-slim
        RUN pip install django==4.2
        COPY manage.py /app/manage.py
        WORKDIR /app
    """).strip()

    NON_DJANGO_DOCKERFILE = textwrap.dedent("""
        FROM python:3.11-slim
        RUN pip install flask
        COPY app.py /app/app.py
    """).strip()

    def _make(self, tmp_path, project_name: str, compose_yaml: str,
              dockerfiles: dict[str, str]) -> Path:
        proj = tmp_path / project_name
        proj.mkdir()
        (proj / "docker-compose.yml").write_text(compose_yaml)
        for rel, content in dockerfiles.items():
            target = proj / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return proj / "docker-compose.yml"

    def _parse(self, text: str) -> dict:
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        return yaml.safe_load(body)

    DJANGO_COMPOSE = textwrap.dedent("""
        services:
          django:
            build:
              context: ./django
            ports:
              - "8000:8000"
    """)

    def test_auto_on_for_rc_test_project(self, tmp_path):
        # Project name 'rc-test-foo' → testing_defaults defaults to True →
        # Django service gets an env: block with star-host knobs.
        compose = self._make(
            tmp_path, "rc-test-foo", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        env = cfg["services"]["django"].get("env") or {}
        assert env.get("DJANGO_ALLOWED_HOSTS") == "*"
        assert env.get("CSRF_TRUSTED_ORIGINS") == "*"
        assert env.get("DJANGO_DEBUG") == "False"

    def test_auto_off_for_non_rc_test_project(self, tmp_path):
        # Plain 'myapp' project → testing_defaults stays False → no env: block.
        compose = self._make(
            tmp_path, "myapp", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        assert "env" not in cfg["services"]["django"]

    def test_explicit_true_overrides_auto_off(self, tmp_path):
        # Non-rc-test project but user passed --testing-defaults: opt in.
        compose = self._make(
            tmp_path, "myapp", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose, testing_defaults=True))
        env = cfg["services"]["django"].get("env") or {}
        assert env["DJANGO_ALLOWED_HOSTS"] == "*"

    def test_explicit_false_overrides_auto_on(self, tmp_path):
        # rc-test project but user passed --no-testing-defaults: opt out.
        compose = self._make(
            tmp_path, "rc-test-foo", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose, testing_defaults=False))
        assert "env" not in cfg["services"]["django"]

    def test_skipped_when_compose_already_pins_allowed_hosts_dict(self, tmp_path):
        # Compose already declares DJANGO_ALLOWED_HOSTS in environment: dict
        # form → user is aware → don't shadow with the star-host fallback.
        compose_yaml = textwrap.dedent("""
            services:
              django:
                build:
                  context: ./django
                ports:
                  - "8000:8000"
                environment:
                  DJANGO_ALLOWED_HOSTS: mydomain.com
        """)
        compose = self._make(
            tmp_path, "rc-test-foo", compose_yaml,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        assert "env" not in cfg["services"]["django"]

    def test_skipped_when_compose_already_pins_allowed_hosts_list(self, tmp_path):
        # Compose environment list-form: 'KEY=VALUE' string entries.
        compose_yaml = textwrap.dedent("""
            services:
              django:
                build:
                  context: ./django
                ports:
                  - "8000:8000"
                environment:
                  - DJANGO_ALLOWED_HOSTS=app.example.com
        """)
        compose = self._make(
            tmp_path, "rc-test-foo", compose_yaml,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose))
        assert "env" not in cfg["services"]["django"]

    def test_non_django_service_never_gets_env(self, tmp_path):
        # Even with testing_defaults=True, non-Django services stay clean.
        compose_yaml = textwrap.dedent("""
            services:
              postgres:
                image: postgres:16-alpine
              api:
                build:
                  context: ./api
                ports:
                  - "5000:5000"
        """)
        compose = self._make(
            tmp_path, "rc-test-foo", compose_yaml,
            {"api/Dockerfile": self.NON_DJANGO_DOCKERFILE},
        )
        cfg = self._parse(generate_v2_rc_yml(compose, testing_defaults=True))
        assert "env" not in cfg["services"]["postgres"]
        assert "env" not in cfg["services"]["api"]

    def test_header_mentions_testing_defaults_when_active(self, tmp_path):
        compose = self._make(
            tmp_path, "rc-test-foo", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        text = generate_v2_rc_yml(compose)
        head = "\n".join(text.splitlines()[:20])
        assert "Testing defaults: ON" in head
        assert "DJANGO_ALLOWED_HOSTS" in head
        # Header steers users toward the off-switch.
        assert "--no-testing-defaults" in head

    def test_header_silent_when_inactive(self, tmp_path):
        compose = self._make(
            tmp_path, "myapp", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        text = generate_v2_rc_yml(compose)
        assert "Testing defaults: ON" not in text

    def test_generated_env_round_trips_through_v2_parser(self, tmp_path):
        # The emitted services.<svc>.env must parse back into ServiceV2.env
        # without ConfigError — schema field has to actually exist.
        from remote_compose.config.v2_schema import parse as parse_v2
        compose = self._make(
            tmp_path, "rc-test-foo", self.DJANGO_COMPOSE,
            {"django/Dockerfile": self.DJANGO_DOCKERFILE},
        )
        text = generate_v2_rc_yml(compose)
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        cfg = parse_v2(yaml.safe_load(body))
        django_env = cfg.services["django"].env
        assert django_env["DJANGO_ALLOWED_HOSTS"] == "*"
        assert django_env["CSRF_TRUSTED_ORIGINS"] == "*"
        # YAML parses 'False' as the bool False; parser coerces to str.
        assert django_env["DJANGO_DEBUG"] == "False"
