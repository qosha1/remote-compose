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
