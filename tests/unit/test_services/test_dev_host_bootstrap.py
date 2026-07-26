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

    def test_render_user_data_with_gh_token_writes_env_staging(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(
            url="https://github.com/owner/repo.git", gh_token="ghp_secrettoken"
        )
        rendered = src.render_user_data()

        # gh_token must surface in the staged env file (so bootstrap can use it)
        assert "/tmp/rc-dev-env-staging" in rendered
        assert "GH_TOKEN" in rendered
        assert "ghp_secrettoken" in rendered

    def test_render_user_data_with_extra_env_includes_each_key(self):
        from remote_compose.dev_host.bootstrap import GitSource

        src = GitSource(
            url="https://github.com/owner/repo.git",
            extra_env={"ANTHROPIC_API_KEY": "sk-ant-test", "OTHER_VAR": "hello"},
        )
        rendered = src.render_user_data()

        assert "ANTHROPIC_API_KEY" in rendered
        assert "sk-ant-test" in rendered
        assert "OTHER_VAR" in rendered
        assert "hello" in rendered

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
        assert "steveyegge/beads/releases/download" in rendered
        assert "/usr/local/bin/bd" in rendered
        assert "uname -m" in rendered  # arch detect

    def test_gh_cli_installed_in_runcmd(self):
        """gh CLI is needed for 'gh pr', 'gh repo clone', etc. inside the box."""
        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(url="https://github.com/owner/repo.git").render_user_data()

        assert "github.com/cli/cli/releases/download" in rendered
        assert "gh_" in rendered
        assert ".rpm" in rendered  # AL2023 uses dnf install <url>.rpm


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

        assert "path: /tmp/rc-dev-env-staging" in rendered
        assert "ghp_secret" in rendered

    def test_skip_permissions_propagates_to_claude_command(self):
        from remote_compose.dev_host.bootstrap import MultiGitSource

        rendered = MultiGitSource(
            repos=[{"url": "https://github.com/owner/repo.git"}],
            compose_filenames=["x.yml"],
            skip_permissions=True,
        ).render_user_data()

        assert "--dangerously-skip-permissions" in rendered

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

    def test_tmux_conf_strips_ms_capability(self):
        for src in self._both_sources():
            files = self._write_files(src.render_user_data())
            assert "/home/ec2-user/.tmux.conf" in files, f"{src.type}: no tmux.conf"
            conf = files["/home/ec2-user/.tmux.conf"]["content"]
            assert 'terminal-overrides ",*:Ms@"' in conf, f"{src.type}: Ms not stripped"

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
