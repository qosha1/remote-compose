"""rc compose import: scaffold a starter rc.yml from a docker-compose.yml.

The auto-import path (rc-e5u.41.1/.2) means rc.yml services[] is OPTIONAL —
compose drives the deploy set. This command produces a starting-point
rc.yml the user can hand-edit: project + provider + provider_config
shell, plus *commented* per-service hints for anything we can detect
(public service, db service, etc.).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.compose_import import scaffold_rc_yml


def _write_compose(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "docker-compose.yml"
    p.write_text("services:\n" + body)
    return p


# ---------------------------------------------------------------------
# Top-level rc.yml shape
# ---------------------------------------------------------------------


class TestTopLevelShape:
    def test_minimal_compose_emits_valid_v2(self, tmp_path):
        _write_compose(tmp_path, "  api:\n    image: busybox\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="myapp")
        cfg = yaml.safe_load(out)
        assert cfg["version"] == 2
        assert cfg["project"] == "myapp"
        assert cfg["compose_file"] == "docker-compose.yml"
        assert cfg["provider"] == "ecs"

    def test_provider_config_has_ecs_defaults(self, tmp_path):
        _write_compose(tmp_path, "  api:\n    image: busybox\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        ecs = cfg["provider_config"]["ecs"]
        assert ecs["cluster"] == "m-cluster"
        assert "region" in ecs
        assert "vpc_cidr" in ecs

    def test_terraform_backend_defaults_to_local(self, tmp_path):
        _write_compose(tmp_path, "  api:\n    image: busybox\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        assert cfg["terraform"]["backend"]["type"] == "local"


# ---------------------------------------------------------------------
# Service inference — public-vs-private, db hints, framework hints
# ---------------------------------------------------------------------


class TestServiceInference:
    def test_service_with_ports_marked_public(self, tmp_path):
        _write_compose(tmp_path, "  web:\n    image: nginx\n    ports: ['80:80']\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        web = cfg["services"]["web"]
        assert web["public"] is True
        assert web["port"] == 80

    def test_service_without_ports_no_public_field(self, tmp_path):
        _write_compose(tmp_path, "  worker:\n    image: busybox\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        # Workers don't get public: true.
        assert "public" not in cfg["services"].get("worker", {})

    def test_db_service_gets_infrastructure_type(self, tmp_path):
        _write_compose(tmp_path, "  postgres:\n    image: postgres:17\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        assert cfg["services"]["postgres"]["type"] == "infrastructure"

    def test_redis_recognized_as_infrastructure(self, tmp_path):
        _write_compose(tmp_path, "  redis:\n    image: redis:7\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        assert cfg["services"]["redis"]["type"] == "infrastructure"

    def test_postgres_gets_volume_suggestion(self, tmp_path):
        _write_compose(tmp_path, "  postgres:\n    image: postgres:17\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        vols = cfg["services"]["postgres"].get("volumes") or []
        assert any(v.get("mount") == "/var/lib/postgresql/data" for v in vols)

    def test_celery_worker_gets_worker_type(self, tmp_path):
        _write_compose(tmp_path, "  celeryworker:\n    image: busybox\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        cfg = yaml.safe_load(out)
        assert cfg["services"]["celeryworker"]["type"] == "worker"


# ---------------------------------------------------------------------
# Compose env_file refs surface as a comment (since we don't auto-create
# rc.yml secrets entries — user has to opt in).
# ---------------------------------------------------------------------


class TestEnvFileSurfacing:
    def test_env_file_listed_in_summary(self, tmp_path):
        _write_compose(
            tmp_path,
            "  api:\n    image: busybox\n    env_file:\n      - .envs/.production/.django\n",
        )
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        # We expect a comment near the secrets: stub mentioning the env_file.
        assert ".envs/.production/.django" in out


# ---------------------------------------------------------------------
# Defaults: project from cwd, compose path resolution
# ---------------------------------------------------------------------


class TestDefaults:
    def test_project_defaults_to_compose_parent_dir_name(self, tmp_path):
        appdir = tmp_path / "myproject"
        appdir.mkdir()
        _write_compose(appdir, "  api:\n    image: busybox\n")
        out = scaffold_rc_yml(appdir / "docker-compose.yml")
        cfg = yaml.safe_load(out)
        assert cfg["project"] == "myproject"

    def test_explicit_project_wins(self, tmp_path):
        _write_compose(tmp_path, "  api:\n    image: busybox\n")
        out = scaffold_rc_yml(tmp_path / "docker-compose.yml", project="explicit")
        cfg = yaml.safe_load(out)
        assert cfg["project"] == "explicit"


# ---------------------------------------------------------------------
# --exclude (rc-e5u.41.3): drop dev-only sidecars from the deploy set
# ---------------------------------------------------------------------


class TestExcludeFlag:
    def test_excluded_services_appear_in_compose_exclude_block(self, tmp_path):
        _write_compose(
            tmp_path,
            "  api:\n    image: busybox\n"
            "  ngrok:\n    image: ngrok/ngrok:latest\n"
            "  docs:\n    image: busybox\n",
        )
        out = scaffold_rc_yml(
            tmp_path / "docker-compose.yml",
            project="m",
            exclude=["ngrok", "docs"],
        )
        cfg = yaml.safe_load(out)
        # Stable sorted order in output for diff-friendly behavior.
        assert cfg["compose"]["exclude"] == ["docs", "ngrok"]

    def test_excluded_services_not_in_overrides_block(self, tmp_path):
        # ngrok is a worker-shaped image with worker hint in name; without
        # --exclude it'd land in services{}. With --exclude it must be
        # entirely absent from services{}.
        _write_compose(
            tmp_path,
            "  worker-cron:\n    image: app:latest\n"
            "  api:\n    image: busybox\n    ports: ['8000:8000']\n",
        )
        out = scaffold_rc_yml(
            tmp_path / "docker-compose.yml",
            project="m",
            exclude=["worker-cron"],
        )
        cfg = yaml.safe_load(out)
        # api still gets the public+port override; worker-cron is gone.
        assert "api" in cfg.get("services", {})
        assert "worker-cron" not in cfg.get("services", {})
        assert cfg["compose"]["exclude"] == ["worker-cron"]

    def test_unknown_excluded_service_raises(self, tmp_path):
        _write_compose(tmp_path, "  api:\n    image: busybox\n")
        with pytest.raises(ValueError, match="not in compose"):
            scaffold_rc_yml(
                tmp_path / "docker-compose.yml",
                project="m",
                exclude=["does-not-exist"],
            )

    def test_empty_exclude_list_is_no_op(self, tmp_path):
        _write_compose(tmp_path, "  api:\n    image: busybox\n")
        out = scaffold_rc_yml(
            tmp_path / "docker-compose.yml",
            project="m",
            exclude=[],
        )
        cfg = yaml.safe_load(out)
        assert "compose" not in cfg


# ---------------------------------------------------------------------
# Round-trip: emitted rc.yml parses through the v2 schema
# ---------------------------------------------------------------------


class TestRoundTrip:
    def test_emitted_rc_yml_parses_through_v2_schema(self, tmp_path):
        _write_compose(
            tmp_path,
            "  postgres:\n    image: postgres:17\n"
            "  redis:\n    image: redis:7\n"
            "  django:\n    image: busybox\n    ports: ['8001:8001']\n"
            "  celeryworker:\n    image: busybox\n",
        )
        out_path = tmp_path / "rc.yml"
        out_path.write_text(
            scaffold_rc_yml(tmp_path / "docker-compose.yml", project="m")
        )

        from remote_compose.config.v2_schema import load

        cfg = load(out_path)
        assert cfg.project == "m"
        assert "postgres" in cfg.services
        assert cfg.services["django"].public is True
        assert cfg.services["django"].port == 8001
