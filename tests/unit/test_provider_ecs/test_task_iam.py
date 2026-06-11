"""Task-role app-IAM grants (rc-8y7).

rc emits a single shared task role (``aws_iam_role.task``) for every service in
a stack. It currently has no way to grant that role *application* IAM
(S3/SQS/SES/...), so apps needing e.g. S3 media access had to bolt grants on
out-of-band — the browser-mgr ``reconcile_task_env.py`` ``GrantAccessS3Media``
inline policy, whose absence on the migrated role caused every session
recording to 403 on upload.

``provider_config.ecs.iam`` lets rc.yml declare, on the shared task role:
  - ``managed_policies: [arn, ...]``  -> ``aws_iam_role_policy_attachment``
  - ``statements: [{sid?, actions, resources, condition?}]`` -> a single
    ``aws_iam_role_policy`` (inline) named ``<project>-task-app``.

GENERAL + opt-in + strictly ADDITIVE: with no ``iam`` block the emitted
terraform is byte-identical (guarded by test_golden.py). This file proves the
opt-in render exists (RED until the iam.tf.j2 template + provider context land).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider

# Mirrors the live browser-mgr GrantAccessS3Media grant the migration had to
# apply out-of-band — the canonical motivating case for this feature.
_MANAGED_ARN = "arn:aws:iam::aws:policy/AmazonSESFullAccess"
_BUCKET = "browser-mgr-media-033937118837"
_S3_STATEMENT = {
    "sid": "S3Media",
    "actions": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
    ],
    "resources": [f"arn:aws:s3:::{_BUCKET}", f"arn:aws:s3:::{_BUCKET}/*"],
}
IAM_CFG = {"managed_policies": [_MANAGED_ARN], "statements": [_S3_STATEMENT]}


def _ctx(tmp_path: Path, iam: dict | None = None) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "browser-mgr-prod",
        "vpc_id": "vpc-0b6967",
        "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
        "security_group_ids": ["sg-013b"],
    }
    if iam is not None:
        ecs_cfg["iam"] = iam
    return DeployContext(
        project="browser-mgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "worker": ServiceSpec(name="worker", cpu=512, memory=1024, type="worker"),
        },
        secrets=[],
    )


def _emit(tmp_path: Path, iam: dict | None) -> str:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, iam), out)
    return (out / "iam.tf").read_text()


class TestTaskIamEmission:
    def test_managed_policy_attached_to_task_role(self, tmp_path):
        iam = _emit(tmp_path, IAM_CFG)
        assert 'resource "aws_iam_role_policy_attachment"' in iam
        assert _MANAGED_ARN in iam
        # Attached to the shared TASK role, not the execution role.
        assert "role       = aws_iam_role.task.name" in iam

    def test_inline_statement_rendered_on_task_role(self, tmp_path):
        iam = _emit(tmp_path, IAM_CFG)
        assert 'resource "aws_iam_role_policy" "task_app"' in iam
        assert "role = aws_iam_role.task.id" in iam
        # The S3 actions + bucket resources from the declared statement.
        assert '"s3:PutObject"' in iam
        assert f"arn:aws:s3:::{_BUCKET}/*" in iam
        assert '"S3Media"' in iam  # sid preserved

    def test_condition_serialized_in_statement(self, tmp_path):
        cfg = {
            "statements": [
                {
                    "actions": ["elasticfilesystem:ClientMount"],
                    "resources": [
                        "arn:aws:elasticfilesystem:us-east-2:1:file-system/fs-1"
                    ],
                    "condition": {
                        "StringEquals": {
                            "elasticfilesystem:AccessPointArn": "arn:aws:x:access-point/fsap-1"
                        }
                    },
                }
            ]
        }
        iam = _emit(tmp_path, cfg)
        assert '"Condition"' in iam
        assert "elasticfilesystem:AccessPointArn" in iam


class TestTaskIamValidation:
    def test_statement_requires_actions_and_resources(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="actions.*resources"):
            _emit(tmp_path, {"statements": [{"actions": ["s3:GetObject"]}]})


class TestTaskIamDefaultPath:
    def test_no_iam_block_emits_no_app_grants(self, tmp_path):
        iam = _emit(tmp_path, None)
        assert 'resource "aws_iam_role_policy" "task_app"' not in iam
        assert _MANAGED_ARN not in iam
        assert '"s3:PutObject"' not in iam


class TestTaskRoleTags:
    """rc-h72: provider_config.ecs.iam.role_tags -> tags on aws_iam_role.task
    (adopted resource policies, e.g. Copilot EFS, gate on principal tags)."""

    def test_role_tags_rendered_on_task_role(self, tmp_path):
        iam = _emit(
            tmp_path,
            {
                "role_tags": {
                    "copilot-application": "browser-mgr",
                    "copilot-environment": "production",
                }
            },
        )
        # tags land on the TASK role resource block
        role = iam[iam.index('resource "aws_iam_role" "task" {') :]
        assert "tags = {" in role
        assert '"copilot-application" = "browser-mgr"' in role
        assert '"copilot-environment" = "production"' in role

    def test_no_role_tags_emits_no_tags_block(self, tmp_path):
        iam = _emit(tmp_path, None)
        role = iam[iam.index('resource "aws_iam_role" "task" {') :]
        assert "tags = {" not in role
