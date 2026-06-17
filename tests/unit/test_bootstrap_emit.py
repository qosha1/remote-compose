"""Emit the committed bootstrap deploy-role stack (rc-kiz.3).

Renders a self-contained terraform stack from a parsed GithubOidcDeployRole +
the workload backend. Deterministic (no AWS calls). The committed-ness comes
from LOCATION (outside the gitignored deploy/ tree) — state is still never
committed, so the stack's .gitignore still excludes *.tfstate.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.bootstrap import emit_bootstrap_stack
from remote_compose.config.v2_schema import GithubOidcDeployRole

_S3_BACKEND = {
    "type": "s3",
    "bucket": "acct-rc-tfstate",
    "key": "start-simpli/ecs.tfstate",
    "region": "us-west-1",
    "dynamodb_table": "rc-tfstate-locks",
}


def _fr_role(**over) -> GithubOidcDeployRole:
    base = dict(
        github_repo="qosha1/start-simpli-api",
        github_branch="main",
        permissions={
            "codebuild_project": "${project}-build",
            "ecr_namespace": "${project}/*",
            "ecs_clusters": ["${cluster}", "foundry-tenant-*"],
            "pass_roles": ["${project}-task", "${project}-task-exec"],
        },
    )
    base.update(over)
    return GithubOidcDeployRole(**base)


def _emit(tmp_path: Path, role=None, backend=None):
    out = tmp_path / "bootstrap" / "terraform"
    emit_bootstrap_stack(
        role or _fr_role(),
        project="start-simpli",
        cluster="start-simpli-cluster",
        workload_backend=backend if backend is not None else _S3_BACKEND,
        out_dir=out,
    )
    return out


class TestEmittedFileSet:
    def test_expected_files_present(self, tmp_path):
        out = _emit(tmp_path)
        names = {p.name for p in out.iterdir()}
        assert {
            "versions.tf",
            "provider.tf",
            "backend.tf",
            "oidc.tf",
            "deploy_role.tf",
            "outputs.tf",
            "README.md",
            ".gitignore",
        } <= names


class TestDeployRole:
    def test_default_role_name_interpolated(self, tmp_path):
        role = (_emit(tmp_path) / "deploy_role.tf").read_text()
        assert 'resource "aws_iam_role" "deploy"' in role
        assert '"start-simpli-github-deploy"' in role

    def test_role_name_override(self, tmp_path):
        out = _emit(tmp_path, role=_fr_role(role_name="EcsRollFoundryTenants"))
        role = (out / "deploy_role.tf").read_text()
        assert '"EcsRollFoundryTenants"' in role

    def test_trust_policy_sub_for_repo_and_branch(self, tmp_path):
        role = (_emit(tmp_path) / "deploy_role.tf").read_text()
        assert "sts:AssumeRoleWithWebIdentity" in role
        assert "repo:qosha1/start-simpli-api:ref:refs/heads/main" in role
        assert "sts.amazonaws.com" in role

    def test_permission_statements_interpolated(self, tmp_path):
        role = (_emit(tmp_path) / "deploy_role.tf").read_text()
        # codebuild + ecr + ecs (exact + wildcard) + passrole, all interpolated
        assert "project/start-simpli-build" in role
        assert "repository/start-simpli/*" in role
        assert "service/start-simpli-cluster/*" in role
        assert "service/foundry-tenant-*/*" in role
        assert "role/start-simpli-task-exec" in role
        assert "iam:PassRole" in role
        # region/account remain terraform refs (resolved at apply)
        assert "${data.aws_caller_identity.current.account_id}" in role
        assert "${data.aws_region.current.name}" in role
        # data sources declared
        assert 'data "aws_caller_identity" "current"' in role
        assert 'data "aws_region" "current"' in role


class TestOidcAdoptOrCreate:
    def test_default_adopts_via_data_source(self, tmp_path):
        oidc = (_emit(tmp_path) / "oidc.tf").read_text()
        assert 'data "aws_iam_openid_connect_provider" "github"' in oidc
        assert 'resource "aws_iam_openid_connect_provider"' not in oidc

    def test_create_opt_in_emits_resource(self, tmp_path):
        out = _emit(tmp_path, role=_fr_role(create_oidc_provider=True))
        oidc = (out / "oidc.tf").read_text()
        assert 'resource "aws_iam_openid_connect_provider" "github"' in oidc
        assert "sts.amazonaws.com" in oidc


class TestSeparateState:
    def test_backend_uses_distinct_bootstrap_key(self, tmp_path):
        backend = (_emit(tmp_path) / "backend.tf").read_text()
        assert 'backend "s3"' in backend
        assert "start-simpli/bootstrap.tfstate" in backend
        assert "acct-rc-tfstate" in backend


class TestCommittedButStateIgnored:
    def test_gitignore_excludes_state_not_tf(self, tmp_path):
        gi = (_emit(tmp_path) / ".gitignore").read_text()
        assert "*.tfstate" in gi
        assert ".terraform/" in gi
        # the .tf files ARE committed — never blanket-ignore them
        assert "*.tf\n" not in gi
        assert not gi.startswith("*.tf\n")

    def test_readme_documents_import_no_op(self, tmp_path):
        readme = (_emit(tmp_path) / "README.md").read_text()
        assert "import" in readme.lower()
        assert "rc bootstrap" in readme


class TestIdempotentEmit:
    def test_reemit_is_byte_identical(self, tmp_path):
        out = _emit(tmp_path)
        first = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
        emit_bootstrap_stack(
            _fr_role(),
            project="start-simpli",
            cluster="start-simpli-cluster",
            workload_backend=_S3_BACKEND,
            out_dir=out,
        )
        second = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
        assert first == second
