"""rc-2kp: rc fix subcommands write a sentinel; next rc up consumes
it and forces --no-cache so stale buildx layer cache can't silently
mask the user's edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.no_cache_state import (
    consume_no_cache,
    is_no_cache_pending,
    mark_no_cache,
)


class TestNoCacheState:
    def test_mark_creates_sentinel(self, tmp_path):
        assert not is_no_cache_pending(tmp_path)
        mark_no_cache(tmp_path)
        assert is_no_cache_pending(tmp_path)

    def test_consume_clears_sentinel_and_returns_true(self, tmp_path):
        mark_no_cache(tmp_path)
        assert consume_no_cache(tmp_path) is True
        assert not is_no_cache_pending(tmp_path)

    def test_consume_returns_false_when_no_sentinel(self, tmp_path):
        assert consume_no_cache(tmp_path) is False

    def test_mark_is_idempotent(self, tmp_path):
        mark_no_cache(tmp_path, reason="first")
        mark_no_cache(tmp_path, reason="second")
        assert is_no_cache_pending(tmp_path)
        assert consume_no_cache(tmp_path) is True
        # Only one consume; second returns False.
        assert consume_no_cache(tmp_path) is False

    def test_mark_creates_parent_dir(self, tmp_path):
        # .rc/ doesn't exist yet — mark must create it.
        deep = tmp_path / "fresh-project"
        deep.mkdir()
        mark_no_cache(deep, reason="rc fix bake-bind-mount-source django")
        assert (deep / ".rc/no-cache-next-build").exists()

    def test_reason_is_written_to_file(self, tmp_path):
        mark_no_cache(tmp_path, reason="rc fix django-tls")
        contents = (tmp_path / ".rc/no-cache-next-build").read_text()
        assert "rc fix django-tls" in contents
