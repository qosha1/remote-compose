"""rc-xmz: rc db push surfaces pg_restore errors that pg_restore itself
swallows (exits 0 on partial-success). Sentinal repro lost
workflows_pagecapture this way; the user only saw 'rc db push: complete'.
"""

from __future__ import annotations

from remote_compose.cli_commands._dispatchers import _count_pg_restore_errors


class TestCountPgRestoreErrors:
    def test_clean_run_returns_zero(self):
        stdout = (
            "pg_restore: connecting to database for restore\n"
            "pg_restore: creating TABLE \"public.users\"\n"
            "pg_restore: processing data for table \"public.users\"\n"
        )
        assert _count_pg_restore_errors(stdout, "") == 0

    def test_warnings_are_not_counted_as_errors(self):
        # rc-ln1 covers benign WARNING handling separately. This counter
        # MUST NOT trip on warnings.
        stderr = (
            "pg_restore: warning: errors ignored on restore: 0\n"
            "pg_restore: warning: relation already exists\n"
        )
        assert _count_pg_restore_errors("", stderr) == 0

    def test_single_error_line_in_stderr(self):
        stderr = (
            "pg_restore: error: could not execute query: ERROR: "
            "table \"workflows_pagecapture\" does not exist\n"
        )
        assert _count_pg_restore_errors("", stderr) == 1

    def test_multiple_errors_across_stdout_and_stderr(self):
        stdout = "pg_restore: error: could not execute query for table A\n"
        stderr = (
            "pg_restore: error: could not execute query for table B\n"
            "pg_restore: error: could not execute query for table C\n"
        )
        assert _count_pg_restore_errors(stdout, stderr) == 3

    def test_case_insensitive(self):
        stderr = "PG_RESTORE: ERROR: something broke\n"
        assert _count_pg_restore_errors("", stderr) == 1

    def test_handles_none_streams(self):
        assert _count_pg_restore_errors("", "") == 0
