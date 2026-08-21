"""rc-56bq: `rc db backup` on a v2 stack.

Before this, db_backup went straight to the v1 `_resolve_ecs_exec_target`,
which resolves the target through rc's LOCAL Django ORM (ECSCluster /
ECSService rows). A terraform-managed v2 stack has no rows there, so the
command died with "Error: Service 'django' not found." and an empty
available-services list — while the service was declared in rc.yml and
running in AWS the whole time.

`rc db push` already resolved via Provider.exec (live AWS); backup now
mirrors it, and additionally VERIFIES the uploaded object. A pg_dump that
dies mid-stream otherwise leaves the command looking successful, which is
the one failure mode a backup tool must not have.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from remote_compose.cli import cli


def _scaffold_v2(tmp_path: Path) -> Path:
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text(textwrap.dedent("""
        version: 2
        project: rc-test
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
            cluster: rc-test-cluster
            vpc_cidr: 10.0.0.0/16
        services:
          django:
            type: application
          postgres:
            type: infrastructure
        backup:
          bucket: rc-test-dumps
          service: django
    """).strip())
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  django:\n    image: django:latest\n"
        "  postgres:\n    image: postgres:17\n"
    )
    return rc_yml


def _s3_stub(head_size: int | None = 4096):
    """boto3 S3 client stub. head_size=None -> object missing (404)."""
    s3 = mock.MagicMock()
    s3.generate_presigned_url.return_value = (
        "https://rc-test-dumps.s3.amazonaws.com/rc-test/dump?X-Amz-Signature=deadbeef"
    )
    if head_size is None:
        s3.head_object.side_effect = Exception("Not Found: 404")
    else:
        s3.head_object.return_value = {"ContentLength": head_size}
    return s3


def _run_backup(tmp_path, exec_result, s3, extra_args=()):
    rc_yml = _scaffold_v2(tmp_path)
    provider = mock.MagicMock()
    provider.exec.return_value = exec_result
    session = mock.MagicMock()
    session.client.return_value = s3

    with (
        mock.patch("boto3.Session", return_value=session),
        mock.patch(
            "remote_compose.cli_v2.resolve_provider",
            return_value=provider,
        ),
        mock.patch("remote_compose.cli_v2.build_deploy_context"),
    ):
        result = CliRunner().invoke(
            cli, ["-c", str(rc_yml), "db", "backup", *extra_args]
        )
    return result, provider, s3


def _ok_exec(stdout="dump bytes: 4096\nuploaded\n"):
    r = mock.MagicMock()
    r.exit_code = 0
    r.stdout = stdout
    r.stderr = ""
    return r


def test_backup_v2_does_not_touch_the_v1_orm_registry(tmp_path):
    """The regression itself: v2 config must never reach _resolve_ecs_exec_target."""
    with mock.patch(
        "remote_compose.cli_commands._legacy._resolve_ecs_exec_target"
    ) as legacy:
        result, provider, _ = _run_backup(tmp_path, _ok_exec(), _s3_stub())

    assert legacy.call_count == 0, (
        "v2 stack fell through to the v1 ORM resolver:\n" + result.output
    )
    assert result.exit_code == 0, result.output
    assert "not found" not in result.output.lower()
    assert provider.exec.call_count == 1


def test_backup_v2_dumps_in_the_backup_service_and_puts_to_the_presigned_url(tmp_path):
    result, provider, s3 = _run_backup(tmp_path, _ok_exec(), _s3_stub())
    assert result.exit_code == 0, result.output

    # Presigned PUT (not GET) so the container needs no S3 grant at all.
    kwargs = s3.generate_presigned_url.call_args.args or ()
    assert "put_object" in kwargs, s3.generate_presigned_url.call_args

    ctx_arg, service, command = provider.exec.call_args.args[:3]
    assert service == "django", "must exec in backup.service, not postgres"
    script = command[-1]
    assert "pg_dump" in script
    assert "-Fc" in script, "custom format keeps pg_restore --clean usable"
    assert "curl" in script and "-T" in script, "upload is a PUT of the dump file"
    assert "X-Amz-Signature=deadbeef" in script


def test_backup_v2_fails_when_the_dump_command_fails(tmp_path):
    bad = mock.MagicMock()
    bad.exit_code = 1
    bad.stdout = "pg_dump: error: connection failed\n"
    bad.stderr = ""
    result, _, s3 = _run_backup(tmp_path, bad, _s3_stub())
    assert result.exit_code != 0, result.output
    assert s3.head_object.call_count == 0, "no point verifying after a failed dump"


def test_backup_v2_fails_when_the_object_never_landed(tmp_path):
    """Exit 0 from the container is not proof the object is in S3."""
    result, _, _ = _run_backup(tmp_path, _ok_exec(), _s3_stub(head_size=None))
    assert result.exit_code != 0, result.output
    assert "verify" in result.output.lower() or "missing" in result.output.lower()


def test_backup_v2_fails_on_a_zero_byte_object(tmp_path):
    result, _, _ = _run_backup(tmp_path, _ok_exec(), _s3_stub(head_size=0))
    assert result.exit_code != 0, result.output


def test_backup_v2_reports_the_verified_size(tmp_path):
    result, _, _ = _run_backup(tmp_path, _ok_exec(), _s3_stub(head_size=4096))
    assert result.exit_code == 0, result.output
    assert "4096" in result.output


def test_backup_v2_service_flag_overrides_rc_yml(tmp_path):
    result, provider, _ = _run_backup(
        tmp_path, _ok_exec(), _s3_stub(), extra_args=("--service", "postgres")
    )
    assert result.exit_code == 0, result.output
    assert provider.exec.call_args.args[1] == "postgres"


def test_backup_v2_rejects_a_service_not_in_rc_yml(tmp_path):
    result, provider, _ = _run_backup(
        tmp_path, _ok_exec(), _s3_stub(), extra_args=("--service", "nope")
    )
    assert result.exit_code != 0, result.output
    assert provider.exec.call_count == 0
