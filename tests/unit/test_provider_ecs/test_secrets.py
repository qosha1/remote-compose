"""Unit tests for ECS secrets integration (Phase 6b.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, SecretRef, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, secrets: list[SecretRef]) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-2", "cluster": "test", "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=secrets,
    )


class TestNoSecrets:
    def test_secrets_tf_empty(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, []), out)
        assert (out / "secrets.tf").read_text().strip() == ""

    def test_task_def_no_secrets_block(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, []), out)
        services = (out / "services.tf").read_text()
        assert "secrets = [" not in services

    def test_iam_no_secrets_policy(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, []), out)
        iam = (out / "iam.tf").read_text()
        assert "task_execution_secrets" not in iam


class TestAwsSmSecret:
    def test_references_existing_arn(self, tmp_path):
        arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:myapp/db-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="db_password", source="aws_sm", arn=arn)]),
            out,
        )
        services = (out / "services.tf").read_text()
        assert arn in services
        assert "DB_PASSWORD" in services  # env name derived from secret name

    def test_no_terraform_secret_created_for_aws_sm(self, tmp_path):
        arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:myapp/db-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="db", source="aws_sm", arn=arn)]), out,
        )
        # secrets.tf only creates placeholders for source=file secrets
        assert (out / "secrets.tf").read_text().strip() == ""

    def test_iam_policy_lists_aws_sm_arn(self, tmp_path):
        arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:myapp/db-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="db", source="aws_sm", arn=arn)]), out,
        )
        iam = (out / "iam.tf").read_text()
        assert "task_execution_secrets" in iam
        assert arn in iam

    def test_missing_arn_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="arn"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, [SecretRef(name="db", source="aws_sm")]),
                tmp_path / "tf",
            )


class TestFileSecret:
    def test_terraform_creates_placeholder_secret(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="django", source="file",
                                      path=".envs/.production/.django")]),
            out,
        )
        secrets_tf = (out / "secrets.tf").read_text()
        assert 'aws_secretsmanager_secret" "django"' in secrets_tf
        assert 'aws_secretsmanager_secret_version" "django_placeholder"' in secrets_tf

    def test_placeholder_uses_ignore_changes_lifecycle(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="django", source="file",
                                      path=".envs/.production/.django")]),
            out,
        )
        secrets_tf = (out / "secrets.tf").read_text()
        assert "ignore_changes = [secret_string]" in secrets_tf

    def test_task_def_references_terraform_arn(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="django", source="file", path="/x/.django")]),
            out,
        )
        services = (out / "services.tf").read_text()
        assert "aws_secretsmanager_secret.django.arn" in services
        assert "DJANGO" in services  # env name

    def test_iam_policy_references_terraform_arn(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="django", source="file", path="/x/.django")]),
            out,
        )
        iam = (out / "iam.tf").read_text()
        assert "task_execution_secrets" in iam
        assert "aws_secretsmanager_secret.django.arn" in iam


class TestMixedSecrets:
    def test_file_and_aws_sm_coexist(self, tmp_path):
        arn = "arn:aws:secretsmanager:us-west-2:1:secret:db-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [
                SecretRef(name="django", source="file", path="/x/.django"),
                SecretRef(name="db",     source="aws_sm", arn=arn),
            ]),
            out,
        )
        services = (out / "services.tf").read_text()
        iam = (out / "iam.tf").read_text()
        secrets_tf = (out / "secrets.tf").read_text()
        assert "aws_secretsmanager_secret.django.arn" in services
        assert arn in services
        assert "aws_secretsmanager_secret.django.arn" in iam
        assert arn in iam
        # file secret has terraform resource; aws_sm does not
        assert 'aws_secretsmanager_secret" "django"' in secrets_tf
        assert 'aws_secretsmanager_secret" "db"' not in secrets_tf


class TestValueNeverLeaks:
    def test_sentinel_never_appears_in_any_hcl(self, tmp_path):
        """Even in exotic fields (path, arn) the VALUE must not land in HCL."""
        sentinel = "SECRET_SENTINEL_f00ba2c0de"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [
                SecretRef(name="django", source="file", path=f"/tmp/{sentinel}"),
            ]),
            out,
        )
        for tf in out.glob("*.tf"):
            assert sentinel not in tf.read_text(), f"sentinel leaked into {tf.name}"


class TestUnknownSource:
    def test_unsupported_source_raises(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="source"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, [SecretRef(name="x", source="vault")]),
                tmp_path / "tf",
            )

    def test_k8s_source_silently_skipped_on_ecs(self, tmp_path):
        """k8s_secret makes sense for a k8s provider — ECS should skip, not fail."""
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="x", source="k8s_secret", ref="x-secret")]),
            out,
        )
        # No secrets in task def, no iam policy, no secrets.tf resources
        services = (out / "services.tf").read_text()
        assert "secrets = [" not in services


class TestEnvNameDerivation:
    def test_dashes_become_underscores(self, tmp_path):
        arn = "arn:aws:secretsmanager:us-west-2:1:secret:x-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="my-db-password", source="aws_sm", arn=arn)]),
            out,
        )
        services = (out / "services.tf").read_text()
        assert 'name      = "MY_DB_PASSWORD"' in services
