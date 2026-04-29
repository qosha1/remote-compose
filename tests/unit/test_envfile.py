"""Unit tests for remote_compose.envfile."""

from __future__ import annotations

import pytest

from remote_compose.envfile import EnvFileError, keys, parse


def _write(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body)
    return p


class TestParse:
    def test_simple(self, tmp_path):
        p = _write(tmp_path, "FOO=1\nBAR=two\n")
        assert parse(p) == {"FOO": "1", "BAR": "two"}

    def test_strips_quotes(self, tmp_path):
        p = _write(tmp_path, 'A="hello"\nB=\'world\'\n')
        assert parse(p) == {"A": "hello", "B": "world"}

    def test_preserves_mismatched_quotes(self, tmp_path):
        p = _write(tmp_path, 'A="only-opening\n')
        assert parse(p) == {"A": '"only-opening'}

    def test_handles_export_prefix(self, tmp_path):
        p = _write(tmp_path, "export DATABASE_URL=postgresql://x\n")
        assert parse(p) == {"DATABASE_URL": "postgresql://x"}

    def test_skips_comments_and_blanks(self, tmp_path):
        p = _write(tmp_path, "# comment\n\nFOO=1\n# trailing\n")
        assert parse(p) == {"FOO": "1"}

    def test_value_with_equals(self, tmp_path):
        p = _write(tmp_path, "DATABASE_URL=postgres://u:p=p@h/d\n")
        assert parse(p) == {"DATABASE_URL": "postgres://u:p=p@h/d"}

    def test_empty_value(self, tmp_path):
        p = _write(tmp_path, "FOO=\n")
        assert parse(p) == {"FOO": ""}

    def test_missing_file(self, tmp_path):
        with pytest.raises(EnvFileError, match="not found"):
            parse(tmp_path / "missing.env")

    def test_line_without_equals_rejected(self, tmp_path):
        p = _write(tmp_path, "FOO=1\nNO_EQUALS_HERE\nBAR=2\n")
        with pytest.raises(EnvFileError, match="expected KEY=value"):
            parse(p)

    def test_invalid_key_name_rejected(self, tmp_path):
        p = _write(tmp_path, "FOO-BAR=1\n")
        with pytest.raises(EnvFileError, match="invalid env var name"):
            parse(p)

    def test_key_starting_with_digit_rejected(self, tmp_path):
        p = _write(tmp_path, "1FOO=1\n")
        with pytest.raises(EnvFileError, match="invalid env var name"):
            parse(p)

    def test_duplicate_key_rejected(self, tmp_path):
        p = _write(tmp_path, "FOO=1\nFOO=2\n")
        with pytest.raises(EnvFileError, match="duplicate"):
            parse(p)


class TestKeys:
    def test_preserves_declaration_order(self, tmp_path):
        p = _write(tmp_path, "B=1\nA=2\nC=3\n")
        assert keys(p) == ["B", "A", "C"]

    def test_empty_file(self, tmp_path):
        p = _write(tmp_path, "")
        assert keys(p) == []
