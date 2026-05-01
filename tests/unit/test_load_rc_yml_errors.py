"""rc-8y6: load_rc_yml error path tests.

YAMLError + schema ConfigError both get the rc.yml file path
prepended, plus YAMLError gets the line+column. Without these
enrichments, users saw 'mapping values are not allowed' or 'service x:
missing required field' with no idea which rc.yml caused it (matters
when there are multiple — rc-td9 — or in CI logs where the cwd is not
obvious).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.cli_v2 import load_rc_yml
from remote_compose.config._schema_types import ConfigError


class TestYamlSyntaxErrors:
    def test_yaml_syntax_error_includes_file_path(self, tmp_path):
        rc = tmp_path / "rc.yml"
        rc.write_text("project: ok\n  bad: indent\n")  # Invalid indent
        with pytest.raises(ConfigError) as info:
            load_rc_yml(rc)
        assert str(rc) in str(info.value)

    def test_yaml_syntax_error_includes_line_number(self, tmp_path):
        rc = tmp_path / "rc.yml"
        rc.write_text("project: ok\n: this is not valid yaml :\n")
        with pytest.raises(ConfigError) as info:
            load_rc_yml(rc)
        # Either line 2 (the actual broken token) or line 3 — we just
        # need *some* line context.
        assert "line " in str(info.value)


class TestSchemaErrorEnrichment:
    def test_schema_error_includes_file_path(self, tmp_path):
        rc = tmp_path / "rc.core.yml"
        rc.write_text(
            "version: 2\n"
            "project: x\n"
            "compose_file: c.yml\n"
            "provider: ecs\n"
            "services:\n"
            "  api:\n"
            "    type: nope-not-a-valid-type\n"
            "    cpu: 256\n"
            "    memory: 512\n"
        )
        with pytest.raises(ConfigError) as info:
            load_rc_yml(rc)
        assert "rc.core.yml" in str(info.value)
        # Original message preserved after the colon.
        assert "type" in str(info.value)


class TestNonMappingError:
    def test_top_level_string_rejected_with_path(self, tmp_path):
        rc = tmp_path / "rc.yml"
        rc.write_text("just a bare string\n")
        with pytest.raises(ConfigError) as info:
            load_rc_yml(rc)
        assert str(rc) in str(info.value)
        assert "must be a mapping" in str(info.value)


class TestHappyPath:
    def test_valid_v2_loads_clean(self, tmp_path):
        rc = tmp_path / "rc.yml"
        rc.write_text(
            "version: 2\n"
            "project: x\n"
            "compose_file: c.yml\n"
            "provider: ecs\n"
            "services:\n"
            "  api:\n"
            "    cpu: 256\n"
            "    memory: 512\n"
            "    type: application\n"
        )
        version, raw, v2 = load_rc_yml(rc)
        assert version == 2
        assert v2 is not None

    def test_valid_v1_loads_clean(self, tmp_path):
        rc = tmp_path / "rc.yml"
        rc.write_text(
            "cluster: my-cluster\n"
            "region: us-west-2\n"
            "project_name: x\n"
            "compose_file: c.yml\n"
            "services:\n"
            "  api:\n"
            "    cpu: 256\n"
            "    memory: 512\n"
        )
        version, raw, v2 = load_rc_yml(rc)
        assert version == 1
        assert v2 is None
