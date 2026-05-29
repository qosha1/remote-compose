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
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "test",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=secrets,
    )


def _write_env(
    tmp_path: Path, rel: str, body: str = "SECRET_KEY=x\nDATABASE_URL=y\n"
) -> Path:
    """Create an env file under tmp_path and return its path."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


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
            _ctx(tmp_path, [SecretRef(name="db", source="aws_sm", arn=arn)]),
            out,
        )
        # secrets.tf only creates placeholders for source=file secrets
        assert (out / "secrets.tf").read_text().strip() == ""

    def test_iam_policy_lists_aws_sm_arn(self, tmp_path):
        arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:myapp/db-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="db", source="aws_sm", arn=arn)]),
            out,
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
        _write_env(tmp_path, ".envs/.production/.django")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                [
                    SecretRef(
                        name="django", source="file", path=".envs/.production/.django"
                    )
                ],
            ),
            out,
        )
        secrets_tf = (out / "secrets.tf").read_text()
        assert 'aws_secretsmanager_secret" "django"' in secrets_tf
        assert 'aws_secretsmanager_secret_version" "django_placeholder"' in secrets_tf

    def test_placeholder_uses_ignore_changes_lifecycle(self, tmp_path):
        _write_env(tmp_path, ".envs/.production/.django")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                [
                    SecretRef(
                        name="django", source="file", path=".envs/.production/.django"
                    )
                ],
            ),
            out,
        )
        secrets_tf = (out / "secrets.tf").read_text()
        assert "ignore_changes = [secret_string]" in secrets_tf

    def test_task_def_emits_one_entry_per_key_with_json_selector(self, tmp_path):
        env = _write_env(tmp_path, ".django", "SECRET_KEY=x\nDATABASE_URL=y\nDEBUG=0\n")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="django", source="file", path=str(env))]),
            out,
        )
        services = (out / "services.tf").read_text()
        # One task-def `secrets[]` entry per key, each with ECS JSON-key syntax
        # "arn:KEY::" so the container gets individual env vars.
        for key in ("SECRET_KEY", "DATABASE_URL", "DEBUG"):
            assert f'name      = "{key}"' in services
            assert f":{key}::" in services
        # All three point at the same SM secret ARN.
        assert services.count("aws_secretsmanager_secret.django.arn") >= 3

    def test_iam_policy_references_terraform_arn(self, tmp_path):
        env = _write_env(tmp_path, ".django")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, [SecretRef(name="django", source="file", path=str(env))]),
            out,
        )
        iam = (out / "iam.tf").read_text()
        assert "task_execution_secrets" in iam
        assert "aws_secretsmanager_secret.django.arn" in iam

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="env file not found"):
            ECSProvider().emit_terraform(
                _ctx(
                    tmp_path,
                    [
                        SecretRef(
                            name="django", source="file", path="/nonexistent/.django"
                        )
                    ],
                ),
                tmp_path / "tf",
            )

    def test_empty_env_file_rejected(self, tmp_path):
        env = _write_env(tmp_path, ".empty", "# just a comment\n")
        with pytest.raises(ProviderConfigError, match="no KEY=value entries"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, [SecretRef(name="empty", source="file", path=str(env))]),
                tmp_path / "tf",
            )

    def test_missing_path_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="requires path"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, [SecretRef(name="django", source="file")]),
                tmp_path / "tf",
            )

    def test_malformed_env_file_rejected(self, tmp_path):
        env = _write_env(tmp_path, ".bad", "NO_EQUALS_HERE\n")
        with pytest.raises(ProviderConfigError, match="expected KEY=value"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, [SecretRef(name="bad", source="file", path=str(env))]),
                tmp_path / "tf",
            )

    def test_relative_path_resolved_against_compose_dir(self, tmp_path):
        # Relative path in rc.yml resolves against the compose file's dir,
        # matching docker-compose and user expectation.
        _write_env(tmp_path, ".envs/.prod/.django")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                [SecretRef(name="django", source="file", path=".envs/.prod/.django")],
            ),
            out,
        )
        # Succeeds only if the provider resolved the relative path correctly.
        assert 'aws_secretsmanager_secret" "django"' in (out / "secrets.tf").read_text()


class TestMixedSecrets:
    def test_file_and_aws_sm_coexist(self, tmp_path):
        env = _write_env(tmp_path, ".django")
        arn = "arn:aws:secretsmanager:us-west-2:1:secret:db-AbCdEf"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                [
                    SecretRef(name="django", source="file", path=str(env)),
                    SecretRef(name="db", source="aws_sm", arn=arn),
                ],
            ),
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
        """Secret VALUES from the env file must not land in HCL, ever."""
        sentinel = "SECRET_SENTINEL_f00ba2c0de"
        env = _write_env(tmp_path, ".django", f"SECRET_KEY={sentinel}\nOTHER=y\n")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                [
                    SecretRef(name="django", source="file", path=str(env)),
                ],
            ),
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
            _ctx(
                tmp_path, [SecretRef(name="my-db-password", source="aws_sm", arn=arn)]
            ),
            out,
        )
        services = (out / "services.tf").read_text()
        assert 'name      = "MY_DB_PASSWORD"' in services
