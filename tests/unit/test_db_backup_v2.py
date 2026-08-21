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

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

import remote_compose.cli_commands._dispatchers as dispatchers
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


def _s3_stub(head_size: int | None = 4096, parts: int | None = 1):
    """boto3 S3 client stub.

    head_size=None -> object missing (404). parts=0 -> ListParts came back
    empty, i.e. nothing the container claimed to upload actually landed.
    """
    s3 = mock.MagicMock()
    s3.create_multipart_upload.return_value = {"UploadId": "upload-1"}
    s3.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: (
        f"https://rc-test-dumps.s3.amazonaws.com/{Params['Key']}"
        f"?partNumber={Params.get('PartNumber')}&X-Amz-Signature=deadbeef"
    )
    s3.list_parts.return_value = {
        "Parts": [
            {"PartNumber": n, "ETag": f'"etag{n}"'} for n in range(1, (parts or 0) + 1)
        ],
        "IsTruncated": False,
    }
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


def _ok_exec(stdout="dump bytes: 4096\nuploading 1 part(s)\nuploaded\n"):
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


def test_backup_v2_dumps_in_the_backup_service_and_uploads_presigned_parts(tmp_path):
    result, provider, s3 = _run_backup(tmp_path, _ok_exec(), _s3_stub())
    assert result.exit_code == 0, result.output

    # Presigned upload_part (not put_object): a single PUT caps at 5 GiB and
    # startsimpli-prod's dump is already past it (startsim-36qr).
    ops = {c.args[0] for c in s3.generate_presigned_url.call_args_list}
    assert ops == {"upload_part"}, ops
    assert s3.create_multipart_upload.call_count == 1

    _ctx, service, command = provider.exec.call_args.args[:3]
    assert service == "django", "must exec in backup.service, not postgres"
    script = command[2]
    assert "pg_dump" in script
    assert "-Fc" in script, "custom format keeps pg_restore --clean usable"
    assert "curl" in script and "-T" in script
    assert "dd if=" in script, "parts are cut with dd, not a second full copy"

    # URLs are POSITIONAL ARGS, never interpolated into the script body — a
    # signature cannot break out of a shlex-quoted argv element.
    assert "X-Amz-Signature" not in script, script[:200]
    assert command[3] == "rcbackup", "argv[0] placeholder so URLs start at $1"
    urls = command[4:]
    assert len(urls) == 64, len(urls)
    assert all("X-Amz-Signature=deadbeef" in u for u in urls)


def test_backup_v2_completes_from_list_parts_not_from_container_output(tmp_path):
    """S3 is the authority on what landed; the container is only the reporter."""
    result, _, s3 = _run_backup(tmp_path, _ok_exec(), _s3_stub(parts=3))
    assert result.exit_code == 0, result.output
    sent = s3.complete_multipart_upload.call_args.kwargs["MultipartUpload"]["Parts"]
    assert sent == [
        {"ETag": '"etag1"', "PartNumber": 1},
        {"ETag": '"etag2"', "PartNumber": 2},
        {"ETag": '"etag3"', "PartNumber": 3},
    ], sent


def test_backup_v2_aborts_the_upload_when_no_part_landed(tmp_path):
    """Orphaned multipart parts bill indefinitely."""
    result, _, s3 = _run_backup(tmp_path, _ok_exec(), _s3_stub(parts=0))
    assert result.exit_code != 0, result.output
    assert s3.abort_multipart_upload.call_count == 1
    assert s3.complete_multipart_upload.call_count == 0


def test_backup_v2_fails_when_the_dump_command_fails(tmp_path):
    bad = mock.MagicMock()
    bad.exit_code = 1
    bad.stdout = "pg_dump: error: connection failed\n"
    bad.stderr = ""
    result, _, s3 = _run_backup(tmp_path, bad, _s3_stub())
    assert result.exit_code != 0, result.output
    assert s3.head_object.call_count == 0, "no point verifying after a failed dump"
    assert s3.abort_multipart_upload.call_count == 1, "must not strand parts"
    assert s3.complete_multipart_upload.call_count == 0


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


# --------------------------------------------------------------------------
# The generated shell script, actually executed.
#
# Everything above mocks Provider.exec, so it proves rc's side and nothing
# about the script rc sends. The script is where the risk is: part arithmetic,
# dd offsets, the too-large guard, and cleanup. This runs it under /bin/sh
# with fake pg_dump/curl on PATH and checks the bytes.
# --------------------------------------------------------------------------


FAKE_PG_DUMP = """#!/bin/sh
prev=""; out=""
for a in "$@"; do [ "$prev" = "-f" ] && out="$a"; prev="$a"; done
dd if=/dev/zero of="$out" bs=1024 count=$FAKE_DUMP_KB 2>/dev/null
"""

FAKE_CURL = """#!/bin/sh
prev=""; f=""; u=""
for a in "$@"; do
  [ "$prev" = "-T" ] && f="$a"
  case "$a" in https://*) u="$a";; esac
  prev="$a"
done
echo "$(wc -c < "$f") $u" >> "$CURL_LOG"
"""


def _run_script(tmp_path, monkeypatch, dump_kb, urls, part_kb=1024):
    monkeypatch.setattr(dispatchers, "_BACKUP_PART_BYTES", part_kb * 1024)
    binv = tmp_path / "bin"
    binv.mkdir()
    for name, body in (("pg_dump", FAKE_PG_DUMP), ("curl", FAKE_CURL)):
        f = binv / name
        f.write_text(body)
        f.chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("")
    env = dict(os.environ)
    env.update(
        PATH=f"{binv}:{env['PATH']}",
        CURL_LOG=str(log),
        FAKE_DUMP_KB=str(dump_kb),
        POSTGRES_PASSWORD="x",
        POSTGRES_USER="u",
        POSTGRES_DB="d",
        TMPDIR=str(tmp_path),
    )
    proc = subprocess.run(
        # keepalive_s=1 so the marker loop retires promptly; at the default
        # 30s every one of these would sit for half a minute at EOF.
        ["sh", "-c", dispatchers._build_dump_script(1), "rcbackup", *urls],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    uploads = [
        (int(ln.split()[0]), ln.split()[1])
        for ln in log.read_text().splitlines()
        if ln.strip()
    ]
    return proc, uploads


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_script_splits_the_dump_on_exact_part_boundaries(tmp_path, monkeypatch):
    """2.5 parts of data -> full, full, remainder. Wrong dd offsets corrupt
    the dump in a way no mock can see."""
    proc, uploads = _run_script(
        tmp_path,
        monkeypatch,
        dump_kb=2560,
        part_kb=1024,
        urls=[f"https://p{n}" for n in range(1, 65)],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [u[0] for u in uploads] == [1048576, 1048576, 524288], uploads
    assert [u[1] for u in uploads] == ["https://p1", "https://p2", "https://p3"]
    assert sum(u[0] for u in uploads) == 2560 * 1024


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_script_uses_one_part_for_a_small_dump(tmp_path, monkeypatch):
    proc, uploads = _run_script(
        tmp_path,
        monkeypatch,
        dump_kb=64,
        part_kb=1024,
        urls=[f"https://p{n}" for n in range(1, 65)],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert uploads == [(65536, "https://p1")], uploads


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_script_refuses_before_uploading_when_parts_do_not_cover_the_dump(
    tmp_path, monkeypatch
):
    """Fail fast. A half-uploaded multipart is worse than a clean refusal,
    and the dump already cost however long it cost."""
    proc, uploads = _run_script(
        tmp_path,
        monkeypatch,
        dump_kb=2560,
        part_kb=1024,
        urls=["https://p1"],
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert uploads == [], "must not upload a single byte it cannot finish"
    assert "Raise the part size" in proc.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_script_fails_and_uploads_nothing_when_pg_dump_fails(tmp_path, monkeypatch):
    binv = tmp_path / "bin"
    proc, uploads = _run_script(
        tmp_path,
        monkeypatch,
        dump_kb=1,
        part_kb=1024,
        urls=["https://p1"],
    )
    # Replace pg_dump with a failing one and re-run.
    (binv / "pg_dump").write_text("#!/bin/sh\nexit 1\n")
    (binv / "pg_dump").chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("")
    env = dict(os.environ)
    env.update(
        PATH=f"{binv}:{env['PATH']}",
        CURL_LOG=str(log),
        FAKE_DUMP_KB="1",
        POSTGRES_PASSWORD="x",
        POSTGRES_USER="u",
        POSTGRES_DB="d",
    )
    proc = subprocess.run(
        ["sh", "-c", dispatchers._build_dump_script(1), "rcbackup", "https://p1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert log.read_text().strip() == "", "nothing may be uploaded"
    assert "pg_dump failed" in proc.stdout
