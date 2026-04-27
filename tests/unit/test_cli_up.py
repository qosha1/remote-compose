"""Unit tests for the `rc up` one-shot command.

Real-AWS deploy verification is out of scope for these tests — the
deployment + secrets-push paths are mocked. The tests exercise the
orchestration: scaffold-when-missing, error-when-no-source, --ttl
acknowledgement, and v1-rejection.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli


COMPOSE_FIXTURE = textwrap.dedent("""
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
""")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def compose_file(tmp_path):
    p = tmp_path / "docker-compose.yml"
    p.write_text(COMPOSE_FIXTURE)
    return p


# ---------------------------------------------------------------------------
# Missing rc.yml + no --from-compose -> clear error
# ---------------------------------------------------------------------------

def test_missing_rcyml_without_from_compose_errors(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    result = runner.invoke(cli, ["-c", str(rc_yml), "up"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
    assert "--from-compose" in result.output


# ---------------------------------------------------------------------------
# Scaffold-then-deploy path
# ---------------------------------------------------------------------------

def test_scaffolds_rcyml_when_missing(runner, tmp_path, compose_file):
    rc_yml = tmp_path / "rc.yml"
    # Stub the deploy + secrets steps so we exercise just the scaffold.
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli_commands.up._secrets_push_v2", return_value=True):
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "up", "--from-compose", str(compose_file),
             "--region", "us-west-1", "--aws-profile", "default"],
        )
    assert result.exit_code == 0, result.output
    assert rc_yml.exists()
    body = rc_yml.read_text()
    assert "version: 2" in body
    assert "us-west-1" in body
    assert "aws_profile: default" in body


def test_existing_rcyml_skips_scaffold(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("version: 2\nproject: existing\n")
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True) as dispatch, \
         patch("remote_compose.cli_commands.up._secrets_push_v2", return_value=True):
        result = runner.invoke(cli, ["-c", str(rc_yml), "up"])
    assert result.exit_code == 0, result.output
    assert dispatch.called
    assert "Scaffolding" not in result.output
    # Pre-existing content untouched.
    assert rc_yml.read_text() == "version: 2\nproject: existing\n"


# ---------------------------------------------------------------------------
# v1 rejection
# ---------------------------------------------------------------------------

def test_v1_rcyml_rejected(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("cluster: legacy\nregion: us-west-2\n")
    # Returns False on v1 -> rc up should raise ClickException.
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=False), \
         patch("remote_compose.cli_commands.up._secrets_push_v2", return_value=False):
        result = runner.invoke(cli, ["-c", str(rc_yml), "up"])
    assert result.exit_code != 0
    assert "v2" in result.output.lower()


# ---------------------------------------------------------------------------
# --ttl acknowledgement (real enforcement is in rc-e5u.44.14)
# ---------------------------------------------------------------------------

def test_ttl_flag_is_acknowledged(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("version: 2\nproject: x\n")
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli_commands.up._secrets_push_v2", return_value=True):
        result = runner.invoke(cli, ["-c", str(rc_yml), "up", "--ttl", "4h"])
    assert result.exit_code == 0, result.output
    assert "ttl" in result.output.lower()
    assert "4h" in result.output


def test_no_ttl_no_ack_message(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("version: 2\nproject: x\n")
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli_commands.up._secrets_push_v2", return_value=True):
        result = runner.invoke(cli, ["-c", str(rc_yml), "up"])
    assert result.exit_code == 0, result.output
    # The "TTL acknowledged" line must NOT appear when --ttl wasn't passed
    assert "ttl" not in result.output.lower() or "rc-e5u.44.14" not in result.output


# ---------------------------------------------------------------------------
# Secrets push failure is non-fatal — user still gets a working stack to
# operate on, with a clear instruction to retry secrets push.
# ---------------------------------------------------------------------------

def test_secrets_push_failure_warns_but_succeeds(runner, tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text("version: 2\nproject: x\n")
    with patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True), \
         patch("remote_compose.cli_commands.up._secrets_push_v2",
               side_effect=RuntimeError("boto3 lol")):
        result = runner.invoke(cli, ["-c", str(rc_yml), "up"])
    assert result.exit_code == 0, result.output
    assert "warn" in result.output.lower()
    assert "rc secrets push" in result.output
