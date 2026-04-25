"""Multi-environment override handling for Copilot manifests.

Copilot lets services override fields per environment via a top-level
'environments:' block:

    name: api
    cpu: 256
    memory: 512
    environments:
      production:
        cpu: 2048
        memory: 4096
      dev:
        count: 1

When --env is given, those overrides deep-merge onto the base manifest.
When --env is omitted, no overrides are applied (base wins).
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.copilot.discover import CopilotService
from remote_compose.copilot.translate import apply_environment_overrides


def _svc(raw: dict) -> CopilotService:
    return CopilotService(
        name=raw.get("name", "x"),
        type=raw.get("type", "Backend Service"),
        manifest_path=Path("/dev/null"),
        raw=raw,
    )


class TestNoEnvironments:
    def test_no_environments_block_returns_svc_unchanged(self):
        original = _svc({"name": "api", "type": "Backend Service", "cpu": 256})
        result = apply_environment_overrides(original, "production")
        assert result.raw == original.raw

    def test_env_none_returns_svc_unchanged_even_with_overrides(self):
        original = _svc({
            "name": "api", "type": "Backend Service", "cpu": 256,
            "environments": {"production": {"cpu": 2048}},
        })
        result = apply_environment_overrides(original, None)
        assert result.raw["cpu"] == 256

    def test_env_not_in_environments_block_returns_unchanged(self):
        original = _svc({
            "name": "api", "type": "Backend Service", "cpu": 256,
            "environments": {"production": {"cpu": 2048}},
        })
        result = apply_environment_overrides(original, "staging")
        assert result.raw["cpu"] == 256


class TestScalarOverrides:
    def test_cpu_memory_replaced(self):
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service",
            "cpu": 256, "memory": 512,
            "environments": {"production": {"cpu": 2048, "memory": 4096}},
        }), "production")
        assert result.raw["cpu"] == 2048
        assert result.raw["memory"] == 4096

    def test_count_replaced(self):
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service",
            "count": 1,
            "environments": {"production": {"count": 5}},
        }), "production")
        assert result.raw["count"] == 5

    def test_field_only_in_environments_appears_in_merged(self):
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service",
            "environments": {"production": {"count": 4}},
        }), "production")
        assert result.raw["count"] == 4


class TestNestedDictOverrides:
    def test_image_subfield_deep_merged(self):
        # Base has image.build, env overrides image.location.
        # Both should appear in merged (real-world: env may pin a built
        # image instead of building from source).
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service",
            "image": {"build": ".", "port": 8001},
            "environments": {
                "production": {"image": {"location": "ecr/myapp:v1"}},
            },
        }), "production")
        assert result.raw["image"]["build"] == "."
        assert result.raw["image"]["port"] == 8001
        assert result.raw["image"]["location"] == "ecr/myapp:v1"

    def test_http_alias_replaced_at_leaf(self):
        result = apply_environment_overrides(_svc({
            "name": "front-end", "type": "Load Balanced Web Service",
            "image": {"port": 80},
            "http": {"alias": "dev.example.com"},
            "environments": {
                "production": {"http": {"alias": "app.example.com"}},
            },
        }), "production")
        assert result.raw["http"]["alias"] == "app.example.com"

    def test_storage_volumes_deep_merged(self):
        # New env adds another volume; base volume stays.
        result = apply_environment_overrides(_svc({
            "name": "db", "type": "Backend Service",
            "storage": {"volumes": {"data": {"path": "/data", "efs": True}}},
            "environments": {
                "production": {
                    "storage": {"volumes": {
                        "backups": {"path": "/backups", "efs": True}
                    }},
                },
            },
        }), "production")
        assert "data" in result.raw["storage"]["volumes"]
        assert "backups" in result.raw["storage"]["volumes"]


class TestVariablesAndSecretsOverrides:
    def test_variables_per_env_merged(self):
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service",
            "variables": {"FOO": "base", "ENV": "x"},
            "environments": {
                "production": {"variables": {"ENV": "prod", "EXTRA": "1"}},
            },
        }), "production")
        # Per-key merge — production ENV wins, FOO stays from base, EXTRA added.
        assert result.raw["variables"]["FOO"] == "base"
        assert result.raw["variables"]["ENV"] == "prod"
        assert result.raw["variables"]["EXTRA"] == "1"

    def test_secrets_per_env_merged(self):
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service",
            "secrets": {"DB": "arn::base/db"},
            "environments": {
                "production": {"secrets": {"DB": "arn::prod/db", "NEW": "arn::prod/new"}},
            },
        }), "production")
        assert result.raw["secrets"]["DB"] == "arn::prod/db"
        assert result.raw["secrets"]["NEW"] == "arn::prod/new"


class TestEnvironmentsKeyDropped:
    def test_environments_key_removed_from_merged_raw(self):
        # The merged manifest should NOT carry the `environments:` block —
        # we already collapsed it. Leaving it would confuse downstream
        # translators that re-read raw.
        result = apply_environment_overrides(_svc({
            "name": "api", "type": "Backend Service", "cpu": 256,
            "environments": {"production": {"cpu": 2048}},
        }), "production")
        assert "environments" not in result.raw


class TestImmutability:
    def test_original_svc_raw_not_mutated(self):
        # apply_environment_overrides must return a NEW CopilotService
        # without mutating the input — translators may read the original
        # multiple times for different envs.
        original_raw = {
            "name": "api", "type": "Backend Service", "cpu": 256,
            "environments": {"production": {"cpu": 2048}},
        }
        original = _svc(original_raw)
        snapshot = {**original_raw, "environments": {"production": {"cpu": 2048}}}
        apply_environment_overrides(original, "production")
        assert original.raw == snapshot
