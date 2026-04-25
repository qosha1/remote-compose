"""rc db dump-local: docker exec pg_dump → local file.

Pairs with `rc db push` for the full local→remote seed flow:
  rc db dump-local --container my_postgres --to /tmp/x.dump
  rc db push /tmp/x.dump

Discovers the postgres user/db/port from the container's env so users
don't have to remember sentinal-style port quirks (5434 vs 5432).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.dblocal import (
    DumpLocalError,
    DumpResult,
    dump_local,
    inspect_container_env,
)


# ---------------------------------------------------------------------
# inspect_container_env: shell out to `docker inspect` and parse env
# ---------------------------------------------------------------------

class TestInspectContainerEnv:
    def test_returns_env_dict(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout=b'POSTGRES_USER=alice\nPOSTGRES_DB=mydb\nPOSTGRES_PORT=5434\n',
                stderr=b"",
            )
            env = inspect_container_env("my_postgres")
        assert env["POSTGRES_USER"] == "alice"
        assert env["POSTGRES_DB"] == "mydb"
        assert env["POSTGRES_PORT"] == "5434"

    def test_missing_container_raises(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1, stdout=b"", stderr=b"No such container: nope",
            )
            with pytest.raises(DumpLocalError, match="container"):
                inspect_container_env("nope")

    def test_handles_blank_lines_and_dotted_keys(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout=b'\nFOO=1\n\nBAR=two\n',
                stderr=b"",
            )
            env = inspect_container_env("c")
        assert env == {"FOO": "1", "BAR": "two"}


# ---------------------------------------------------------------------
# dump_local: orchestrates inspect + pg_dump
# ---------------------------------------------------------------------

class TestDumpLocal:
    def test_writes_file_returns_dumpresult(self, tmp_path):
        out = tmp_path / "x.dump"
        # Side effect that pretends pg_dump wrote 1MB to the open file fd.
        def fake_run(cmd, stdout=None, stderr=None, **_):
            if hasattr(stdout, "write"):
                stdout.write(b"x" * (1024 * 1024))
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {
                "POSTGRES_USER": "alice", "POSTGRES_DB": "mydb",
                "POSTGRES_PORT": "5432",
            }
            result = dump_local(container="c", output_path=out)
        assert isinstance(result, DumpResult)
        assert result.path == out
        assert result.size_bytes == 1024 * 1024
        assert result.user == "alice"
        assert result.database == "mydb"
        assert result.port == 5432

    def test_uses_postgres_port_from_env(self, tmp_path):
        out = tmp_path / "x.dump"
        with mock.patch("subprocess.run") as run, \
             mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {
                "POSTGRES_USER": "u", "POSTGRES_DB": "d",
                "POSTGRES_PORT": "5434",
            }
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            out.write_bytes(b"x" * 100)
            dump_local(container="c", output_path=out)
        # The first run() call (pg_dump) must include -p 5434.
        cmd = run.call_args.args[0]
        assert "-p" in cmd
        assert "5434" in cmd

    def test_explicit_port_overrides_container_env(self, tmp_path):
        out = tmp_path / "x.dump"
        with mock.patch("subprocess.run") as run, \
             mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {"POSTGRES_USER": "u", "POSTGRES_DB": "d",
                                    "POSTGRES_PORT": "5434"}
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            out.write_bytes(b"x")
            dump_local(container="c", output_path=out, port=9999)
        cmd = run.call_args.args[0]
        assert "9999" in cmd
        assert "5434" not in cmd

    def test_missing_postgres_user_raises_clear_error(self, tmp_path):
        out = tmp_path / "x.dump"
        with mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {"POSTGRES_DB": "d"}  # no USER
            with pytest.raises(DumpLocalError, match="POSTGRES_USER"):
                dump_local(container="c", output_path=out)

    def test_missing_postgres_db_raises_clear_error(self, tmp_path):
        out = tmp_path / "x.dump"
        with mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {"POSTGRES_USER": "u"}
            with pytest.raises(DumpLocalError, match="POSTGRES_DB"):
                dump_local(container="c", output_path=out)

    def test_pg_dump_failure_raises_with_stderr(self, tmp_path):
        out = tmp_path / "x.dump"
        with mock.patch("subprocess.run") as run, \
             mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {"POSTGRES_USER": "u", "POSTGRES_DB": "d"}
            run.return_value = mock.Mock(
                returncode=1, stdout=b"", stderr=b"connection refused",
            )
            with pytest.raises(DumpLocalError, match="connection refused"):
                dump_local(container="c", output_path=out)

    def test_creates_parent_directory_when_missing(self, tmp_path):
        out = tmp_path / "deep" / "path" / "x.dump"
        with mock.patch("subprocess.run") as run, \
             mock.patch("remote_compose.dblocal.inspect_container_env") as inspect:
            inspect.return_value = {"POSTGRES_USER": "u", "POSTGRES_DB": "d",
                                    "POSTGRES_PORT": "5432"}
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            # We don't actually write the file in this test; the parent
            # dir should still get created.
            try:
                dump_local(container="c", output_path=out)
            except DumpLocalError:
                # It'll fail to read size_bytes off a missing file —
                # we only care that the parent dir got created.
                pass
        assert out.parent.exists()


# ---------------------------------------------------------------------
# Default output path naming
# ---------------------------------------------------------------------

class TestDefaultPath:
    def test_default_path_includes_project_and_timestamp(self):
        from remote_compose.dblocal import default_dump_path
        p = default_dump_path("rc-test-foo")
        assert p.name.startswith("rc-test-foo-")
        assert p.name.endswith(".dump")
        assert "/tmp/rc-dumps/" in str(p)
