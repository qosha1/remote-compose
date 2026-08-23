"""
Unit tests for dev_host_bootstrap source plugins.

TDD red phase for [rc dev 2.1] (rc-ejl). Tests assert the contract that
implementation in [rc dev 4.1] (rc-z7p) must satisfy.

Each source plugin produces cloud-init YAML for an EC2 dev-host bootstrap.
Plugins: GitSource (default for sentinel), ImageSource, LocalSource, ScriptSource.
"""

import pytest
import yaml

pytestmark = pytest.mark.unit


class TestGitSource:
    def test_git_source_defaults(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(url="https://github.com/owner/repo.git", ref="main")

        assert src.type == "git"
        assert src.url == "https://github.com/owner/repo.git"
        assert src.ref == "main"

    def test_render_user_data_emits_valid_yaml(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(url="https://github.com/owner/repo.git", ref="alice/feat-x")
        rendered = src.render_user_data()

        assert rendered.startswith("#cloud-config")
        # body after the #cloud-config marker must parse as YAML
        body = "\n".join(rendered.splitlines()[1:])
        parsed = yaml.safe_load(body)
        assert isinstance(parsed, dict)

    def test_render_user_data_clones_repo_at_branch(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(url="https://github.com/owner/sentinal.git", ref="alice/feat-x")
        rendered = src.render_user_data()

        # the bootstrap must clone the repo and check out the requested ref
        assert "git clone" in rendered
        assert "https://github.com/owner/sentinal.git" in rendered
        assert "alice/feat-x" in rendered

    def test_render_user_data_installs_docker_and_starts_compose(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(url="https://github.com/owner/sentinal.git", ref="main")
        rendered = src.render_user_data()

        # AL2023 docker install must run; docker compose must be brought up
        assert "docker" in rendered.lower()
        assert "compose" in rendered.lower()

    def test_render_user_data_omits_env_block_when_no_secrets(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        # No GH_TOKEN, no ANTHROPIC_API_KEY → no `path: /tmp/rc-dev-env-staging`
        # entry in write_files (the conditional `if [ -f ... ]` references in the
        # bootstrap script are fine — they're a no-op when the file doesn't exist).
        assert "path: /tmp/rc-dev-env-staging" not in rendered

    def test_render_user_data_with_secrets_writes_staging_file(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(
            url="https://github.com/owner/repo.git",
            extra_env={"FOO": "bar"},
        ).render_user_data()

        assert "path: /tmp/rc-dev-env-staging" in rendered

    def test_gh_token_never_reaches_user_data(self):
        """rc-bd7. user-data is readable from the box's own IMDS, off the box
        with ec2:DescribeInstanceAttribute, and terraform serializes it into
        tfvars.json and tfstate. The PAT goes over SSH instead."""
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(
            url="https://github.com/owner/repo.git", gh_token="ghp_secrettoken"
        )
        rendered = src.render_user_data()

        assert "ghp_secrettoken" not in rendered
        # ...but the box is told to wait for it, otherwise a private clone
        # would race the delivery.
        assert "/tmp/rc-dev-secrets" in rendered
        # ...and it IS in the payload the CLI hands over SSH.
        assert "ghp_secrettoken" in src.secret_env_content()
        assert "GH_TOKEN" in src.secret_env_content()

    def test_secret_extra_env_goes_over_ssh_and_plain_env_stays_inline(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(
            url="https://github.com/owner/repo.git",
            extra_env={"ANTHROPIC_API_KEY": "sk-ant-test", "OTHER_VAR": "hello"},
        )
        rendered = src.render_user_data()
        secrets = src.secret_env_content()

        # Credential-shaped key: value must not be in user-data at all.
        assert "sk-ant-test" not in rendered
        assert "sk-ant-test" in secrets
        # Ordinary config: no reason to delay it, stays in user-data.
        assert "OTHER_VAR" in rendered and "hello" in rendered
        assert "OTHER_VAR" not in secrets

    def test_no_secrets_means_no_boot_time_wait(self):
        """A token-less box must not sit through the secrets wait."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()
        assert "/tmp/rc-dev-secrets" not in rendered

    def test_claude_tmux_starter_in_runcmd(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        # tmux session for claude must be set up
        assert "rc-dev-start-claude.sh" in rendered
        assert "tmux" in rendered.lower()

    def test_claude_starts_without_skip_permissions_by_default(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        assert "--dangerously-skip-permissions" not in rendered

    def test_claude_starts_with_skip_permissions_when_set(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(
            url="https://github.com/owner/repo.git", skip_permissions=True
        ).render_user_data()

        assert "--dangerously-skip-permissions" in rendered

    def test_bd_beads_installed_in_runcmd(self):
        """Local Claude settings.json calls 'bd prime' on SessionStart —
        the in-box claude needs bd available or it errors at startup."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        # arch detection + tarball url + extract
        assert "gastownhall/beads/releases/download" in rendered
        assert "/usr/local/bin/bd" in rendered
        assert "uname -m" in rendered  # arch detect

    def test_gh_cli_installed_in_runcmd(self):
        """gh CLI is needed for 'gh pr', 'gh repo clone', etc. inside the box."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        assert "github.com/cli/cli/releases/download" in rendered
        assert "gh_" in rendered
        assert ".rpm" in rendered  # AL2023 uses dnf install <url>.rpm

    def test_dolt_installed_in_runcmd(self):
        """dolt backs beads' remote-sync ('bd dolt push'/'pull')."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        assert "dolthub/dolt/releases/download" in rendered
        assert "/usr/local/bin/dolt" in rendered

    def test_headroom_installed_in_runcmd(self):
        """headroom needs python>=3.10; AL2023 ships 3.9, so it must not rely
        on the system interpreter."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        assert "python3.12" in rendered
        assert "headroom-ai" in rendered
        assert "/usr/local/bin/headroom" in rendered

    def test_rtk_built_from_source_in_runcmd(self):
        """No prebuilt aarch64 rtk binary is glibc-compatible with AL2023
        (gnu build needs glibc>=2.39, AL2023 ships 2.34, no musl aarch64
        build exists) — must build from source via dnf's rust/cargo."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        assert "rtk-ai/rtk/archive" in rendered
        assert "cargo build --release" in rendered
        assert "/usr/local/bin/rtk" in rendered


class TestImageSource:
    def test_image_source_defaults(self):
        from remote_compose.dev_host.bootstrap import ImageSource

        src = ImageSource(image="ghcr.io/owner/app:latest")

        assert src.type == "image"
        assert src.image == "ghcr.io/owner/app:latest"

    def test_render_user_data_pulls_and_runs_image(self):
        from remote_compose.dev_host.bootstrap import ImageSource

        src = ImageSource(image="ghcr.io/owner/app:v1.2.3")
        rendered = src.render_user_data()

        assert rendered.startswith("#cloud-config")
        assert "docker pull" in rendered
        assert "ghcr.io/owner/app:v1.2.3" in rendered


class TestLocalSource:
    def test_local_source_defaults(self):
        from remote_compose.dev_host.bootstrap import LocalSource

        src = LocalSource(path="/some/local/path")

        assert src.type == "local"
        assert src.path == "/some/local/path"

    def test_render_user_data_prepares_target_dir(self):
        from remote_compose.dev_host.bootstrap import LocalSource

        src = LocalSource(path="/tmp/myapp")
        rendered = src.render_user_data()

        # cloud-init creates the target dir; rsync from laptop happens via
        # `rc dev push-local` after boot (out of cloud-init scope).
        assert rendered.startswith("#cloud-config")


class TestScriptSource:
    def test_script_source_defaults(self):
        from remote_compose.dev_host.bootstrap import ScriptSource

        src = ScriptSource(script="echo hello")

        assert src.type == "script"
        assert src.script == "echo hello"

    def test_render_user_data_embeds_script_verbatim(self):
        from remote_compose.dev_host.bootstrap import ScriptSource

        src = ScriptSource(script="echo hello && touch /tmp/marker")
        rendered = src.render_user_data()

        assert rendered.startswith("#cloud-config")
        assert "echo hello && touch /tmp/marker" in rendered


class TestMultiGitSource:
    def test_defaults(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        src = MultiGitSource(
            repos=[
                {"url": "https://github.com/owner/backend.git"},
                {"url": "https://github.com/owner/frontend.git"},
            ],
            compose_filenames=["docker-compose.full.yml"],
        )

        assert src.type == "multi-git"
        assert len(src.repos) == 2
        assert src.compose_filenames == ["docker-compose.full.yml"]
        assert src.gh_token == ""
        assert src.skip_permissions is False

    def test_render_clones_each_repo(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        src = MultiGitSource(
            repos=[
                {"url": "https://github.com/owner/backend.git", "ref": "main"},
                {"url": "https://github.com/owner/frontend.git", "ref": "alice/feat-x"},
            ],
            compose_filenames=["docker-compose.full.yml"],
        )
        rendered = src.render_user_data()

        assert rendered.startswith("#cloud-config")
        # both clones must appear
        assert "github.com/owner/backend.git" in rendered
        assert "github.com/owner/frontend.git" in rendered
        # branches preserved
        assert "alice/feat-x" in rendered
        # compose-file wait + apply present
        assert "docker-compose.full.yml" in rendered
        assert "docker-compose.full.yml" in rendered
        assert "docker compose -f" in rendered

    def test_render_uses_url_basename_as_target_dir(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        src = MultiGitSource(
            repos=[{"url": "https://github.com/qosha1/sentinal.git"}],
            compose_filenames=["x.yml"],
        )
        rendered = src.render_user_data()

        # default target dir = repo basename without .git
        assert "/home/ec2-user/sentinal" in rendered

    def test_render_with_gh_token_writes_env_staging(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        src = MultiGitSource(
            repos=[{"url": "https://github.com/owner/repo.git"}],
            compose_filenames=["x.yml"],
            gh_token="ghp_secret",
        )
        rendered = src.render_user_data()

        # rc-bd7: same guarantee as the single-repo source.
        assert "ghp_secret" not in rendered
        assert "/tmp/rc-dev-secrets" in rendered
        assert "ghp_secret" in src.secret_env_content()

    def test_skip_permissions_propagates_to_claude_command(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        rendered = MultiGitSource(
            repos=[{"url": "https://github.com/owner/repo.git"}],
            compose_filenames=["x.yml"],
            skip_permissions=True,
        ).render_user_data()

        assert "--dangerously-skip-permissions" in rendered

    def test_dolt_headroom_rtk_installed_in_runcmd(self):
        """Same box-tooling guarantee as the single-repo source: dolt (beads
        remote-sync), headroom (needs its own python3.12, not AL2023's 3.9),
        and rtk (built from source — no AL2023-compatible prebuilt binary)."""
        from remote_compose.dev_host.bootstrap import MultiGitSource

        rendered = MultiGitSource(
            repos=[{"url": "https://github.com/owner/repo.git"}],
            compose_filenames=["x.yml"],
        ).render_user_data()

        assert "dolthub/dolt/releases/download" in rendered
        assert "/usr/local/bin/dolt" in rendered
        assert "headroom-ai" in rendered
        assert "/usr/local/bin/headroom" in rendered
        assert "rtk-ai/rtk/archive" in rendered
        assert "/usr/local/bin/rtk" in rendered

    def test_multiple_compose_files_each_run_as_separate_project(self):
        """Each --compose file runs as its own `docker compose -p <basename>`
        project so service-name conflicts across repos don't collide."""
        from remote_compose.dev_host.bootstrap import MultiGitSource

        rendered = MultiGitSource(
            repos=[
                {"url": "https://github.com/owner/sentinal.git"},
                {"url": "https://github.com/owner/browser-mgr.git"},
            ],
            compose_filenames=[
                "docker-compose.full.yml",
                "docker-compose.browser-mgr.yml",
            ],
        ).render_user_data()

        # Both filenames must appear in the bootstrap (wait + up loops)
        assert "docker-compose.full.yml" in rendered
        assert "docker-compose.browser-mgr.yml" in rendered
        # Project naming logic must be present (basename-with-prefix-stripped)
        assert "docker compose -f" in rendered
        assert " -p " in rendered

    def test_legacy_compose_filename_kwarg_still_accepted(self):
        """Backwards-compat: old single-file kwarg migrates to the list."""
        from remote_compose.dev_host.bootstrap import MultiGitSource

        src = MultiGitSource(
            repos=[{"url": "https://github.com/owner/repo.git"}],
            compose_filename="legacy.yml",
        )
        assert src.compose_filenames == ["legacy.yml"]

    def test_yaml_round_trips_through_state(self):
        """source_from_dict should reconstruct a MultiGitSource from its dict form."""
        from remote_compose.dev_host.bootstrap import (
            MultiGitSource,
            source_from_dict,
        )

        original = MultiGitSource(
            repos=[
                {"url": "https://github.com/a/b.git", "ref": "main"},
                {"url": "https://github.com/c/d.git", "ref": "v2"},
            ],
            compose_filenames=["x.yml"],
        )
        from dataclasses import asdict

        restored = source_from_dict(asdict(original))
        assert isinstance(restored, MultiGitSource)
        assert len(restored.repos) == 2
        assert restored.repos[0]["url"] == "https://github.com/a/b.git"
        assert restored.compose_filenames == ["x.yml"]


class TestClaudeConfigTarball:
    """Unit tests for _build_claude_config_tarball — the local helper that
    packs only auth + minimal settings (NOT the 7GB of project history)."""

    def test_includes_claude_json(self, tmp_path):
        from remote_compose.cli_commands.dev import _build_claude_config_tarball
        import tarfile

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        json_path = tmp_path / ".claude.json"
        json_path.write_text('{"oauth": "token"}')

        tarball = _build_claude_config_tarball(claude_dir, json_path)

        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
        assert ".claude.json" in names

    def test_includes_settings_and_agents_when_present(self, tmp_path):
        from remote_compose.cli_commands.dev import _build_claude_config_tarball
        import tarfile

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}")
        (claude_dir / "CLAUDE.md").write_text("# global mem")
        (claude_dir / "agents").mkdir()
        (claude_dir / "agents" / "my-agent.md").write_text("---\nname: x\n---\nbody")
        json_path = tmp_path / ".claude.json"
        json_path.write_text("{}")

        tarball = _build_claude_config_tarball(claude_dir, json_path)

        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
        assert ".claude/settings.json" in names
        assert ".claude/CLAUDE.md" in names
        assert ".claude/agents" in names

    def test_excludes_projects_and_cache_dirs(self, tmp_path):
        """The 7GB anti-bloat assertion: do NOT pack projects/, backups/, cache/."""
        from remote_compose.cli_commands.dev import _build_claude_config_tarball
        import tarfile

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        for d in (
            "projects",
            "backups",
            "cache",
            "history.jsonl",
            "shell-snapshots",
            "telemetry",
            "statsig",
        ):
            p = claude_dir / d
            if d.endswith(".jsonl"):
                p.write_text("history")
            else:
                p.mkdir()
                (p / "junk.bin").write_bytes(b"x" * 1024)
        json_path = tmp_path / ".claude.json"
        json_path.write_text("{}")

        tarball = _build_claude_config_tarball(claude_dir, json_path)
        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()

        for excluded in (
            "projects",
            "backups",
            "cache",
            "history.jsonl",
            "shell-snapshots",
            "telemetry",
            "statsig",
        ):
            for n in names:
                assert (
                    excluded not in n
                ), f"tarball should not contain {excluded}, found {n}"

    def test_includes_hooks_dir_when_present(self, tmp_path):
        """Hooks copied to the box so local SessionStart/Stop hooks fire there
        too. Assumes hooks use $HOME paths (we fix the user's settings.json
        to be portable, see git history)."""
        from remote_compose.cli_commands.dev import _build_claude_config_tarball
        import tarfile

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        hooks = claude_dir / "hooks"
        hooks.mkdir()
        (hooks / "my_hook.py").write_text("#!/usr/bin/env python3\nprint('hi')\n")
        json_path = tmp_path / ".claude.json"
        json_path.write_text("{}")

        tarball = _build_claude_config_tarball(claude_dir, json_path)
        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
        assert ".claude/hooks" in names
        assert ".claude/hooks/my_hook.py" in names

    def test_includes_credentials_when_file_exists(self, tmp_path, monkeypatch):
        """When ~/.claude/.credentials.json exists (Linux path), it must be
        in the tarball — without it, in-box claude is 'Not logged in'."""
        from remote_compose.cli_commands.dev import _build_claude_config_tarball
        import tarfile

        # Simulate a Linux home with a credentials file
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(
            '{"claudeAiOauth":{"accessToken":"sk-x"}}'
        )
        json_path = tmp_path / ".claude.json"
        json_path.write_text("{}")

        tarball = _build_claude_config_tarball(claude_dir, json_path)
        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
        assert ".claude/.credentials.json" in names

    def test_handles_missing_claude_dir_gracefully(self, tmp_path):
        """If ~/.claude doesn't exist (fresh machine), still tarball whatever
        files do exist (e.g. just .claude.json)."""
        from remote_compose.cli_commands.dev import _build_claude_config_tarball
        import tarfile

        json_path = tmp_path / ".claude.json"
        json_path.write_text('{"x":1}')
        # No claude_dir — pass a path that doesn't exist
        tarball = _build_claude_config_tarball(tmp_path / "nonexistent", json_path)

        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
        assert ".claude.json" in names


class TestCopyClaudeConfigFixesHeadroomMcpPath:
    """_copy_claude_config must rewrite mcpServers.headroom.command in the
    copied ~/.claude.json to the box's actual headroom path.

    Root cause: the shipped .claude.json is a straight copy of whatever ran
    `rc dev up` — its headroom MCP entry carries THAT machine's absolute
    path (e.g. a laptop's /Users/<name>/.local/bin/headroom), which never
    exists on the box. Confirmed live: left unfixed, `claude mcp list`
    shows headroom as "Failed to connect", silently breaking
    `headroom wrap claude`'s compression-marker retrieval tool — the box
    still launches and routes through the proxy fine, but that one feature
    quietly does nothing.
    """

    def _copy_with_mocks(self, tmp_path):
        """Run _copy_claude_config against hermetic tmp_path sources (never
        the real machine's ~/.claude.json) and return the extraction
        command string sent over the second `subprocess.run` (SSH) call."""
        from unittest.mock import patch, MagicMock
        from remote_compose.cli_commands.dev import _copy_claude_config

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text('{"mcpServers": {"headroom": {"command": "x"}}}')

        with (
            # Avoid the real macOS-keychain/Linux-credentials-file subprocess
            # call inside _build_claude_config_tarball — irrelevant to what
            # this test checks, and would otherwise feed a MagicMock through
            # the mocked subprocess.run below into a real file write.
            patch(
                "remote_compose.cli_commands.dev._read_claude_credentials",
                return_value=None,
            ),
            patch("subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0)
            _copy_claude_config(
                "203.0.113.42",
                "fake-pem-contents",
                claude_dir=claude_dir,
                claude_json=claude_json,
            )

        # Second subprocess.run call is the SSH extract+chown+fix command.
        return run.call_args_list[1].args[0][-1]

    def test_extraction_command_rewrites_headroom_mcp_path(self, tmp_path):
        extract_cmd = self._copy_with_mocks(tmp_path)
        assert "/usr/local/bin/headroom" in extract_cmd
        assert "mcpServers" in extract_cmd
        # Must not hard-fail the whole copy if .claude.json is missing/odd.
        assert "2>/dev/null || true" in extract_cmd

    def test_fix_runs_before_cleanup(self, tmp_path):
        """The rm -f of the staged tarball must come after the JSON fix, not
        before — ordering matters since both are chained with && on one
        remote command."""
        extract_cmd = self._copy_with_mocks(tmp_path)
        assert extract_cmd.index("/usr/local/bin/headroom") < extract_cmd.index(
            "rm -f /tmp/rc-claude-cfg.tar.gz"
        )


class TestSanitizedSourceRepr:
    """rc-h40: stdout-printed source repr must not leak secret-bearing fields."""

    def test_redacts_gh_token(self):
        from remote_compose.cli_commands.dev import _sanitized_source_repr
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(url="https://github.com/x/y.git", gh_token="ghp_secret")
        out = _sanitized_source_repr(src)

        assert "ghp_secret" not in out
        assert "<redacted>" in out

    def test_redacts_extra_env_secret_keys(self):
        from remote_compose.cli_commands.dev import _sanitized_source_repr
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(
            url="https://github.com/x/y.git",
            extra_env={"ANTHROPIC_API_KEY": "sk-ant-leak", "MY_PORT": "8002"},
        )
        out = _sanitized_source_repr(src)

        assert "sk-ant-leak" not in out
        assert "MY_PORT" in out and "8002" in out  # non-secret env preserved

    def test_no_secrets_renders_normally(self):
        from remote_compose.cli_commands.dev import _sanitized_source_repr
        from remote_compose.dev_host.bootstrap import GitSource

        out = _sanitized_source_repr(
            GitSource(url="https://github.com/x/y.git", ref="main")
        )

        assert "https://github.com/x/y.git" in out
        assert "main" in out


class TestComposePortAutoDetect:
    """rc-5c0: compose host ports auto-extracted for SG --port default."""

    def test_simple_string_ports(self, tmp_path):
        from remote_compose.cli_commands.dev import _ports_from_compose

        cf = tmp_path / "docker-compose.yml"
        cf.write_text("""
services:
  django:
    ports:
      - "8002:8002"
      - "8003:8003/tcp"
  postgres:
    ports:
      - "5434"
""")
        assert _ports_from_compose([cf]) == [5434, 8002, 8003]

    def test_long_form_dict_ports(self, tmp_path):
        from remote_compose.cli_commands.dev import _ports_from_compose

        cf = tmp_path / "docker-compose.yml"
        cf.write_text("""
services:
  api:
    ports:
      - target: 8002
        published: 8012
        protocol: tcp
""")
        assert _ports_from_compose([cf]) == [8012]

    def test_follows_include_directive_one_level(self, tmp_path):
        from remote_compose.cli_commands.dev import _ports_from_compose

        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "local.yml").write_text("""
services:
  django:
    ports: ["8002:8002"]
""")
        cf = tmp_path / "docker-compose.full.yml"
        cf.write_text("""
include:
  - path: sub/local.yml
services:
  react:
    ports: ["3011:3011"]
""")
        assert _ports_from_compose([cf]) == [3011, 8002]

    def test_dedupes_across_multiple_compose_files(self, tmp_path):
        from remote_compose.cli_commands.dev import _ports_from_compose

        cf1 = tmp_path / "a.yml"
        cf1.write_text('services:\n  s1:\n    ports: ["8000:8000"]\n')
        cf2 = tmp_path / "b.yml"
        cf2.write_text('services:\n  s2:\n    ports: ["8000:8000", "9000:9000"]\n')
        assert _ports_from_compose([cf1, cf2]) == [8000, 9000]

    def test_handles_no_ports_gracefully(self, tmp_path):
        from remote_compose.cli_commands.dev import _ports_from_compose

        cf = tmp_path / "docker-compose.yml"
        cf.write_text("services:\n  worker:\n    image: alpine\n")
        assert _ports_from_compose([cf]) == []


class TestSourceAutodetect:
    def test_detect_from_git_repo_returns_gitsource(self, tmp_path, monkeypatch):
        """In a git repo cwd, factory returns GitSource with detected url+branch."""
        from remote_compose.dev_host.bootstrap import (
            GitSource,
            detect_source_from_cwd,
        )

        # set up a fake git repo
        repo = tmp_path / "fake-repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        import subprocess

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit",
                "--allow-empty",
                "-m",
                "init",
                "-q",
            ],
            cwd=repo,
            check=True,
        )

        detected = detect_source_from_cwd(repo)

        assert isinstance(detected, GitSource)
        assert detected.url == "https://github.com/owner/repo.git"
        assert detected.ref == "main"

    def test_detect_outside_git_repo_raises(self, tmp_path):
        from remote_compose.exceptions import ValidationError
        from remote_compose.dev_host.bootstrap import detect_source_from_cwd

        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()

        with pytest.raises(ValidationError):
            detect_source_from_cwd(non_repo)


class TestTmuxHardening:
    """The in-box agent session must survive a human attaching to it.

    AL2023 ships tmux 3.2a, which segfaults the whole tmux server in
    cmd_load_buffer_done -> tty_set_selection -> tty_putcode_ptr2 when a
    clipboard write (`load-buffer -w`, which the claude TUI does) reaches a
    client whose terminal advertises the Ms (OSC-52) capability. That is
    exactly what a normal terminal does, so attaching killed every session on
    the box and `rc dev attach` then had nothing to attach to.

    Measured on AL2023 aarch64 by coredump count:
      3.2a + xterm-256color / tmux-256color (have Ms) -> server dumped core
      3.2a + screen-256color (no Ms)                  -> survived
      3.2a + Ms stripped via terminal-overrides       -> survived
    `set-clipboard off` does NOT cover it — an explicit `load-buffer -w`
    bypasses that option — so the terminal-overrides line is load-bearing.
    """

    def _write_files(self, rendered):
        body = "\n".join(rendered.splitlines()[1:])
        return {f["path"]: f for f in yaml.safe_load(body)["write_files"]}

    def _both_sources(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_crash_protection_still_exists_as_a_fallback(self):
        """The Ms strip must remain reachable, but NOT unconditionally.

        This test used to assert .tmux.conf hardcoded `terminal-overrides
        ",*:Ms@"`. That stopped the 3.2a segfault but also permanently disabled
        copy-to-clipboard, because Ms *is* OSC-52 — sessions survived and
        nothing could be copied out of them. The strip now lives in the 3.2a
        safe-mode branch of rc-dev-start-claude.sh, used only when the tmux 3.5a
        build is unavailable. See TestClipboardWorksOnTheBox.
        """
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            start = files["/usr/local/bin/rc-dev-start-claude.sh"]["content"]
            assert (
                'terminal-overrides ",*:Ms@"' in start
            ), f"{src.type}: lost the 3.2a segfault protection entirely"
            conf = files["/home/ec2-user/.tmux.conf"]["content"]
            assert (
                'terminal-overrides ",*:Ms@"' not in conf
            ), f"{src.type}: strip is unconditional again — copy will be dead"

    def test_tmux_conf_survives_reattach_at_a_different_size(self):
        # A `new-session -d` session is pinned to its creation geometry;
        # attaching from a different-size terminal renders garbage without this.
        for src in self._both_sources():
            conf = self._write_files(src.render_user_data())[
                "/home/ec2-user/.tmux.conf"
            ]["content"]
            assert "window-size latest" in conf
            assert "aggressive-resize on" in conf

    def test_session_created_with_explicit_geometry(self):
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            start = files["/usr/local/bin/rc-dev-start-claude.sh"]["content"]
            assert "-x 220 -y 50" in start, f"{src.type}: session pinned to 80x24"

    def test_claude_symlink_resolves_real_target_not_itself(self):
        # Once ~/.local/bin is on PATH, `command -v claude` finds the symlink
        # being created, so a naive `ln -sf $(command -v claude)` self-links and
        # claude reports "missing or broken (symlink points to ...)".
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            start = files["/usr/local/bin/rc-dev-start-claude.sh"]["content"]
            assert (
                "/home/ec2-user/.local/bin/claude|" in start
            ), f"{src.type}: symlink target not guarded against self-reference"

    def test_trust_prompt_is_cleared(self):
        # `up` prints "attach lands ready". Without clearing the one-time
        # "Do you trust this folder?" dialog, attaching drops the user on a
        # modal prompt instead of a usable agent.
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            start = files["/usr/local/bin/rc-dev-start-claude.sh"]["content"]
            assert (
                'send-keys -t claude "1" Enter' in start
            ), f"{src.type}: trust prompt left on screen for the attaching user"

    def test_systemd_vt220_term_is_not_inherited(self):
        """cloud-init runs under systemd, whose default TERM is vt220.

        Left in place, the tmux SERVER stores TERM=vt220 in its global env and
        every pane process inherits it — so the claude TUI thinks it is driving
        a 1983 terminal and falls back to ACS line-drawing, which renders as
        stray letters and symbols all over the UI. Observed on a live box:
        the claude process had TERM=vt220 while tmux rendered the pane as
        tmux-256color. The two disagreeing is what garbles the display.
        """
        for src in self._both_sources():
            start = self._write_files(src.render_user_data())[
                "/usr/local/bin/rc-dev-start-claude.sh"
            ]["content"]
            assert "unset TERM" in start, f"{src.type}: systemd TERM leaks into tmux"

    def test_utf8_locale_is_set_for_the_agent(self):
        # AL2023 ships no LANG at all -> POSIX/C locale -> UTF-8 glyphs render
        # as mojibake.
        for src in self._both_sources():
            start = self._write_files(src.render_user_data())[
                "/usr/local/bin/rc-dev-start-claude.sh"
            ]["content"]
            assert "C.UTF-8" in start, f"{src.type}: no UTF-8 locale for the agent"

    def test_login_shells_get_a_utf8_locale(self):
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            assert (
                "/etc/profile.d/rc-dev-locale.sh" in files
            ), f"{src.type}: interactive ssh sessions still land in POSIX locale"


class TestCloudInitWait:
    """`rc dev up` returning is not the same as the box being usable."""

    def test_wait_helper_reports_done(self, monkeypatch):
        from remote_compose.cli_commands import dev

        class _Proc:
            stdout = "status: done"

        monkeypatch.setattr(dev.os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc(), raising=False)
        assert dev._wait_for_cloud_init("1.2.3.4", "PEM", timeout=30) is True

    def test_wait_helper_reports_error_status(self, monkeypatch):
        from remote_compose.cli_commands import dev

        class _Proc:
            stdout = "status: error"

        monkeypatch.setattr(dev.os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc(), raising=False)
        assert dev._wait_for_cloud_init("1.2.3.4", "PEM", timeout=30) is False


class TestWriteFilesOwnership:
    """write_files runs BEFORE the ec2-user account exists.

    Naming a not-yet-existent owner aborts the entire write_files module:
        ('write-files', OSError('Unknown user or group: getpwnam(): name not
         found: ec2-user'))
    cloud-init then reports status: error and NOTHING else runs — no clones, no
    env files, no compose. The box boots and looks alive while being completely
    unprovisioned, which is the worst possible failure mode. Files destined for
    ec2-user must land root-owned and rely on rc-dev-bootstrap.sh's
    `chown -R ec2-user:ec2-user /home/ec2-user`.
    """

    def _write_files(self, rendered):
        body = "\n".join(rendered.splitlines()[1:])
        return yaml.safe_load(body)["write_files"]

    def _all_sources(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_no_write_file_declares_a_non_root_owner(self):
        for src in self._all_sources():
            for entry in self._write_files(src.render_user_data()):
                owner = entry.get("owner")
                assert owner in (None, "root:root"), (
                    f"{src.type}: {entry['path']} declares owner={owner!r}; "
                    "that user does not exist yet at write_files time and will "
                    "abort cloud-init"
                )


class TestComposeFailureSurfacing:
    """A compose project that fails to start must not read as a healthy box.

    The bootstrap used to run `docker compose up ... || true`. A compose file
    that aborted instantly (missing env_file, bad YAML) was swallowed whole:
    cloud-init still reported 'done' and `rc dev up` still reported success,
    while half the services had never started. Observed in the wild — a
    two-project box came up with only the second project running and nothing
    anywhere said so.
    """

    def _bootstrap_script(self, rendered):
        body = "\n".join(rendered.splitlines()[1:])
        files = {f["path"]: f for f in yaml.safe_load(body)["write_files"]}
        return files["/usr/local/bin/rc-dev-bootstrap.sh"]["content"]

    def _all_sources(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_compose_failure_is_not_swallowed(self):
        for src in self._all_sources():
            script = self._bootstrap_script(src.render_user_data())
            assert (
                "up -d --build || true" not in script
            ), f"{src.type}: compose failure is swallowed by `|| true`"

    def test_bootstrap_records_per_project_exit_status(self):
        for src in self._all_sources():
            script = self._bootstrap_script(src.render_user_data())
            assert (
                ".rc-dev-compose-status" in script
            ), f"{src.type}: no status file for the CLI to read"


class TestFailedComposeProjectsParsing:
    """`rc dev up --wait` must refuse to claim success on a partial box."""

    def _patch_ssh(self, monkeypatch, stdout):
        from remote_compose.cli_commands import dev

        class _Proc:
            pass

        proc = _Proc()
        proc.stdout = stdout
        monkeypatch.setattr(dev.os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr("subprocess.run", lambda *a, **k: proc, raising=False)
        return dev

    def test_reports_failed_project(self, monkeypatch):
        dev = self._patch_ssh(monkeypatch, "full\t14\nbrowser-mgr\t0\n")
        assert dev._failed_compose_projects("1.2.3.4", "PEM") == [("full", 14)]

    def test_all_ok_reports_nothing(self, monkeypatch):
        dev = self._patch_ssh(monkeypatch, "full\t0\nbrowser-mgr\t0\n")
        assert dev._failed_compose_projects("1.2.3.4", "PEM") == []

    def test_missing_status_file_is_not_a_failure(self, monkeypatch):
        # Older boxes, or source types that run no compose at all.
        dev = self._patch_ssh(monkeypatch, "")
        assert dev._failed_compose_projects("1.2.3.4", "PEM") == []

    def test_garbage_lines_are_ignored(self, monkeypatch):
        dev = self._patch_ssh(monkeypatch, "nonsense\nfull\tNaN\nweb\t2\n")
        assert dev._failed_compose_projects("1.2.3.4", "PEM") == [("web", 2)]


class TestWaitForPorts:
    """`compose up -d` returning is not the same as the services being reachable.

    Containers report Up while Django is still migrating and Next.js is still
    compiling its first route, so --wait handed back a box whose ports answered
    nothing for another few minutes.
    """

    def test_returns_empty_when_all_ports_accept(self, monkeypatch):
        from remote_compose.cli_commands import dev

        class _Sock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("socket.create_connection", lambda *a, **k: _Sock())
        assert dev._wait_for_ports("1.2.3.4", [8012, 3011], timeout=5) == []

    def test_reports_ports_that_never_listen(self, monkeypatch):
        from remote_compose.cli_commands import dev

        def _refuse(*a, **k):
            raise OSError("refused")

        monkeypatch.setattr("socket.create_connection", _refuse)
        monkeypatch.setattr("time.sleep", lambda *a: None)
        assert dev._wait_for_ports("1.2.3.4", [9999], timeout=1, interval=0) == [9999]

    def test_stops_waiting_once_a_slow_port_comes_up(self, monkeypatch):
        from remote_compose.cli_commands import dev

        calls = {"n": 0}

        class _Sock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _flaky(addr, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("not yet")
            return _Sock()

        monkeypatch.setattr("socket.create_connection", _flaky)
        monkeypatch.setattr("time.sleep", lambda *a: None)
        assert dev._wait_for_ports("1.2.3.4", [3011], timeout=30, interval=0) == []
        assert calls["n"] == 3


class TestDestroyVerification:
    """`destroy` must not report success when it destroyed nothing.

    Terraform state lives in ./.rc/terraform-state/<name> — relative to the
    CURRENT WORKING DIRECTORY. Running destroy from a different dir than the one
    that ran `up` hands terraform an empty state: it destroys nothing and exits
    0. rc printed "✓ destroyed" over a box that was still running and still
    billing. Observed for real: `✓ destroyed 'rctest3'` followed by AWS
    reporting that instance as `running`.
    """

    def _fake_boto(self, monkeypatch, states):
        import sys as _sys
        import types

        reservations = [{"Instances": [{"State": {"Name": st}} for st in states]}]

        class _Client:
            def describe_instances(self, **kwargs):
                return {"Reservations": reservations}

        class _Session:
            def __init__(self, *a, **k):
                pass

            def client(self, *a, **k):
                return _Client()

        fake = types.ModuleType("boto3")
        fake.Session = _Session
        monkeypatch.setitem(_sys.modules, "boto3", fake)

    def test_running_instance_is_reported_as_alive(self, monkeypatch):
        from remote_compose.cli_commands import dev

        self._fake_boto(monkeypatch, ["running"])
        assert dev._live_instance_states("rctest3", "us-west-1", None) == {"running"}

    def test_terminated_instance_is_not_alive(self, monkeypatch):
        from remote_compose.cli_commands import dev

        self._fake_boto(monkeypatch, ["terminated", "shutting-down"])
        assert dev._live_instance_states("rctest3", "us-west-1", None) == set()

    def test_lookup_failure_does_not_block_the_command(self, monkeypatch):
        import sys as _sys
        import types

        from remote_compose.cli_commands import dev

        broken = types.ModuleType("boto3")

        def _boom(*a, **k):
            raise RuntimeError("no credentials")

        broken.Session = _boom
        monkeypatch.setitem(_sys.modules, "boto3", broken)
        assert dev._live_instance_states("x", "us-west-1", None) == set()

    def test_transient_running_then_terminated_is_not_a_failure(self, monkeypatch):
        # EC2 can report 'running' for a few seconds after terraform tore the
        # instance down. Checking once flagged successful teardowns as failures.
        from remote_compose.cli_commands import dev

        seq = [{"running"}, {"running"}, set()]
        calls = {"n": 0}

        def _states(*a, **k):
            i = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            return seq[i]

        monkeypatch.setattr(dev, "_live_instance_states", _states)
        monkeypatch.setattr("time.sleep", lambda *a: None)
        assert (
            dev._live_instance_states_settled("box", "us-west-1", None, interval=0)
            == set()
        )
        assert calls["n"] == 3

    def test_persistently_running_is_still_reported(self, monkeypatch):
        from remote_compose.cli_commands import dev

        monkeypatch.setattr(dev, "_live_instance_states", lambda *a, **k: {"running"})
        monkeypatch.setattr("time.sleep", lambda *a: None)
        assert dev._live_instance_states_settled(
            "box", "us-west-1", None, timeout=1, interval=0
        ) == {"running"}


class TestOrphanedEipRelease:
    """An unassociated Elastic IP bills forever and is invisible unless you look.

    terraform tags the dev host's address rc-dev-<name>-eip, so once the host is
    gone an address still carrying that tag is definitionally orphaned. Three
    such addresses were found in a live account (rc-dev-wjrep-eip and two
    rc-dev-triage-*-eip), left by hosts that no longer existed.
    """

    def _fake_ec2(self, monkeypatch, addresses):
        import sys as _sys
        import types

        released = []

        class _Client:
            def describe_addresses(self, **kwargs):
                return {"Addresses": addresses}

            def release_address(self, AllocationId=None):
                released.append(AllocationId)

        class _Session:
            def __init__(self, *a, **k):
                pass

            def client(self, *a, **k):
                return _Client()

        fake = types.ModuleType("boto3")
        fake.Session = _Session
        monkeypatch.setitem(_sys.modules, "boto3", fake)
        return released

    def test_releases_unassociated_tagged_eip(self, monkeypatch):
        from remote_compose.cli_commands import dev

        released = self._fake_ec2(
            monkeypatch,
            [{"AllocationId": "eipalloc-1", "PublicIp": "1.2.3.4"}],
        )
        freed = dev._release_orphaned_eips("wjrep", "us-west-1", None)
        assert freed == ["1.2.3.4"]
        assert released == ["eipalloc-1"]

    def test_leaves_attached_eip_alone(self, monkeypatch):
        from remote_compose.cli_commands import dev

        released = self._fake_ec2(
            monkeypatch,
            [
                {
                    "AllocationId": "eipalloc-1",
                    "PublicIp": "1.2.3.4",
                    "AssociationId": "eipassoc-9",
                }
            ],
        )
        assert dev._release_orphaned_eips("wjrep", "us-west-1", None) == []
        assert released == []

    def test_aws_error_does_not_fail_the_destroy(self, monkeypatch):
        import sys as _sys
        import types

        from remote_compose.cli_commands import dev

        broken = types.ModuleType("boto3")

        def _boom(*a, **k):
            raise RuntimeError("no credentials")

        broken.Session = _boom
        monkeypatch.setitem(_sys.modules, "boto3", broken)
        assert dev._release_orphaned_eips("x", "us-west-1", None) == []


class TestFirstBootSessionIsResumable:
    """The first-boot claude session must launch with --continue, with a
    fallback to bare `claude` if it fails.

    A box surviving a stop (deliberate `rc dev stop`, or a Spot interruption
    configured to stop rather than terminate) is only half the story if the
    relaunched agent has no memory of what it was doing — the code on disk
    survives either way, but without --continue every relaunch starts a
    brand new conversation.

    The `|| claude {flags}` fallback is load-bearing, not defensive filler.
    Measured live: `claude --continue` in interactive mode can exit 1 with
    "No deferred tool marker found in the resumed session" — seen on a box
    whose copied ~/.claude.json carried a stale deferred-tool reference from
    the local machine, not a case of "no prior session" (that case IS a safe
    no-op, verified separately). A command that exits non-zero as a detached
    tmux pane's sole process kills the pane, the window, and the whole tmux
    SERVER along with it — `rc dev attach` then has nothing to attach to at
    all, on a box that never even got the chance to use --continue for real.
    """

    def test_git_source_first_boot_uses_continue(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()
        assert "claude --continue" in rendered

    def test_git_source_first_boot_has_fallback(self):
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()
        assert "claude --continue" in rendered
        assert " || claude" in rendered

    def test_multi_git_source_first_boot_uses_continue(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        rendered = MultiGitSource(
            repos=[{"url": "https://github.com/owner/backend.git"}],
            compose_filenames=["docker-compose.full.yml"],
        ).render_user_data()
        assert "claude --continue" in rendered

    def test_multi_git_source_first_boot_has_fallback(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        rendered = MultiGitSource(
            repos=[{"url": "https://github.com/owner/backend.git"}],
            compose_filenames=["docker-compose.full.yml"],
        ).render_user_data()
        assert "claude --continue" in rendered
        assert " || claude" in rendered


class TestAttachForcesUtf8:
    """`rc dev attach` must not hand the user an ACS-garbled UI.

    tmux decides whether the CLIENT can do UTF-8 from its locale, and
    `ssh host '<cmd>'` is a non-login non-interactive shell, so /etc/profile.d
    never runs and LANG is empty. tmux then re-encodes the pane's box-drawing
    into ACS escapes, which render as streams of `qqqq` and stray `m`/`l`.
    Measured on a live box from the raw attach stream:

        tmux attach              -> 65 ACS escapes,   0 UTF-8 box chars
        LANG=C.UTF-8 tmux attach ->  0 ACS escapes, 634 UTF-8 box chars
        tmux -u attach           ->  0 ACS escapes, 634 UTF-8 box chars

    This is a client-side decision — fixing TERM/locale for the pane process on
    the box does NOT help.
    """

    def _attach_source(self):
        import inspect

        from remote_compose.cli_commands import dev

        return inspect.getsource(dev.dev_attach_cmd.callback)

    def test_attach_passes_dash_u(self):
        assert (
            "tmux -u attach" in self._attach_source()
        ), "attach does not force UTF-8 (-u); the agent UI will render as ACS"

    def test_fallback_new_session_also_forces_utf8(self):
        assert (
            "tmux -u new-session" in self._attach_source()
        ), "fallback session does not force UTF-8"

    def test_attach_exports_a_utf8_locale(self):
        assert (
            "C.UTF-8" in self._attach_source()
        ), "attach does not export a UTF-8 locale"


class TestClipboardWorksOnTheBox:
    """Copy-to-clipboard must actually work, not be traded away for stability.

    AL2023's tmux 3.2a segfaults the whole server in tty_set_selection when a
    clipboard write reaches a client whose terminal advertises Ms (OSC-52). The
    only 3.2a workaround is stripping Ms — which is precisely the capability
    that makes copy work over SSH. Sessions survived but nothing could be copied
    out of them, which is not an acceptable resting state.

    So the bootstrap builds tmux 3.5a (no such bug) and the clipboard policy is
    chosen from the version actually present, falling back to the Ms-stripping
    safe mode only if that build failed.
    """

    def _files(self, src):
        body = "\n".join(src.render_user_data().splitlines()[1:])
        doc = yaml.safe_load(body)
        return {f["path"]: f["content"] for f in doc["write_files"]}, doc

    def _both(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_builds_a_tmux_without_the_segfault(self):
        for src in self._both():
            _, doc = self._files(src)
            runcmd = "\n".join(str(x) for x in doc["runcmd"])
            assert "tmux-3.5a.tar.gz" in runcmd, f"{src.type}: no fixed tmux built"

    def test_conf_does_not_hardcode_the_ms_strip(self):
        # Hardcoding it means copy is permanently dead even on a good tmux.
        for src in self._both():
            files, _ = self._files(src)
            conf = files["/home/ec2-user/.tmux.conf"]
            assert (
                'terminal-overrides ",*:Ms@"' not in conf
            ), f"{src.type}: .tmux.conf unconditionally disables copy"
            assert "source-file -q ~/.tmux.clipboard.conf" in conf

    def test_clipboard_enabled_when_tmux_is_new_enough(self):
        for src in self._both():
            files, _ = self._files(src)
            start = files["/usr/local/bin/rc-dev-start-claude.sh"]
            assert "set -g set-clipboard on" in start, f"{src.type}: never enables copy"

    def test_falls_back_to_safe_mode_if_the_build_failed(self):
        # A failed build must not leave a box whose sessions segfault on copy.
        for src in self._both():
            files, _ = self._files(src)
            start = files["/usr/local/bin/rc-dev-start-claude.sh"]
            assert (
                'terminal-overrides ",*:Ms@"' in start
            ), f"{src.type}: no 3.2a safe-mode fallback"

    def test_mouse_drag_reaches_the_system_clipboard(self):
        for src in self._both():
            files, _ = self._files(src)
            assert (
                "copy-pipe-and-cancel" in files["/home/ec2-user/.tmux.conf"]
            ), f"{src.type}: drag selects into tmux's buffer only"


class TestClaudeSessionSurvivesAnyReboot:
    """The claude tmux session must come back on EVERY boot, not just the
    first one and not just a `rc dev stop`/`start` cycle.

    Root cause, found live on debugg-dev-3: cloud-init's `runcmd` only ever
    fires once, tied to the instance's ID, so a plain one-time
    `sudo -u ec2-user rc-dev-start-claude.sh` call in runcmd never re-runs on
    a later boot. `rc dev start`'s own explicit relaunch
    (_relaunch_claude_session) covers a deliberate stop/start, but that path
    only fires when something actually calls `rc dev start` — confirmed live
    this is not guaranteed: a persistent Spot Instance can stop AND resume
    entirely on AWS's own initiative (journalctl showed a clean ACPI "Power
    key pressed short" shutdown followed by a fresh boot about a minute
    later, with no `rc dev start` in the picture at all). Docker's
    containers already self-heal from this via restart:unless-stopped; the
    claude session had no equivalent until this systemd unit.
    """

    def _write_files(self, rendered):
        body = "\n".join(rendered.splitlines()[1:])
        return {f["path"]: f for f in yaml.safe_load(body)["write_files"]}

    def _runcmd(self, rendered):
        body = "\n".join(rendered.splitlines()[1:])
        return "\n".join(str(x) for x in yaml.safe_load(body)["runcmd"])

    def _both_sources(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_systemd_unit_is_written(self):
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            assert "/etc/systemd/system/rc-dev-claude.service" in files, (
                f"{src.type}: no boot-time relaunch unit — claude only ever "
                "starts once, on first boot"
            )

    def test_systemd_unit_runs_the_same_start_script(self):
        for src in self._both_sources():
            unit = self._write_files(src.render_user_data())[
                "/etc/systemd/system/rc-dev-claude.service"
            ]["content"]
            assert "ExecStart=/usr/local/bin/rc-dev-start-claude.sh" in unit
            assert "User=ec2-user" in unit
            assert "WantedBy=multi-user.target" in unit, (
                f"{src.type}: unit not registered to start on boot — "
                "enabling it would be a no-op"
            )

    def test_unit_is_enabled_and_started_in_runcmd(self):
        for src in self._both_sources():
            runcmd = self._runcmd(src.render_user_data())
            assert "systemctl enable --now rc-dev-claude.service" in runcmd, (
                f"{src.type}: unit written but never enabled — dead file, "
                "no different from not having it"
            )

    def test_old_one_time_direct_call_is_gone(self):
        # A leftover `sudo -u ec2-user rc-dev-start-claude.sh` alongside the
        # new systemd unit would double-launch (kill+recreate) on every
        # first boot — harmless given the script's own kill-session guard,
        # but it means two mechanisms are responsible for the same thing
        # instead of one.
        for src in self._both_sources():
            runcmd = self._runcmd(src.render_user_data())
            assert (
                "sudo -u ec2-user /usr/local/bin/rc-dev-start-claude.sh" not in runcmd
            )


class TestSwapfile:
    """A box with no swap goes UNREACHABLE rather than degrading.

    A t4g.large running the full stack sits at ~90% memory and AL2023 ships no
    swap. Under pressure the kernel thrashes in reclaim instead of OOM-killing
    anything, so sshd can never fork: TCP is accepted on :22 but the banner
    never arrives, while AWS still reports status checks ok and healthy CPU
    credits. Observed live, with ZERO OOM kills in the journal — which is what
    makes it so misleading. Swap turns "unreachable" into "slow".
    """

    def _runcmd(self, src):
        body = "\n".join(src.render_user_data().splitlines()[1:])
        return "\n".join(str(x) for x in yaml.safe_load(body)["runcmd"])

    def _both(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_swap_is_provisioned(self):
        for src in self._both():
            r = self._runcmd(src)
            assert "/swapfile" in r and "swapon" in r, f"{src.type}: no swap configured"

    def test_swap_survives_reboot(self):
        for src in self._both():
            assert "/etc/fstab" in self._runcmd(
                src
            ), f"{src.type}: swap not persisted, lost on reboot"

    def test_swap_is_set_up_before_the_image_builds(self):
        # The Playwright/Chromium build is the memory spike that needs it.
        for src in self._both():
            r = self._runcmd(src)
            assert r.index("/swapfile") < r.index(
                "rc-dev-bootstrap.sh"
            ), f"{src.type}: swap configured after the builds it protects"


class TestUserDataFitsEc2Limit:
    """EC2 caps user-data at 16 KiB and the multi-git blob crossed it.

    At 16,442 bytes provisioning failed outright and terraform rolled the box
    back. The surfaced "error" was the truncated blob itself, which reads like
    nothing in particular — so a box that simply refused to build was the only
    symptom. Every comment added to cloud-init pushed toward that cliff.

    cloud-init inflates gzip-compressed user-data itself, so compressing takes
    the same payload to ~8 KiB and turns a tripwire into real headroom.
    """

    EC2_USER_DATA_LIMIT = 16384

    def _realistic_multigit(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        return MultiGitSource(
            repos=[
                {
                    "url": "https://github.com/debugg-ai/debuggai-api",
                    "target": "sentinal",
                },
                {
                    "url": "https://github.com/debugg-ai/react-web-app",
                    "target": "react-web-app",
                },
                {
                    "url": "https://github.com/debugg-ai/browser-mgr",
                    "target": "browser-mgr",
                },
            ],
            compose_filenames=[
                "docker-compose.full.yml",
                "docker-compose.browser-mgr.yml",
            ],
        )

    def test_compressed_user_data_fits_the_limit(self):
        from remote_compose.dev_host.service import _compress_user_data

        blob = _compress_user_data(self._realistic_multigit().render_user_data())
        assert len(blob) < self.EC2_USER_DATA_LIMIT, (
            f"compressed user-data is {len(blob)} bytes — over EC2's "
            f"{self.EC2_USER_DATA_LIMIT} cap; provisioning will roll back"
        )

    def test_compression_is_deterministic(self):
        # Non-deterministic bytes (gzip mtime) would make every terraform plan
        # show a user-data diff and replace the instance.
        from remote_compose.dev_host.service import _compress_user_data

        raw = self._realistic_multigit().render_user_data()
        assert _compress_user_data(raw) == _compress_user_data(raw)

    def test_compressed_blob_is_gzip(self):
        # cloud-init sniffs the gzip magic bytes to know it must inflate.
        import base64

        from remote_compose.dev_host.service import _compress_user_data

        raw = base64.b64decode(_compress_user_data("#cloud-config\n"))
        assert raw[:2] == b"\x1f\x8b", "not gzip — cloud-init will not inflate it"

    def test_empty_user_data_stays_empty(self):
        from remote_compose.dev_host.service import _compress_user_data

        assert _compress_user_data("") == ""


class TestNoTokenPersistedOnDisk:
    """The GitHub PAT must not survive in .git/config.

    Cloning with the token in the URL leaves a live gho_ credential in
    .git/config for every repo on the box, and tools copy it onward — beads
    lifts the remote URL verbatim into .beads/config.yaml, so the token lands in
    a second file nobody thinks to check. Found exactly that on two live boxes,
    across all three repos.

    Verified on a box that resetting origin + answering git auth from $GH_TOKEN
    keeps fetch AND push working, so this costs no functionality.
    """

    def _bootstrap(self, src):
        body = "\n".join(src.render_user_data().splitlines()[1:])
        files = {f["path"]: f["content"] for f in yaml.safe_load(body)["write_files"]}
        return files["/usr/local/bin/rc-dev-bootstrap.sh"]

    def _both(self):
        from remote_compose.dev_host.bootstrap import GitSource, MultiGitSource

        return [
            GitSource(url="https://github.com/owner/repo.git", ref="main"),
            MultiGitSource(
                repos=[{"url": "https://github.com/owner/backend.git"}],
                compose_filenames=["docker-compose.full.yml"],
            ),
        ]

    def test_origin_is_reset_to_a_clean_url(self):
        for src in self._both():
            assert "remote set-url origin" in self._bootstrap(
                src
            ), f"{src.type}: tokenized clone URL left in .git/config"

    def test_auth_comes_from_the_environment(self):
        for src in self._both():
            boot = self._bootstrap(src)
            assert "credential.helper" in boot, f"{src.type}: no credential helper"
            assert (
                "password=$GH_TOKEN" in boot
            ), f"{src.type}: helper does not read the token from the env"

    def test_helper_does_not_depend_on_the_callers_environment(self):
        """git runs the helper as a fresh child; the caller may have no GH_TOKEN.

        The in-box agent's tmux server is started by cloud-init, which HAS the
        token in its environment. Anything that later recreates that session
        outside cloud-init — a human, a repair script — produces a session with
        no GH_TOKEN, and every subsequent `git push` then fails with no
        credential while `git config` still claims to be wired for it. Observed
        exactly that on a live box: the agent reported "no GitHub credential
        reachable from this session" with a correctly-configured helper.

        Sourcing the 0600 ~/.rc-dev-env makes auth independent of who started
        what. (PR #43 delivers the token to that same file over SSH, so this
        holds for the vault path too.)
        """
        for src in self._both():
            boot = self._bootstrap(src)
            assert ". $HOME/.rc-dev-env" in boot, (
                f"{src.type}: helper trusts the caller's env; a session started "
                "outside cloud-init will be unable to push"
            )

    def test_helper_does_not_bake_the_token_at_render_time(self):
        # The helper must reference $GH_TOKEN literally; rendering it with a
        # concrete value would put the secret straight back into user-data.
        from remote_compose.dev_host.bootstrap import MultiGitSource

        src = MultiGitSource(
            repos=[{"url": "https://github.com/owner/backend.git"}],
            compose_filenames=["docker-compose.full.yml"],
            gh_token="gho_supersecretvalue",
        )
        boot = self._bootstrap(src)
        assert (
            "gho_supersecretvalue" not in boot.split("credential.helper")[1][:200]
        ), "credential helper has a literal token baked into it"
