"""Pure build helpers for the bootstrap stack (rc-kiz.2).

Interpolation + least-privilege IAM derivation are pure functions: no AWS calls,
deterministic output. Region/account land as terraform data-source refs so the
emitted policy resolves at apply time without rc ever calling AWS.
"""

from __future__ import annotations

import pytest

from remote_compose.bootstrap import build
from remote_compose.config.v2_schema import ConfigError

_REGION = "${data.aws_region.current.name}"
_ACCOUNT = "${data.aws_caller_identity.current.account_id}"


class TestInterpolate:
    def test_substitutes_project_and_cluster(self):
        out = build.interpolate(
            {
                "codebuild_project": "${project}-build",
                "ecs_clusters": ["${cluster}", "foundry-tenant-*"],
            },
            project="start-simpli",
            cluster="start-simpli-cluster",
        )
        assert out["codebuild_project"] == "start-simpli-build"
        assert out["ecs_clusters"] == ["start-simpli-cluster", "foundry-tenant-*"]

    def test_unknown_placeholder_rejected(self):
        with pytest.raises(ConfigError, match="placeholder"):
            build.interpolate("${region}-x", project="p", cluster="c")


class TestDeriveStatements:
    def _by_sid(self, stmts):
        return {s["Sid"]: s for s in stmts}

    def test_codebuild_statement(self):
        s = self._by_sid(
            build.derive_statements({"codebuild_project": "start-simpli-build"})
        )
        assert "CodeBuildDeploy" in s
        assert "codebuild:StartBuild" in s["CodeBuildDeploy"]["Action"]
        assert (
            s["CodeBuildDeploy"]["Resource"]
            == f"arn:aws:codebuild:{_REGION}:{_ACCOUNT}:project/start-simpli-build"
        )

    def test_ecr_namespace_auth_plus_pushpull(self):
        s = self._by_sid(build.derive_statements({"ecr_namespace": "start-simpli/*"}))
        # auth token MUST be on Resource "*"
        assert s["EcrAuth"]["Action"] == ["ecr:GetAuthorizationToken"]
        assert s["EcrAuth"]["Resource"] == "*"
        assert "ecr:PutImage" in s["EcrPushPull"]["Action"]
        assert (
            s["EcrPushPull"]["Resource"]
            == f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/start-simpli/*"
        )

    def test_ecs_clusters_exact_and_wildcard_carry_into_arns(self):
        s = self._by_sid(
            build.derive_statements(
                {"ecs_clusters": ["start-simpli-cluster", "foundry-tenant-*"]}
            )
        )
        res = s["EcsDeployServices"]["Resource"]
        # exact cluster -> exact service + cluster ARN
        assert f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:service/start-simpli-cluster/*" in res
        assert f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/start-simpli-cluster" in res
        # wildcard entry -> wildcard service ARN (StringLike-by-ARN)
        assert f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:service/foundry-tenant-*/*" in res
        assert "ecs:UpdateService" in s["EcsDeployServices"]["Action"]
        # task-def actions can't be resource-scoped -> Resource "*"
        assert s["EcsTaskDefinitions"]["Resource"] == "*"
        assert "ecs:RegisterTaskDefinition" in s["EcsTaskDefinitions"]["Action"]

    def test_pass_roles_with_passedtoservice_condition(self):
        s = self._by_sid(
            build.derive_statements(
                {"pass_roles": ["start-simpli-task", "start-simpli-task-exec"]}
            )
        )
        p = s["PassTaskRoles"]
        assert p["Action"] == ["iam:PassRole"]
        assert f"arn:aws:iam::{_ACCOUNT}:role/start-simpli-task" in p["Resource"]
        assert f"arn:aws:iam::{_ACCOUNT}:role/start-simpli-task-exec" in p["Resource"]
        assert (
            p["Condition"]["StringEquals"]["iam:PassedToService"]
            == "ecs-tasks.amazonaws.com"
        )

    def test_empty_permissions_yields_no_statements(self):
        assert build.derive_statements({}) == []

    def test_deterministic_order(self):
        perms = {
            "pass_roles": ["r"],
            "codebuild_project": "b",
            "ecs_clusters": ["c"],
            "ecr_namespace": "n/*",
        }
        sids = [s["Sid"] for s in build.derive_statements(perms)]
        # stable order regardless of dict insertion order
        assert sids == [
            "CodeBuildDeploy",
            "EcrAuth",
            "EcrPushPull",
            "EcsDeployServices",
            "EcsTaskDefinitions",
            "PassTaskRoles",
        ]


class TestTrustPolicy:
    def test_exact_branch_uses_stringequals_and_keeps_aud(self):
        tp = build.build_trust_policy(
            "qosha1/start-simpli-api", "main", "${data.x.arn}"
        )
        cond = tp["Statement"][0]["Condition"]
        # aud must survive alongside the sub match
        assert cond["StringEquals"]["token.actions.githubusercontent.com:aud"] == (
            "sts.amazonaws.com"
        )
        assert cond["StringEquals"]["token.actions.githubusercontent.com:sub"] == (
            "repo:qosha1/start-simpli-api:ref:refs/heads/main"
        )
        assert tp["Statement"][0]["Principal"]["Federated"] == "${data.x.arn}"
        assert tp["Statement"][0]["Action"] == "sts:AssumeRoleWithWebIdentity"

    def test_wildcard_branch_uses_stringlike(self):
        tp = build.build_trust_policy("o/r", "*", "${arn}")
        cond = tp["Statement"][0]["Condition"]
        assert cond["StringLike"]["token.actions.githubusercontent.com:sub"] == (
            "repo:o/r:*"
        )
        # aud stays exact
        assert "StringEquals" in cond


class TestBackendDerivation:
    def test_s3_reuses_bucket_swaps_key(self):
        wb = {
            "type": "s3",
            "bucket": "acct-rc-tfstate",
            "key": "start-simpli/ecs.tfstate",
            "region": "us-west-1",
            "dynamodb_table": "rc-tfstate-locks",
        }
        b = build.derive_bootstrap_backend(wb, "start-simpli")
        assert b["type"] == "s3"
        assert b["bucket"] == "acct-rc-tfstate"
        assert b["dynamodb_table"] == "rc-tfstate-locks"
        # separate state from the workload stack
        assert b["key"] == "start-simpli/bootstrap.tfstate"

    def test_local_backend_stays_local(self):
        assert build.derive_bootstrap_backend({"type": "local"}, "p")["type"] == "local"
        assert build.derive_bootstrap_backend(None, "p")["type"] == "local"
