"""Integration test for `rc db push` v2 path.

Proves the local-dump → S3 → in-container restore wiring without a live
ECS task. moto-backed S3 verifies the upload + presigned URL + cleanup;
the provider.exec call is intercepted to capture the restore script that
WOULD have run inside the container.

Three formats covered: .dump (pg_restore), .sql (psql), .tar.gz
(tar+pg_restore — directory format).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest import mock

import boto3
import pytest
import yaml
from click.testing import CliRunner
from moto import mock_aws

from remote_compose.cli import cli as rc_cli
from remote_compose.provider.base import ExecResult


pytestmark = pytest.mark.integration


_BUCKET = "rc-test-db-push"
_REGION = "us-west-2"


def _write_v2_project(tmp_path: Path, *, with_backup_service: bool = True) -> Path:
    compose = {"services": {
        "django": {"image": "busybox"},
        "postgres": {"image": "postgres:16-alpine"},
    }}
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    rc = {
        "version": 2,
        "project": "itest-rcpush",
        "compose_file": "docker-compose.yml",
        "provider": "fake",
        "provider_config": {"ecs": {
            "region": _REGION,
            "cluster": "itest-cluster",
            "vpc_cidr": "10.0.0.0/16",
        }},
        "backup": {"bucket": _BUCKET},
        "services": {
            "django": {"cpu": 256, "memory": 512},
            "postgres": {"cpu": 256, "memory": 512},
        },
    }
    if with_backup_service:
        rc["backup"]["service"] = "postgres"
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(rc, sort_keys=False))
    return p


def _write_dump(tmp_path: Path, name: str, body: bytes = b"binary-pg-dump-bytes") -> Path:
    p = tmp_path / name
    p.write_bytes(body)
    return p


@pytest.fixture(autouse=True)
def _reset_fake_provider():
    from remote_compose.provider.fake import FakeProvider
    FakeProvider.reset()
    yield
    FakeProvider.reset()


@pytest.fixture
def s3_bucket():
    with mock_aws():
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(
            Bucket=_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": _REGION},
        )
        yield s3


# ---------------------------------------------------------------------
# Round-trip per dump format
# ---------------------------------------------------------------------

class TestDbPushS3RoundTrip:
    @pytest.mark.parametrize(
        "filename,expected_restore_token",
        [
            ("seed.dump", "pg_restore"),       # custom format
            ("seed.sql", "psql"),               # plain SQL
            ("seed.tar.gz", "tar -xzf"),       # tar + pg_restore -Fd
        ],
    )
    def test_full_round_trip(
        self, filename, expected_restore_token, tmp_path, s3_bucket,
    ):
        rc_path = _write_v2_project(tmp_path)
        dump_path = _write_dump(tmp_path, filename)

        captured: dict = {}

        def fake_exec(self, ctx, service, command, **kwargs):
            captured["service"] = service
            captured["command"] = command
            return ExecResult(exit_code=0, stdout="restored ok\n", stderr="")

        with mock.patch(
            "remote_compose.provider.fake.FakeProvider.exec",
            new=fake_exec,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli,
                ["--config", str(rc_path), "db", "push", str(dump_path), "--yes"],
            )

        assert result.exit_code == 0, (
            f"db push failed: stdout={result.output} exc={result.exception}"
        )

        # 1. S3 has the upload deleted post-restore (cleanup happened).
        listed = s3_bucket.list_objects_v2(Bucket=_BUCKET).get("Contents") or []
        assert listed == [], (
            f"S3 cleanup failed — found leftover objects: "
            f"{[o['Key'] for o in listed]}"
        )

        # 2. provider.exec was invoked against the backup.service.
        assert captured["service"] == "postgres"

        # 3. Restore script was passed via sh -c.
        assert captured["command"][:2] == ["sh", "-c"]
        script = captured["command"][2]

        # 4. Script downloads via curl/wget bootstrap...
        assert "curl" in script and "wget" in script, (
            "expected curl+wget bootstrap in restore script"
        )

        # 5. ...uses the right restore tool for this format...
        assert expected_restore_token in script, (
            f"expected '{expected_restore_token}' in restore script for "
            f"{filename!r}; got:\n{script[:500]}"
        )

        # 6. ...and contains the presigned URL (must include the S3 host).
        assert ".s3" in script and "amazonaws.com" in script, (
            f"presigned URL not embedded in script:\n{script[:500]}"
        )

        # 7. The presigned URL refers to the project + filename.
        assert "itest-rcpush/pushed/" in script
        assert filename in script

    def test_unknown_extension_errors_before_upload(self, tmp_path, s3_bucket):
        rc_path = _write_v2_project(tmp_path)
        dump_path = _write_dump(tmp_path, "seed.unknown")

        runner = CliRunner()
        result = runner.invoke(
            rc_cli,
            ["--config", str(rc_path), "db", "push", str(dump_path), "--yes"],
        )
        assert result.exit_code != 0
        # Nothing landed in S3.
        listed = s3_bucket.list_objects_v2(Bucket=_BUCKET).get("Contents") or []
        assert listed == []

    def test_restore_failure_propagates_exit_code_but_still_cleans_s3(
        self, tmp_path, s3_bucket,
    ):
        """Restore exits non-zero — CLI must surface the exit code AND still
        delete the S3 object so storage doesn't accumulate."""
        rc_path = _write_v2_project(tmp_path)
        dump_path = _write_dump(tmp_path, "seed.dump")

        def fake_exec(self, ctx, service, command, **kwargs):
            return ExecResult(exit_code=42, stdout="", stderr="restore failed")

        with mock.patch(
            "remote_compose.provider.fake.FakeProvider.exec",
            new=fake_exec,
        ):
            runner = CliRunner()
            result = runner.invoke(
                rc_cli,
                ["--config", str(rc_path), "db", "push", str(dump_path), "--yes"],
            )

        assert result.exit_code == 42, f"expected exit 42, got {result.exit_code}"
        listed = s3_bucket.list_objects_v2(Bucket=_BUCKET).get("Contents") or []
        assert listed == [], (
            f"S3 not cleaned up after restore failure: "
            f"{[o['Key'] for o in listed]}"
        )

    def test_missing_backup_bucket_errors(self, tmp_path):
        """rc.yml without backup.bucket must error before any boto3 call."""
        compose = {"services": {"django": {"image": "busybox"}}}
        (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
        (tmp_path / "rc.yml").write_text(yaml.safe_dump({
            "version": 2,
            "project": "itest",
            "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {"django": {"cpu": 256, "memory": 512}},
        }))
        dump_path = _write_dump(tmp_path, "seed.dump")
        runner = CliRunner()
        result = runner.invoke(
            rc_cli,
            ["--config", str(tmp_path / "rc.yml"), "db", "push",
             str(dump_path), "--yes"],
        )
        assert result.exit_code != 0
        assert "backup.bucket" in result.output

    def test_missing_backup_service_errors_when_no_flag(
        self, tmp_path, s3_bucket,
    ):
        """No backup.service in rc.yml AND no --service flag → error."""
        rc_path = _write_v2_project(tmp_path, with_backup_service=False)
        dump_path = _write_dump(tmp_path, "seed.dump")
        runner = CliRunner()
        result = runner.invoke(
            rc_cli,
            ["--config", str(rc_path), "db", "push", str(dump_path), "--yes"],
        )
        assert result.exit_code != 0
        assert "backup.service" in result.output or "--service" in result.output
