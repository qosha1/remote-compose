"""`rc outputs` — the consumer-facing half of the declared-network feature.

Declared resources exist so something outside rc can attach to them, which
only works if their ids come back out in a shape a script can consume.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from remote_compose.cli_commands.outputs import _flatten, _render_table, outputs_cmd

pytestmark = pytest.mark.unit


OUTPUTS = {
    "cluster_name": "bmgr-cluster",
    "vpc_id": "vpc-0a1b2c",
    "security_groups": {"runners": "sg-0d4e5f", "api": "sg-0aa111"},
    "subnets": {"runners-private": ["subnet-0aa", "subnet-0bb"]},
    "subnet_egress_modes": {"runners-private": "endpoints"},
    "vpc_endpoints": {"ecr.ecr.api": "vpce-1", "s3.s3": "vpce-2"},
    "repositories": {"db-sidecar": "1234.dkr.ecr.us-west-2.amazonaws.com/bmgr/db"},
}


def _env(value, *parts, prefix="RC_"):
    return dict(_flatten(value, prefix, *parts))


class TestEnvFlattening:
    def test_scalar(self):
        assert _env("vpc-1", "vpc_id") == {"RC_VPC_ID": "vpc-1"}

    def test_map_keys_become_name_suffixes(self):
        assert _env(OUTPUTS["security_groups"], "security_groups") == {
            "RC_SECURITY_GROUPS_RUNNERS": "sg-0d4e5f",
            "RC_SECURITY_GROUPS_API": "sg-0aa111",
        }

    def test_scalar_lists_are_comma_joined(self):
        """A subnet list is consumed as one value (RUNNER_PRIVATE_SUBNETS),
        not as indexed variables."""
        assert _env(OUTPUTS["subnets"], "subnets") == {
            "RC_SUBNETS_RUNNERS_PRIVATE": "subnet-0aa,subnet-0bb"
        }

    def test_dotted_and_hyphenated_keys_are_sanitized(self):
        out = _env(OUTPUTS["vpc_endpoints"], "vpc_endpoints")
        assert out["RC_VPC_ENDPOINTS_ECR_ECR_API"] == "vpce-1"
        assert all(k.replace("_", "").isalnum() for k in out)

    def test_prefix_is_configurable(self):
        assert _env("x", "vpc_id", prefix="") == {"VPC_ID": "x"}
        assert _env("x", "vpc_id", prefix="RUNNER_") == {"RUNNER_VPC_ID": "x"}

    def test_booleans_and_none_render_as_scalars(self):
        assert _env(True, "flag") == {"RC_FLAG": "true"}
        assert _env(None, "missing") == {"RC_MISSING": ""}

    def test_nested_lists_of_maps_are_indexed(self):
        assert _env([{"a": 1}, {"a": 2}], "things") == {
            "RC_THINGS_0_A": "1",
            "RC_THINGS_1_A": "2",
        }


class TestTable:
    def test_maps_and_lists_are_indented_under_their_name(self):
        rendered = _render_table(OUTPUTS)
        assert "cluster_name = bmgr-cluster" in rendered
        assert "security_groups:" in rendered
        assert "    runners = sg-0d4e5f" in rendered
        assert "    runners-private = subnet-0aa, subnet-0bb" in rendered


class _FakeRunner:
    """Stand-in for TerraformRunner returning terraform's {value,type} envelope."""

    def __init__(self, *args, **kwargs):
        pass

    def output(self, name=None):
        return {k: {"value": v, "type": "string"} for k, v in OUTPUTS.items()}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: nginx:alpine\n"
    )
    (tmp_path / "rc.yml").write_text(
        "version: 2\n"
        "project: bmgr\n"
        "compose_file: docker-compose.yml\n"
        "provider: ecs\n"
        "provider_config:\n"
        "  ecs:\n"
        "    region: us-west-2\n"
    )
    (tmp_path / "terraform").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("remote_compose.terraform.runner.TerraformRunner", _FakeRunner)
    return tmp_path


def _run(args=()):
    return CliRunner().invoke(outputs_cmd, list(args), obj={"config_path": None})


class TestCommand:
    def test_default_renders_a_table(self, project):
        result = _run()
        assert result.exit_code == 0
        assert "security_groups:" in result.output
        assert "runners = sg-0d4e5f" in result.output

    def test_json_unwraps_the_terraform_envelope(self, project):
        result = _run(["--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["security_groups"]["runners"] == "sg-0d4e5f"
        assert "value" not in str(parsed["vpc_id"])

    def test_env_emits_assignable_lines(self, project):
        result = _run(["--env"])
        assert result.exit_code == 0
        lines = [ln for ln in result.output.splitlines() if ln]
        assert "RC_VPC_ID=vpc-0a1b2c" in lines
        assert "RC_SUBNETS_RUNNERS_PRIVATE=subnet-0aa,subnet-0bb" in lines
        assert all("=" in ln for ln in lines)

    def test_env_prefix_override(self, project):
        result = _run(["--env", "--prefix", "RUNNER_"])
        assert "RUNNER_VPC_ID=vpc-0a1b2c" in result.output

    def test_single_scalar_output_is_bare(self, project):
        result = _run(["vpc_id"])
        assert result.exit_code == 0
        assert result.output.strip() == "vpc-0a1b2c"

    def test_single_map_output_is_json(self, project):
        result = _run(["security_groups"])
        assert json.loads(result.output)["runners"] == "sg-0d4e5f"

    def test_unknown_output_name_lists_the_available_ones(self, project):
        result = _run(["nope"])
        assert result.exit_code == 1
        assert "no output named 'nope'" in result.output
        assert "security_groups" in result.output

    def test_json_and_env_together_are_rejected(self, project):
        result = _run(["--json", "--env"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_missing_terraform_dir_is_a_clear_error(self, project):
        (project / "terraform").rmdir()
        result = _run()
        assert result.exit_code == 1
        assert "run `rc deploy` first" in result.output

    def test_missing_rc_yml_is_a_clear_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _run()
        assert result.exit_code == 1
        assert "no rc.yml" in result.output
