"""Failing unit tests for `rc init --remote-backend` + TerraformBackend
schema validation (5h8.3 RED gate).

Cover:
  b. rc init --remote-backend writes the s3 backend block into rc.yml.v2
  + TerraformBackend.validate(): type=s3 requires bucket+key (sys reqs spec)
"""

from __future__ import annotations

from unittest import mock

import pytest
import yaml
from click.testing import CliRunner


class TestRcInitRemoteBackend:
    def test_remote_backend_flag_writes_s3_block(self, tmp_path):
        """rc init --remote-backend creates rc.yml with type=s3 backend
        populated by the discovered AWS account id and the configured region."""
        from remote_compose.cli import cli as rc_cli

        # Stub the bootstrap helpers + STS so we don't touch real AWS.
        with (
            mock.patch("remote_compose.state_backend.bootstrap.bootstrap_bucket") as bb,
            mock.patch(
                "remote_compose.state_backend.bootstrap.bootstrap_lock_table"
            ) as bl,
            mock.patch("boto3.Session") as session_cls,
        ):
            bb.return_value = "033937118837-rc-tfstate"
            bl.return_value = "rc-tfstate-locks"
            sts = mock.MagicMock()
            sts.get_caller_identity.return_value = {"Account": "033937118837"}
            session_cls.return_value.client.return_value = sts

            runner = CliRunner()
            target = tmp_path / "rc.yml"
            result = runner.invoke(
                rc_cli,
                [
                    "init",
                    "--remote-backend",
                    "--region",
                    "us-west-2",
                    "-o",
                    str(target),
                ],
            )

        assert result.exit_code == 0, result.output
        rc = yaml.safe_load(target.read_text())
        backend = rc["terraform"]["backend"]
        assert backend["type"] == "s3"
        assert backend["bucket"] == "033937118837-rc-tfstate"
        assert backend["key"]  # populated, exact format in 3.2 design
        assert backend["region"] == "us-west-2"
        assert backend["dynamodb_table"] == "rc-tfstate-locks"

    def test_remote_backend_flag_calls_bootstrap_helpers(self, tmp_path):
        """The flag must trigger bucket + lock-table bootstrap so the FIRST
        rc deploy from any box works without a second manual step."""
        from remote_compose.cli import cli as rc_cli

        with (
            mock.patch("remote_compose.state_backend.bootstrap.bootstrap_bucket") as bb,
            mock.patch(
                "remote_compose.state_backend.bootstrap.bootstrap_lock_table"
            ) as bl,
            mock.patch("boto3.Session") as session_cls,
        ):
            bb.return_value = "033937118837-rc-tfstate"
            bl.return_value = "rc-tfstate-locks"
            sts = mock.MagicMock()
            sts.get_caller_identity.return_value = {"Account": "033937118837"}
            session_cls.return_value.client.return_value = sts

            runner = CliRunner()
            target = tmp_path / "rc.yml"
            result = runner.invoke(
                rc_cli,
                [
                    "init",
                    "--remote-backend",
                    "--region",
                    "us-west-2",
                    "-o",
                    str(target),
                ],
            )

        assert result.exit_code == 0, result.output
        bb.assert_called_once()
        bl.assert_called_once()

    def test_init_without_flag_keeps_local_backend(self, tmp_path):
        """Back-compat: rc init without --remote-backend writes a v2 rc.yml
        whose backend block is type=local (or absent — local is the
        default). Bootstrap helpers must NOT be called."""
        from remote_compose.cli import cli as rc_cli

        with (
            mock.patch("remote_compose.state_backend.bootstrap.bootstrap_bucket") as bb,
            mock.patch(
                "remote_compose.state_backend.bootstrap.bootstrap_lock_table"
            ) as bl,
        ):
            runner = CliRunner()
            target = tmp_path / "rc.yml"
            result = runner.invoke(
                rc_cli,
                ["init", "-o", str(target)],
            )

        assert result.exit_code == 0, result.output
        rc = yaml.safe_load(target.read_text())
        backend = rc.get("terraform", {}).get("backend", {})
        # Either explicitly local or absent — anything BUT s3.
        assert backend.get("type", "local") == "local"
        bb.assert_not_called()
        bl.assert_not_called()


class TestTerraformBackendValidation:
    def test_s3_backend_requires_bucket(self):
        """TerraformBackend.validate() raises ConfigError when type=s3 and
        bucket is missing."""
        from remote_compose.config._schema_types import (
            ConfigError,
            TerraformBackend,
        )

        be = TerraformBackend(type="s3", key="x/y/z.tfstate", region="us-west-2")
        with pytest.raises(ConfigError, match="bucket"):
            be.validate()

    def test_s3_backend_requires_key(self):
        """TerraformBackend.validate() raises ConfigError when type=s3 and
        key is missing."""
        from remote_compose.config._schema_types import (
            ConfigError,
            TerraformBackend,
        )

        be = TerraformBackend(type="s3", bucket="my-bucket", region="us-west-2")
        with pytest.raises(ConfigError, match="key"):
            be.validate()

    def test_s3_backend_with_required_fields_validates(self):
        """Happy path: bucket+key+region present → validate() returns clean."""
        from remote_compose.config._schema_types import TerraformBackend

        be = TerraformBackend(
            type="s3",
            bucket="my-bucket",
            key="proj/env/ecs.tfstate",
            region="us-west-2",
            dynamodb_table="rc-tfstate-locks",
        )
        be.validate()  # no raise

    def test_local_backend_skips_s3_required_field_check(self):
        """Back-compat: type=local doesn't require bucket/key."""
        from remote_compose.config._schema_types import TerraformBackend

        be = TerraformBackend(type="local")
        be.validate()  # no raise

    def test_dynamodb_table_warning_when_s3_without_lock(self, capsys):
        """When type=s3 but no dynamodb_table is configured, emit a warning
        (recommended but not required). Concurrent deploys can corrupt state
        without it."""
        from remote_compose.config._schema_types import TerraformBackend

        be = TerraformBackend(
            type="s3",
            bucket="b",
            key="k",
            region="us-west-2",
            # dynamodb_table omitted on purpose
        )
        be.validate()  # no raise
        captured = capsys.readouterr()
        # System reqs spec: emit a config WARNING (stderr) noting the gap.
        out = captured.err + captured.out
        assert "dynamodb_table" in out.lower() or "lock" in out.lower()
