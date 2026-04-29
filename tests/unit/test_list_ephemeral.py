"""Tests for `rc list --ephemeral` (rc-e5u.44.16).

Covers:
  - empty registry: friendly message, no headers
  - non-empty: header row + each stack as a row
  - --json: machine-parseable shape with ttl_remaining_seconds + expired flag
  - relative-time formatting for created + ttl
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli, _format_relative_time
from remote_compose.ephemeral import EphemeralRecord


@pytest.fixture
def runner():
    return CliRunner()


def _record(project="proj-a", expires_in: timedelta = timedelta(hours=2),
            created_ago: timedelta = timedelta(minutes=30)):
    now = datetime.now(timezone.utc)
    expires = now + expires_in
    created = now - created_ago

    def iso(d: datetime) -> str:
        return d.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return EphemeralRecord(
        project=project,
        region="us-west-1",
        aws_profile="default",
        expires_at=iso(expires),
        rc_yml_path="/tmp/rc.yml",
        terraform_dir="/tmp/tf",
        created_at=iso(created),
    )


# ---------------------------------------------------------------------------
# _format_relative_time helper
# ---------------------------------------------------------------------------

class TestFormatRelativeTime:
    def test_minutes_in_future(self):
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        future = (now + timedelta(minutes=45)).isoformat().replace("+00:00", "Z")
        assert _format_relative_time(future, now) == "in 45m"

    def test_hours_minutes_in_future(self):
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        future = (now + timedelta(hours=2, minutes=15)).isoformat().replace("+00:00", "Z")
        # When days=0 we include hours+minutes; when days>0 we drop minutes.
        assert _format_relative_time(future, now) == "in 2h 15m"

    def test_days_in_future_drops_minutes(self):
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        future = (now + timedelta(days=3, hours=4, minutes=20)).isoformat().replace("+00:00", "Z")
        assert _format_relative_time(future, now) == "in 3d 4h"

    def test_past_renders_with_ago_suffix(self):
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        past = (now - timedelta(hours=5, minutes=30)).isoformat().replace("+00:00", "Z")
        assert _format_relative_time(past, now) == "5h 30m ago"

    def test_seconds_under_a_minute(self):
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        very_recent = (now - timedelta(seconds=15)).isoformat().replace("+00:00", "Z")
        assert _format_relative_time(very_recent, now) == "15s ago"

    def test_invalid_iso_returns_invalid(self):
        assert _format_relative_time("not-a-timestamp") == "(invalid)"


# ---------------------------------------------------------------------------
# rc list --ephemeral table output
# ---------------------------------------------------------------------------

class TestListEphemeralTable:
    def test_empty_registry_friendly_message(self, runner):
        with patch("remote_compose.ephemeral.list_records", return_value=[]):
            result = runner.invoke(cli, ["list", "--ephemeral"])
        assert result.exit_code == 0
        assert "No ephemeral stacks" in result.output
        assert "PROJECT" not in result.output  # no header on empty

    def test_one_record_renders_header_and_row(self, runner):
        rec = _record(project="proj-a")
        with patch("remote_compose.ephemeral.list_records", return_value=[rec]):
            result = runner.invoke(cli, ["list", "--ephemeral"])
        assert result.exit_code == 0
        assert "PROJECT" in result.output
        assert "REGION" in result.output
        assert "TTL" in result.output
        assert "proj-a" in result.output
        assert "us-west-1" in result.output

    def test_multiple_records_each_as_row(self, runner):
        recs = [_record("a"), _record("b"), _record("c")]
        with patch("remote_compose.ephemeral.list_records", return_value=recs):
            result = runner.invoke(cli, ["list", "--ephemeral"])
        for p in ("a", "b", "c"):
            assert p in result.output
        # Header appears exactly once
        assert result.output.count("PROJECT") == 1

    def test_expired_stack_marked(self, runner):
        rec = _record("expired-app", expires_in=timedelta(hours=-1))
        with patch("remote_compose.ephemeral.list_records", return_value=[rec]):
            result = runner.invoke(cli, ["list", "--ephemeral"])
        assert "EXPIRED" in result.output

    def test_no_flag_defaults_to_ephemeral(self, runner):
        """`rc list` without --ephemeral does the same thing today —
        once non-ephemeral inventory is added (.44.24), this becomes a
        deliberate routing decision, but for now the bare command must
        work."""
        recs = [_record("a")]
        with patch("remote_compose.ephemeral.list_records", return_value=recs):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "a" in result.output


# ---------------------------------------------------------------------------
# rc list --ephemeral --json
# ---------------------------------------------------------------------------

class TestListEphemeralJson:
    def test_empty_registry_yields_empty_array(self, runner):
        with patch("remote_compose.ephemeral.list_records", return_value=[]):
            result = runner.invoke(cli, ["list", "--ephemeral", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_record_round_trips_with_ttl_seconds(self, runner):
        rec = _record("proj-a", expires_in=timedelta(hours=2))
        with patch("remote_compose.ephemeral.list_records", return_value=[rec]):
            result = runner.invoke(cli, ["list", "--ephemeral", "--json"])
        data = json.loads(result.output)
        assert len(data) == 1
        item = data[0]
        assert item["project"] == "proj-a"
        assert item["region"] == "us-west-1"
        assert item["aws_profile"] == "default"
        assert item["rc_yml_path"] == "/tmp/rc.yml"
        # TTL in the right ballpark — allow ±30s for test timing
        assert 7170 <= item["ttl_remaining_seconds"] <= 7230
        assert item["expired"] is False

    def test_expired_stack_flag_in_json(self, runner):
        rec = _record("old", expires_in=timedelta(hours=-1))
        with patch("remote_compose.ephemeral.list_records", return_value=[rec]):
            result = runner.invoke(cli, ["list", "--ephemeral", "--json"])
        data = json.loads(result.output)
        assert data[0]["expired"] is True
        assert data[0]["ttl_remaining_seconds"] < 0

    def test_jq_compatible_output(self, runner):
        """Ensure the JSON output is parseable by external tools — strict
        JSON, no trailing commas, no log lines mixed in."""
        recs = [_record("a"), _record("b")]
        with patch("remote_compose.ephemeral.list_records", return_value=recs):
            result = runner.invoke(cli, ["list", "--ephemeral", "--json"])
        # The OUTPUT should be a single JSON document. Click writes a
        # trailing newline via click.echo — that's fine for jq.
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
