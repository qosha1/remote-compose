"""rc.yml `bootstrap:` config surface (rc-kiz.1).

The CI/bootstrap IAM — the GitHub OIDC role CI assumes to trigger deploys — is not a
per-service runtime resource and had no rc representation, so it drifted out-of-band
(the 2026-06-16 foundry-tenant roll hand-edit). This adds a top-level, OPT-IN
``bootstrap:`` section so rc can own that role.

GENERAL + opt-in + strictly ADDITIVE: with no ``bootstrap`` key the parsed config is
unchanged (guarded by test_golden.py / the rest of the fast tier). ${project}/${cluster}
placeholders are stored literally at parse time — interpolation happens downstream
(rc-kiz.2), matching the existing convention where TerraformConfig.output_dir holds
``${provider}``.
"""

from __future__ import annotations

import pytest

from remote_compose.config.v2_schema import (
    BootstrapConfig,
    GithubOidcDeployRole,
    ConfigError,
    parse,
)

_BASE = {
    "version": 2,
    "project": "start-simpli",
    "compose_file": "docker-compose.yml",
    "provider": "ecs",
}


def _doc(**bootstrap):
    d = dict(_BASE)
    d["bootstrap"] = bootstrap
    return d


_FR_BOOTSTRAP = {
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
}


class TestBootstrapParsing:
    def test_full_fr_block_parses_into_typed_objects(self):
        cfg = parse(_doc(**_FR_BOOTSTRAP))
        assert isinstance(cfg.bootstrap, BootstrapConfig)
        role = cfg.bootstrap.github_oidc_deploy_role
        assert isinstance(role, GithubOidcDeployRole)
        assert role.github_repo == "qosha1/start-simpli-api"
        assert role.github_branch == "main"
        # Placeholders stored verbatim — interpolation is downstream.
        assert role.permissions["codebuild_project"] == "${project}-build"
        assert role.permissions["ecs_clusters"] == ["${cluster}", "foundry-tenant-*"]

    def test_defaults(self):
        cfg = parse(_doc(**_FR_BOOTSTRAP))
        role = cfg.bootstrap.github_oidc_deploy_role
        assert role.role_name is None  # default derived later
        assert role.create_oidc_provider is False  # adopt existing by default
        # committed stack lives outside the gitignored deploy/<project>/terraform tree
        assert cfg.bootstrap.output_dir == "bootstrap/terraform"

    def test_branch_defaults_to_main(self):
        role = {"github_repo": "o/r", "permissions": {}}
        cfg = parse(_doc(github_oidc_deploy_role=role))
        assert cfg.bootstrap.github_oidc_deploy_role.github_branch == "main"

    def test_no_bootstrap_key_leaves_field_none(self):
        cfg = parse(dict(_BASE))
        assert cfg.bootstrap is None


class TestBootstrapValidation:
    def test_github_repo_required(self):
        with pytest.raises(ConfigError, match="github_repo"):
            parse(_doc(github_oidc_deploy_role={"permissions": {}}))

    def test_github_repo_must_be_owner_slash_repo(self):
        with pytest.raises(ConfigError, match="owner/repo"):
            parse(_doc(github_oidc_deploy_role={"github_repo": "justrepo"}))

    def test_unknown_permission_key_rejected(self):
        with pytest.raises(ConfigError, match="permission"):
            parse(
                _doc(
                    github_oidc_deploy_role={
                        "github_repo": "o/r",
                        "permissions": {"bogus": "x"},
                    }
                )
            )

    def test_ecs_clusters_must_be_list(self):
        with pytest.raises(ConfigError, match="ecs_clusters"):
            parse(
                _doc(
                    github_oidc_deploy_role={
                        "github_repo": "o/r",
                        "permissions": {"ecs_clusters": "not-a-list"},
                    }
                )
            )

    def test_pass_roles_must_be_list(self):
        with pytest.raises(ConfigError, match="pass_roles"):
            parse(
                _doc(
                    github_oidc_deploy_role={
                        "github_repo": "o/r",
                        "permissions": {"pass_roles": "x"},
                    }
                )
            )

    def test_output_dir_must_be_non_empty(self):
        with pytest.raises(ConfigError, match="output_dir"):
            parse(
                {
                    **_BASE,
                    "bootstrap": {
                        "output_dir": "",
                        "github_oidc_deploy_role": {
                            "github_repo": "o/r",
                            "permissions": {},
                        },
                    },
                }
            )
