"""
Unit tests for dev_host_bootstrap source plugins.

TDD red phase for [rc dev 2.1] (rc-ejl). Tests assert the contract that
implementation in [rc dev 4.1] (rc-z7p) must satisfy.

Each source plugin produces cloud-init YAML for an EC2 dev-host bootstrap.
Plugins: GitSource (default for sentinel), ImageSource, LocalSource, ScriptSource.
"""

from pathlib import Path

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
            compose_filenames=["docker-compose.full.yml", "docker-compose.browser-mgr.yml"],
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
        json_path.write_text('{}')

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
        for d in ("projects", "backups", "cache", "history.jsonl",
                  "shell-snapshots", "telemetry", "statsig"):
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

        for excluded in ("projects", "backups", "cache", "history.jsonl",
                         "shell-snapshots", "telemetry", "statsig"):
            for n in names:
                assert excluded not in n, f"tarball should not contain {excluded}, found {n}"

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
        (claude_dir / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"sk-x"}}')
        json_path = tmp_path / ".claude.json"
        json_path.write_text('{}')

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
            ["git", "commit", "--allow-empty", "-m", "init", "-q"],
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
