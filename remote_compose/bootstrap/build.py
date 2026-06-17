"""Pure build helpers for the bootstrap deploy-role stack (rc-kiz.2).

No AWS calls, no I/O — deterministic transforms only, so the result is
golden-testable and `rc bootstrap` never touches AWS at emit time. Region and
account land as terraform data-source refs that resolve at `terraform apply`.
"""

from __future__ import annotations

from typing import Any

from ..config._schema_types import ConfigError

# Terraform interpolation refs (resolved at apply, not by rc). These survive
# Jinja2 rendering untouched (Jinja uses {{ }}, not ${ }).
REGION_REF = "${data.aws_region.current.name}"
ACCOUNT_REF = "${data.aws_caller_identity.current.account_id}"

# OIDC provider ARN references — adopt an existing provider by default (it is
# account-global and CI already assumes it), create one only when opted in.
OIDC_PROVIDER_ARN_ADOPT = "${data.aws_iam_openid_connect_provider.github.arn}"
OIDC_PROVIDER_ARN_CREATE = "${aws_iam_openid_connect_provider.github.arn}"

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
_AUD_KEY = "token.actions.githubusercontent.com:aud"
_SUB_KEY = "token.actions.githubusercontent.com:sub"


def interpolate(value: Any, *, project: str, cluster: str) -> Any:
    """Recursively replace ``${project}`` / ``${cluster}`` in str/list/dict.

    Any other ``${...}`` left over is a typo (or an unsupported placeholder) and
    raises, so a mistake fails loudly at emit rather than leaking a literal
    ``${foo}`` into generated HCL.
    """
    if isinstance(value, str):
        out = value.replace("${project}", project).replace("${cluster}", cluster)
        if "${" in out:
            raise ConfigError(
                f"unknown placeholder in bootstrap config: {value!r} "
                f"(only ${{project}} and ${{cluster}} are supported)"
            )
        return out
    if isinstance(value, list):
        return [interpolate(v, project=project, cluster=cluster) for v in value]
    if isinstance(value, dict):
        return {
            k: interpolate(v, project=project, cluster=cluster)
            for k, v in value.items()
        }
    return value


def derive_statements(permissions: dict[str, Any]) -> list[dict]:
    """Map the rc.yml ``permissions`` block to least-privilege IAM statements.

    Stable SID order regardless of dict insertion order, so the rendered policy
    is deterministic (golden-testable). Each permission key is independent;
    omit a key and its statement(s) are simply absent.
    """
    stmts: list[dict] = []

    codebuild = permissions.get("codebuild_project")
    if codebuild:
        stmts.append(
            {
                "Sid": "CodeBuildDeploy",
                "Effect": "Allow",
                "Action": [
                    "codebuild:StartBuild",
                    "codebuild:StartBuildBatch",
                    "codebuild:BatchGetBuilds",
                    "codebuild:BatchGetBuildBatches",
                ],
                "Resource": f"arn:aws:codebuild:{REGION_REF}:{ACCOUNT_REF}:project/{codebuild}",
            }
        )

    ecr = permissions.get("ecr_namespace")
    if ecr:
        # GetAuthorizationToken does NOT support resource-level perms.
        stmts.append(
            {
                "Sid": "EcrAuth",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            }
        )
        stmts.append(
            {
                "Sid": "EcrPushPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:PutImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:DescribeRepositories",
                    "ecr:CreateRepository",
                ],
                "Resource": f"arn:aws:ecr:{REGION_REF}:{ACCOUNT_REF}:repository/{ecr}",
            }
        )

    clusters = permissions.get("ecs_clusters") or []
    if clusters:
        resources: list[str] = []
        for c in clusters:
            # Wildcards in the entry (e.g. foundry-tenant-*) carry straight into
            # the ARN -> IAM matches them StringLike-style. Exact names match
            # exactly. Both the service-scoped and cluster ARNs are needed.
            resources.append(f"arn:aws:ecs:{REGION_REF}:{ACCOUNT_REF}:service/{c}/*")
            resources.append(f"arn:aws:ecs:{REGION_REF}:{ACCOUNT_REF}:cluster/{c}")
        stmts.append(
            {
                "Sid": "EcsDeployServices",
                "Effect": "Allow",
                "Action": [
                    "ecs:UpdateService",
                    "ecs:DescribeServices",
                    "ecs:ListServices",
                    "ecs:DescribeTasks",
                    "ecs:ListTasks",
                    "ecs:DescribeClusters",
                ],
                "Resource": resources,
            }
        )
        # RegisterTaskDefinition et al. can't be resource-scoped.
        stmts.append(
            {
                "Sid": "EcsTaskDefinitions",
                "Effect": "Allow",
                "Action": [
                    "ecs:RegisterTaskDefinition",
                    "ecs:DeregisterTaskDefinition",
                    "ecs:DescribeTaskDefinition",
                    "ecs:ListTaskDefinitions",
                ],
                "Resource": "*",
            }
        )

    pass_roles = permissions.get("pass_roles") or []
    if pass_roles:
        stmts.append(
            {
                "Sid": "PassTaskRoles",
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": [
                    f"arn:aws:iam::{ACCOUNT_REF}:role/{r}" for r in pass_roles
                ],
                "Condition": {
                    "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            }
        )

    return stmts


def build_trust_policy(
    github_repo: str, github_branch: str, provider_arn_ref: str
) -> dict:
    """Assume-role-with-web-identity trust policy for the GitHub OIDC provider.

    The ``aud`` claim is always an exact match. The ``sub`` claim is an exact
    branch match (StringEquals) unless ``github_branch == "*"``, which matches
    any ref in the repo (StringLike).
    """
    condition: dict[str, dict[str, str]] = {
        "StringEquals": {_AUD_KEY: "sts.amazonaws.com"}
    }
    if github_branch == "*":
        condition["StringLike"] = {_SUB_KEY: f"repo:{github_repo}:*"}
    else:
        condition["StringEquals"][
            _SUB_KEY
        ] = f"repo:{github_repo}:ref:refs/heads/{github_branch}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": provider_arn_ref},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": condition,
            }
        ],
    }


def derive_bootstrap_backend(
    workload_backend: dict[str, Any] | None, project: str
) -> dict:
    """Derive the bootstrap stack's backend from the workload backend.

    Same bucket/lock table, but a distinct state key so the committed bootstrap
    stack has its OWN state, never colliding with the workload stack.
    """
    wb = dict(workload_backend or {"type": "local"})
    if wb.get("type") == "s3":
        out: dict[str, Any] = {"type": "s3", "key": f"{project}/bootstrap.tfstate"}
        for k in ("bucket", "region", "dynamodb_table", "profile", "encrypt"):
            if wb.get(k) is not None:
                out[k] = wb[k]
        return out
    return {"type": "local"}
