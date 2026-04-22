"""Unit tests for rc.yml v1 → v2 migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.config import v1_schema
from remote_compose.config.migrate import migrate
from remote_compose.config.v2_schema import parse as parse_v2


FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "rc_v1_samples"


@pytest.fixture
def start_simpli_v1() -> dict:
    return v1_schema.load(FIXTURES_DIR / "start_simpli.yml")


class TestMigrate:
    def test_version_is_2(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.v2["version"] == 2

    def test_project_copied_from_project_name(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.v2["project"] == "ss-debuggai"

    def test_provider_defaults_to_ecs(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.v2["provider"] == "ecs"

    def test_ecs_specific_fields_moved_under_provider_config(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        ecs = result.v2["provider_config"]["ecs"]
        assert ecs["cluster"] == "ss-debuggai-prod"
        assert ecs["region"] == "us-west-2"
        assert ecs["aws_profile"] == "debuggai"
        assert ecs["vpc_cidr"] == "10.0.0.0/16"
        assert ecs["domain"] == "api.startsimpli.com"
        assert ecs["default_launch_type"] == "FARGATE"

    def test_services_preserved(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        svc = result.v2["services"]
        assert set(svc.keys()) == {
            "postgres", "redis", "django", "celery-worker",
            "celery-worker-linkedin", "celery-beat", "nginx",
        }
        assert svc["django"]["cpu"] == 1024
        assert svc["django"]["memory"] == 4096
        assert svc["django"]["type"] == "application"
        assert svc["django"]["health_check_path"] == "/api/health/"
        assert svc["django"]["ephemeral_storage"] == 40

    def test_nginx_public_and_default_target_preserved(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        nginx = result.v2["services"]["nginx"]
        assert nginx["public"] is True
        assert nginx["port"] == 80
        assert nginx["default_target"] is True

    def test_secrets_converted_to_file_refs(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        secrets = result.v2["secrets"]
        assert len(secrets) == 2
        paths = {s["path"] for s in secrets}
        assert paths == {
            ".envs/.production/.django",
            ".envs/.production/.postgres",
        }
        for s in secrets:
            assert s["source"] == "file"
            assert s["name"]

    def test_backup_copied(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.v2["backup"]["bucket"] == "ss-debuggai-db-dumps"
        assert result.v2["backup"]["service"] == "django"

    def test_domain_also_top_level(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.v2["domain"] == "api.startsimpli.com"

    def test_migrated_output_parses_as_valid_v2(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        # full round-trip: migrate → serialize → load → parse
        yaml_text = yaml.safe_dump(result.v2)
        roundtripped = yaml.safe_load(yaml_text)
        cfg = parse_v2(roundtripped)
        assert cfg.project == "ss-debuggai"
        assert "django" in cfg.services

    def test_no_warnings_on_known_v1_fields(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.warnings == []

    def test_no_unmigratable_on_canonical_v1(self, start_simpli_v1):
        result = migrate(start_simpli_v1)
        assert result.unmigratable == []


class TestMigrateEdgeCases:
    def test_unknown_top_level_key_is_unmigratable(self):
        raw = {
            "project_name": "x",
            "compose_file": "docker-compose.yml",
            "services": {},
            "mystery_key": "huh?",
        }
        result = migrate(raw)
        assert any("mystery_key" in msg for msg in result.unmigratable)

    def test_unknown_service_key_is_unmigratable(self):
        raw = {
            "project_name": "x",
            "compose_file": "docker-compose.yml",
            "services": {
                "web": {"cpu": 256, "memory": 512, "type": "proxy",
                        "mystery_field": True},
            },
        }
        result = migrate(raw)
        assert any("mystery_field" in msg for msg in result.unmigratable)

    def test_strict_mode_raises_on_unmigratable(self):
        raw = {
            "project_name": "x",
            "compose_file": "docker-compose.yml",
            "services": {},
            "mystery_key": "huh?",
        }
        with pytest.raises(ValueError, match="strict"):
            migrate(raw, strict=True)

    def test_secret_name_derived_from_filename(self):
        raw = {
            "project_name": "x",
            "compose_file": "docker-compose.yml",
            "secrets": [".envs/.production/.django"],
            "services": {},
        }
        result = migrate(raw)
        assert result.v2["secrets"][0]["name"] == "django"

    def test_is_v1_detects_legacy_config(self):
        assert v1_schema.is_v1({"project_name": "x"}) is True
        assert v1_schema.is_v1({"version": 2}) is False
