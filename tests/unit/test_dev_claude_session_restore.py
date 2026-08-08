"""Unit tests for the claude-session restore fix on `rc dev start`/`attach`.

Root cause: `rc dev stop` is a real EC2 power-off, not a suspend — it kills
the in-box tmux server that cloud-init's one-time `runcmd` used to launch
`claude {flags}` on first boot. `runcmd` never re-runs on later boots, so
before this fix a stopped-then-started box came back with no claude session
at all, and `rc dev attach`'s dead-session fallback launched a bare,
unflagged `claude` — silently dropping --dangerously-skip-permissions and
re-triggering the folder-trust prompt on every stop/start cycle.

Also covers --continue (resume the agent's actual conversation, not just a
fresh session, across a stop/start or Spot interruption) and its `|| claude
{flags}` fallback — load-bearing, not defensive filler: measured live,
`claude --continue` in interactive mode can exit 1 ("No deferred tool marker
found in the resumed session"), and a command that exits non-zero as a
detached tmux pane's sole process kills the pane, the window, and the whole
tmux server with it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from remote_compose.cli_commands import dev as dev_cli

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _claude_session_command — pure function, mirrors cloud-init's launch cmd
# ---------------------------------------------------------------------------


class TestClaudeSessionCommand:
    def test_git_source_with_skip_permissions(self):
        cmd = dev_cli._claude_session_command(
            {
                "type": "git",
                "url": "https://github.com/owner/repo.git",
                "skip_permissions": True,
            }
        )
        assert "--dangerously-skip-permissions" in cmd
        assert "cd /home/ec2-user/repo" in cmd

    def test_git_source_without_skip_permissions(self):
        cmd = dev_cli._claude_session_command(
            {
                "type": "git",
                "url": "https://github.com/owner/repo.git",
                "skip_permissions": False,
            }
        )
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd.rstrip().endswith("claude --continue || claude")

    def test_git_source_ssh_style_url(self):
        cmd = dev_cli._claude_session_command(
            {
                "type": "git",
                "url": "git@github.com:owner/repo.git",
                "skip_permissions": True,
            }
        )
        assert "cd /home/ec2-user/repo" in cmd

    def test_multi_git_source_with_skip_permissions(self):
        cmd = dev_cli._claude_session_command(
            {"type": "multi-git", "skip_permissions": True}
        )
        assert cmd == (
            "cd /home/ec2-user; "
            "claude --continue --dangerously-skip-permissions "
            "|| claude --dangerously-skip-permissions"
        )

    def test_multi_git_source_without_skip_permissions(self):
        cmd = dev_cli._claude_session_command(
            {"type": "multi-git", "skip_permissions": False}
        )
        assert cmd == "cd /home/ec2-user; claude --continue || claude"

    def test_source_missing_skip_permissions_key_defaults_off(self):
        # ImageSource/LocalSource/ScriptSource never carry this field at all.
        cmd = dev_cli._claude_session_command({"type": "image"})
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd == "cd /home/ec2-user; claude --continue || claude"

    def test_always_includes_continue_regardless_of_skip_permissions(self):
        # A stop/start or a Spot interruption stopping the box is only half
        # handled by the code on disk surviving — the relaunched agent needs
        # --continue to resume its actual conversation too.
        for skip_permissions in (True, False):
            cmd = dev_cli._claude_session_command(
                {"type": "multi-git", "skip_permissions": skip_permissions}
            )
            assert "--continue" in cmd

    def test_continue_has_a_fallback_to_bare_claude(self):
        # Load-bearing, not defensive filler — measured live, `claude
        # --continue` in interactive mode can exit 1 ("No deferred tool
        # marker found in the resumed session"), and a command that exits
        # non-zero as a detached tmux pane's sole process kills the pane,
        # the window, and the whole tmux server with it. Without a fallback
        # baked into the command itself, a failed --continue means `rc dev
        # attach` has nothing at all to attach to.
        for skip_permissions in (True, False):
            cmd = dev_cli._claude_session_command(
                {"type": "multi-git", "skip_permissions": skip_permissions}
            )
            assert " || claude" in cmd


# ---------------------------------------------------------------------------
# `rc dev attach` dead-session fallback must use the stored flags
# ---------------------------------------------------------------------------


class TestAttachFallbackUsesStoredFlags:
    def _make_record(self, skip_permissions):
        return MagicMock(
            public_ip="203.0.113.42",
            ssh_key_credential_id=1,
            source={"type": "multi-git", "skip_permissions": skip_permissions},
        )

    def _run_attach(self, record):
        service = MagicMock()
        service.get_host.return_value = record
        service.credential_service.get_credential.return_value = "cred"
        service.credential_service.get_ssh_keypair.return_value = (
            "fake-pem",
            "fake-pub",
        )

        runner = CliRunner()
        with (
            patch.object(dev_cli, "_build_service", return_value=service),
            patch("os.execvp") as execvp,
        ):
            result = runner.invoke(dev_cli.dev_group, ["attach", "alice"])
        assert result.exit_code == 0, result.output
        execvp.assert_called_once()
        _file, argv = execvp.call_args.args
        return argv[-1]  # the remote command string, last argv element

    def test_attach_fallback_includes_skip_permissions_when_provisioned(self):
        remote_cmd = self._run_attach(self._make_record(skip_permissions=True))
        assert "--dangerously-skip-permissions" in remote_cmd

    def test_attach_fallback_omits_flag_when_not_provisioned(self):
        remote_cmd = self._run_attach(self._make_record(skip_permissions=False))
        assert "--dangerously-skip-permissions" not in remote_cmd


# ---------------------------------------------------------------------------
# `rc dev start` must proactively restore the claude session
# ---------------------------------------------------------------------------


class TestStartRestoresClaudeSession:
    def _make_record(self, public_ip="203.0.113.42", skip_permissions=True):
        return MagicMock(
            public_ip=public_ip,
            ssh_key_credential_id=1,
            source={"type": "multi-git", "skip_permissions": skip_permissions},
        )

    def _service(self, record):
        service = MagicMock()
        service.get_host.return_value = record
        service.credential_service.get_credential.return_value = "cred"
        service.credential_service.get_ssh_keypair.return_value = (
            "fake-pem",
            "fake-pub",
        )
        return service

    def test_start_relaunches_session_with_stored_flags(self):
        record = self._make_record(skip_permissions=True)
        service = self._service(record)
        runner = CliRunner()
        with (
            patch.object(dev_cli, "_build_service", return_value=service),
            patch.object(
                dev_cli, "_wait_for_ssh_ready", return_value=True
            ) as wait_mock,
            patch.object(
                dev_cli, "_relaunch_claude_session", return_value=True
            ) as relaunch_mock,
        ):
            result = runner.invoke(dev_cli.dev_group, ["start", "alice"])

        assert result.exit_code == 0, result.output
        service.start_host.assert_called_once_with("alice")
        wait_mock.assert_called_once_with("203.0.113.42", "fake-pem")
        relaunch_mock.assert_called_once_with(
            "203.0.113.42", "fake-pem", {"type": "multi-git", "skip_permissions": True}
        )
        assert "claude tmux session restored" in result.output

    def test_start_warns_when_ssh_never_comes_up(self):
        record = self._make_record()
        service = self._service(record)
        runner = CliRunner()
        with (
            patch.object(dev_cli, "_build_service", return_value=service),
            patch.object(dev_cli, "_wait_for_ssh_ready", return_value=False),
            patch.object(dev_cli, "_relaunch_claude_session") as relaunch_mock,
        ):
            result = runner.invoke(dev_cli.dev_group, ["start", "alice"])

        assert result.exit_code == 0, result.output
        relaunch_mock.assert_not_called()
        assert "couldn't confirm" in result.output

    def test_start_skips_relaunch_when_no_public_ip_yet(self):
        record = self._make_record(public_ip=None)
        service = self._service(record)
        runner = CliRunner()
        with (
            patch.object(dev_cli, "_build_service", return_value=service),
            patch.object(dev_cli, "_wait_for_ssh_ready") as wait_mock,
        ):
            result = runner.invoke(dev_cli.dev_group, ["start", "alice"])

        assert result.exit_code == 0, result.output
        wait_mock.assert_not_called()
