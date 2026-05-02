"""rc-xmz: rc db push surfaces pg_restore errors that pg_restore itself
swallows (exits 0 on partial-success). Sentinal repro lost
workflows_pagecapture this way; the user only saw 'rc db push: complete'.
"""

from __future__ import annotations

from remote_compose.cli_commands._dispatchers import (
    _count_pg_restore_errors,
    _pg_restore_ignored_count,
)


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


class TestPgRestoreIgnoredCount:
    """rc-ln1: parse 'errors ignored on restore: N' from pg_restore output.

    Without --exit-on-error, pg_restore continues past per-object failures
    (extension exists, role missing, FK ordering on DROP) and emits this
    summary line. When N == count of pg_restore: error: lines, exit-1
    means warnings, not data loss.
    """

    def test_returns_count_from_stderr(self):
        stderr = "pg_restore: warning: errors ignored on restore: 4\n"
        assert _pg_restore_ignored_count("", stderr) == 4

    def test_returns_count_from_stdout(self):
        stdout = "pg_restore: warning: errors ignored on restore: 7\n"
        assert _pg_restore_ignored_count(stdout, "") == 7

    def test_zero_count_returned_when_present(self):
        # 0 is meaningful — pg_restore confirmed it ran with no ignored errors.
        stderr = "pg_restore: warning: errors ignored on restore: 0\n"
        assert _pg_restore_ignored_count("", stderr) == 0

    def test_returns_none_when_summary_absent(self):
        # No summary line — caller can't tell if pg_restore would have
        # ignored errors or not. Returns None (not 0) so the caller
        # distinguishes 'no summary' from 'zero ignored'.
        stderr = "pg_restore: error: could not execute query\n"
        assert _pg_restore_ignored_count("", stderr) is None

    def test_case_insensitive_match(self):
        stderr = "PG_RESTORE: WARNING: ERRORS IGNORED ON RESTORE: 3\n"
        assert _pg_restore_ignored_count("", stderr) == 3

    def test_handles_empty_streams(self):
        assert _pg_restore_ignored_count("", "") is None
