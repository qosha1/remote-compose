"""`rc bootstrap` CLI command (rc-kiz.4).

Emits the committed deploy-role stack to bootstrap.output_dir, runs
terraform init + plan, and (only with --apply) applies. Never destroys.

Terraform is stubbed with a RecordingTerraformRunner so the test asserts the
init/plan/apply sequence without running terraform or touching AWS.
"""

from __future__ import annotations

import yaml
import pytest
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.cli_commands import bootstrap as bootstrap_mod
from remote_compose.terraform.runner import RecordingTerraformRunner

_RC = {
    "version": 2,
    "project": "start-simpli",
    "compose_file": "docker-compose.yml",
    "provider": "ecs",
    "provider_config": {
        "ecs": {"region": "us-west-1", "cluster": "start-simpli-cluster"}
    },
    "terraform": {
        "backend": {
            "type": "s3",
            "bucket": "acct-rc-tfstate",
            "key": "start-simpli/ecs.tfstate",
            "region": "us-west-1",
            "dynamodb_table": "rc-tfstate-locks",
        }
    },
    "bootstrap": {
        "github_oidc_deploy_role": {
            "github_repo": "qosha1/start-simpli-api",
            "github_branch": "main",
            "permissions": {
                "codebuild_project": "${project}-build",
                "ecr_namespace": "${project}/*",
                "ecs_clusters": ["${cluster}", "foundry-tenant-*"],
                "pass_roles": ["${project}-task", "${project}-task-exec"],
            },
        }
    },
}


@pytest.fixture
def rc_yml(tmp_path):
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(_RC))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    return p


@pytest.fixture
def recorded(monkeypatch):
    """Swap the command's terraform runner for a recording stub."""
    holder = {}

    def _fake(out_dir, progress=None):
        r = RecordingTerraformRunner(out_dir)
        # default plan: a clean create, no destroys
        r.script("plan", "Plan: 1 to add, 0 to change, 0 to destroy.")
        holder["runner"] = r
        return r

    monkeypatch.setattr(bootstrap_mod, "_make_runner", _fake)
    return holder


def _invoke(rc_yml, *args):
    return CliRunner().invoke(cli, ["-c", str(rc_yml), "bootstrap", *args])


class TestEmitAndPlan:
    def test_emits_stack_to_output_dir(self, rc_yml, recorded):
        res = _invoke(rc_yml)
        assert res.exit_code == 0, res.output
        stack = rc_yml.parent / "bootstrap" / "terraform"
        assert (stack / "deploy_role.tf").exists()
        assert (stack / "backend.tf").exists()

    def test_runs_init_then_plan_no_apply_by_default(self, rc_yml, recorded):
        res = _invoke(rc_yml)
        assert res.exit_code == 0, res.output
        cmds = [c.args[0] for c in recorded["runner"].calls]
        assert "init" in cmds
        assert "plan" in cmds
        assert "apply" not in cmds  # plan-only without --apply
        assert "destroy" not in cmds


class TestApplyGate:
    def test_apply_with_yes_invokes_apply(self, rc_yml, recorded):
        res = _invoke(rc_yml, "--apply", "--yes")
        assert res.exit_code == 0, res.output
        cmds = [c.args[0] for c in recorded["runner"].calls]
        assert "apply" in cmds
        assert "destroy" not in cmds

    def test_apply_refuses_when_plan_would_destroy(self, rc_yml, monkeypatch):
        holder = {}

        def _fake(out_dir, progress=None):
            r = RecordingTerraformRunner(out_dir)
            r.script("plan", "Plan: 0 to add, 0 to change, 2 to destroy.")
            holder["runner"] = r
            return r

        monkeypatch.setattr(bootstrap_mod, "_make_runner", _fake)
        res = _invoke(rc_yml, "--apply", "--yes")
        assert res.exit_code != 0
        assert "destroy" in res.output.lower()
        cmds = [c.args[0] for c in holder["runner"].calls]
        assert "apply" not in cmds  # no-clobber: never applied a destructive plan


class TestRefusals:
    def test_no_bootstrap_section_errors_clearly(self, tmp_path, recorded):
        doc = {k: v for k, v in _RC.items() if k != "bootstrap"}
        p = tmp_path / "rc.yml"
        p.write_text(yaml.safe_dump(doc))
        res = _invoke(p)
        assert res.exit_code != 0
        assert "bootstrap" in res.output.lower()
