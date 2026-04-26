"""Schema tests for services[*].dev_volumes (rc-e5u.45.7).

These cover only the v2 schema parser + validator. The provider's EFS-backed
mount logic is rc-e5u.45.8 (separate bead). The CLI dev-mode gating that
toggles whether dev_volumes are honored at deploy time is rc-e5u.45.9 / .10.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from remote_compose.config.v2_schema import (
    ConfigError,
    ServiceV2,
    parse as parse_v2,
)


def _v2(svc_overrides: str) -> dict:
    """Build a minimal valid v2 rc.yml dict, splicing in service entries."""
    yml = textwrap.dedent(f"""
        version: 2
        project: test
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
        terraform:
          backend:
            type: local
        services:
          {svc_overrides}
    """)
    return yaml.safe_load(yml)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParse:
    def test_no_dev_volumes_field_defaults_to_empty(self):
        cfg = parse_v2(_v2("api: { cpu: 256, memory: 512 }"))
        assert cfg.services["api"].dev_volumes == []

    def test_empty_list_accepted(self):
        cfg = parse_v2(_v2("api: { cpu: 256, memory: 512, dev_volumes: [] }"))
        assert cfg.services["api"].dev_volumes == []

    def test_single_entry_round_trips(self):
        cfg = parse_v2(_v2("""
          api:
            cpu: 256
            memory: 512
            dev_volumes:
              - name: source
                source: ./backend
                mount: /app
        """))
        dv = cfg.services["api"].dev_volumes
        assert len(dv) == 1
        assert dv[0]["name"] == "source"
        assert dv[0]["source"] == "./backend"
        assert dv[0]["mount"] == "/app"

    def test_multiple_entries_round_trip(self):
        cfg = parse_v2(_v2("""
          api:
            cpu: 256
            memory: 512
            dev_volumes:
              - { name: src, source: ./backend, mount: /app }
              - { name: cfg, source: ./config, mount: /etc/app }
        """))
        dv = cfg.services["api"].dev_volumes
        assert [d["name"] for d in dv] == ["src", "cfg"]


# ---------------------------------------------------------------------------
# Validation — required fields
# ---------------------------------------------------------------------------

class TestRequiredFields:
    @pytest.mark.parametrize("missing", ["name", "source", "mount"])
    def test_missing_field_raises(self, missing):
        entry = {"name": "src", "source": "./backend", "mount": "/app"}
        del entry[missing]
        with pytest.raises(ConfigError, match=f"missing required field {missing!r}"):
            parse_v2(_v2(f"""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - {entry!r}
            """))

    def test_non_dict_entry_rejected(self):
        with pytest.raises(ConfigError, match="must be a mapping"):
            parse_v2(_v2("""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - "./backend:/app"
            """))


# ---------------------------------------------------------------------------
# Validation — path semantics
# ---------------------------------------------------------------------------

class TestPathSemantics:
    def test_absolute_source_rejected(self):
        with pytest.raises(ConfigError, match="must be a relative path"):
            parse_v2(_v2("""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - { name: src, source: /Users/me/code, mount: /app }
            """))

    def test_relative_source_accepted(self):
        # Both ./X and X (no leading ./) are relative paths in POSIX terms.
        for src in ["./backend", "backend", "../shared", "subdir/inner"]:
            cfg = parse_v2(_v2(f"""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - {{ name: src, source: '{src}', mount: /app }}
            """))
            assert cfg.services["api"].dev_volumes[0]["source"] == src

    def test_relative_mount_rejected(self):
        with pytest.raises(ConfigError, match="must be an absolute path"):
            parse_v2(_v2("""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - { name: src, source: ./backend, mount: app }
            """))

    def test_absolute_mount_accepted(self):
        cfg = parse_v2(_v2("""
          api:
            cpu: 256
            memory: 512
            dev_volumes:
              - { name: src, source: ./backend, mount: /app }
        """))
        assert cfg.services["api"].dev_volumes[0]["mount"] == "/app"


# ---------------------------------------------------------------------------
# Validation — uniqueness
# ---------------------------------------------------------------------------

class TestUniqueness:
    def test_duplicate_name_rejected(self):
        with pytest.raises(ConfigError, match="declared twice"):
            parse_v2(_v2("""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - { name: src, source: ./a, mount: /a }
                  - { name: src, source: ./b, mount: /b }
            """))

    def test_duplicate_mount_rejected(self):
        with pytest.raises(ConfigError, match="mount.*declared on two entries"):
            parse_v2(_v2("""
              api:
                cpu: 256
                memory: 512
                dev_volumes:
                  - { name: a, source: ./a, mount: /app }
                  - { name: b, source: ./b, mount: /app }
            """))


# ---------------------------------------------------------------------------
# Distinct from `volumes:` (persistent state)
# ---------------------------------------------------------------------------

def test_dev_volumes_separate_from_volumes():
    """Having both fields populated is OK — they cover different mounts.

    Persistent volumes use EFS regardless of dev mode; dev_volumes are only
    materialized in dev mode (enforced at deploy time, not in the schema).
    """
    raw = yaml.safe_load(textwrap.dedent("""
        version: 2
        project: test
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
        terraform:
          backend:
            type: local
        services:
          api:
            cpu: 256
            memory: 512
            volumes:
              - { name: data, mount: /var/lib/postgresql/data }
            dev_volumes:
              - { name: src, source: ./backend, mount: /app }
    """))
    cfg = parse_v2(raw)
    svc = cfg.services["api"]
    assert len(svc.volumes) == 1
    assert len(svc.dev_volumes) == 1
    assert svc.volumes[0]["name"] == "data"
    assert svc.dev_volumes[0]["name"] == "src"
