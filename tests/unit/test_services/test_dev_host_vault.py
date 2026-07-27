"""Where `rc dev up` gets its GitHub PAT, and where it must never end up.

Two things are under test here:

  1. remote_compose.dev_host.vault — the StartSimpli lookup. Its contract is
     "best effort, never fatal, never leaks": every failure mode has to come
     back as an empty result with a printable reason, because a developer with
     no vault access must still be able to provision a box.

  2. dev._resolve_gh_token — the precedence between an explicit --gh-token, the
     vault, and $GH_TOKEN.

Every fake token in this file is a made-up literal; nothing here reads a real
credential store.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


FAKE_TOKEN = "ghp_faketokenfortestsonly000000000000"


def _completed(stdout="", returncode=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


class TestConfiguredVaultEnvs:
    """Env slugs come from simpli's own config file — and only the slugs.

    The access credential lives in the same file at environments[slug].key.
    rc has no reason to read it (`simpli exchange` does that itself), so it
    must not appear in anything this function hands back.
    """

    def _write_config(self, tmp_path, monkeypatch, payload):
        import json

        monkeypatch.setenv("SIMPLI_CONFIG_DIR", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps(payload))

    def test_returns_slugs_not_keys(self, tmp_path, monkeypatch):
        from remote_compose.dev_host import vault

        self._write_config(
            tmp_path,
            monkeypatch,
            {
                "apiUrl": "https://api.example.test",
                "environments": {
                    "some-laptop": {"key": "sk_vault_secret_value"},
                    "other-laptop": {"key": "sk_another_secret"},
                },
            },
        )

        envs = vault.configured_vault_envs()

        assert envs == ["other-laptop", "some-laptop"]
        assert not any("secret" in e for e in envs)

    def test_missing_config_is_not_an_error(self, tmp_path, monkeypatch):
        from remote_compose.dev_host import vault

        monkeypatch.setenv("SIMPLI_CONFIG_DIR", str(tmp_path / "nope"))
        assert vault.configured_vault_envs() == []

    def test_corrupt_config_is_not_an_error(self, tmp_path, monkeypatch):
        from remote_compose.dev_host import vault

        monkeypatch.setenv("SIMPLI_CONFIG_DIR", str(tmp_path))
        (tmp_path / "config.json").write_text("{not json")
        assert vault.configured_vault_envs() == []

    def test_honors_xdg_config_home(self, tmp_path, monkeypatch):
        from remote_compose.dev_host import vault

        monkeypatch.delenv("SIMPLI_CONFIG_DIR", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert vault.simpli_config_path() == tmp_path / "simpli" / "config.json"


class TestResolveGhToken:
    def _run_with(self, secrets_json, returncode=0, envs=("dev-box",)):
        """Drive resolve_gh_token against a stubbed simpli CLI."""
        from remote_compose.dev_host import vault

        with (
            patch.object(vault.shutil, "which", return_value="/usr/bin/simpli"),
            patch(
                "subprocess.run", return_value=_completed(secrets_json, returncode)
            ) as run,
            patch.object(vault, "configured_vault_envs", return_value=list(envs)),
        ):
            return vault.resolve_gh_token(), run

    def test_returns_token_from_the_vault(self):
        result, _ = self._run_with('{"GH_TOKEN": "%s"}' % FAKE_TOKEN)

        assert result
        assert result.token == FAKE_TOKEN
        assert result.key == "GH_TOKEN"
        assert result.env == "dev-box"
        assert "dev-box" in result.origin and "GH_TOKEN" in result.origin

    def test_falls_back_through_the_candidate_key_names(self):
        # The debugg-ai vault has no purpose-named key today; the PAT that is
        # actually there is GITHUB_VERCEL_PAT. It must still be found.
        result, _ = self._run_with('{"GITHUB_VERCEL_PAT": "%s"}' % FAKE_TOKEN)

        assert result.token == FAKE_TOKEN
        assert result.key == "GITHUB_VERCEL_PAT"

    def test_purpose_named_key_wins_over_the_incidental_one(self):
        # So that adding RC_DEV_GH_TOKEN to the vault takes effect with no
        # code change, and outranks whatever happens to be lying around.
        result, _ = self._run_with(
            '{"GITHUB_VERCEL_PAT": "ghp_incidental", "RC_DEV_GH_TOKEN": "%s"}'
            % FAKE_TOKEN
        )

        assert result.key == "RC_DEV_GH_TOKEN"
        assert result.token == FAKE_TOKEN

    def test_exchange_never_writes_the_bundle_to_disk(self):
        # simpli's -o flag would drop the whole secret bundle into a file. Read
        # it on stdout instead so it only ever exists in this process.
        _, run = self._run_with('{"GH_TOKEN": "%s"}' % FAKE_TOKEN)

        argv = run.call_args.args[0]
        assert "-o" not in argv, f"exchange wrote secrets to a file: {argv}"
        assert argv[1:4] == ["exchange", "creds", "-e"]
        assert "-f" in argv and "json" in argv

    def test_missing_key_reports_what_it_looked_for(self):
        from remote_compose.dev_host import vault

        result, _ = self._run_with('{"BRIGHTDATA_API_KEY": "nope"}')

        assert not result
        for key in vault.GH_TOKEN_VAULT_KEYS:
            assert key in result.reason
        # The vault's own contents are not ours to advertise.
        assert "BRIGHTDATA_API_KEY" not in result.reason

    def test_blank_values_do_not_count_as_a_token(self):
        result, _ = self._run_with('{"GH_TOKEN": "   "}')
        assert not result


class TestVaultFailuresAreNeverFatal:
    """A developer with no simpli, no config, or no network still provisions.

    Each of these used to be a plausible place to raise; every one has to come
    back as a falsy result carrying a reason instead.
    """

    def test_simpli_not_installed(self):
        from remote_compose.dev_host import vault

        with patch.object(vault.shutil, "which", return_value=None):
            result = vault.resolve_gh_token(env_slug="dev-box")

        assert not result
        assert "not on PATH" in result.reason

    def test_no_environment_configured(self, tmp_path, monkeypatch):
        from remote_compose.dev_host import vault

        monkeypatch.setenv("SIMPLI_CONFIG_DIR", str(tmp_path))
        result = vault.resolve_gh_token()

        assert not result
        assert "--vault-env" in result.reason

    def test_ambiguous_environment_asks_rather_than_guessing(self):
        from remote_compose.dev_host import vault

        with patch.object(
            vault, "configured_vault_envs", return_value=["a-box", "b-box"]
        ):
            result = vault.resolve_gh_token()

        assert not result
        assert "--vault-env" in result.reason
        assert "a-box" in result.reason and "b-box" in result.reason

    def test_exchange_exits_nonzero(self):
        from remote_compose.dev_host import vault

        with (
            patch.object(vault.shutil, "which", return_value="/usr/bin/simpli"),
            patch("subprocess.run", return_value=_completed("", returncode=1)),
        ):
            result = vault.resolve_gh_token(env_slug="dev-box")

        assert not result
        assert "exited 1" in result.reason

    def test_exchange_times_out(self):
        import subprocess

        from remote_compose.dev_host import vault

        with (
            patch.object(vault.shutil, "which", return_value="/usr/bin/simpli"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="simpli", timeout=20),
            ),
        ):
            result = vault.resolve_gh_token(env_slug="dev-box", timeout=20)

        assert not result
        assert "timed out" in result.reason

    def test_exchange_returns_garbage(self):
        from remote_compose.dev_host import vault

        with (
            patch.object(vault.shutil, "which", return_value="/usr/bin/simpli"),
            patch("subprocess.run", return_value=_completed("<html>login</html>")),
        ):
            result = vault.resolve_gh_token(env_slug="dev-box")

        assert not result
        assert "not return JSON" in result.reason

    def test_exec_failure(self):
        from remote_compose.dev_host import vault

        with (
            patch.object(vault.shutil, "which", return_value="/usr/bin/simpli"),
            patch("subprocess.run", side_effect=OSError("exec format error")),
        ):
            result = vault.resolve_gh_token(env_slug="dev-box")

        assert not result
        assert "could not run simpli" in result.reason


class TestVaultNeverLeaks:
    def test_reason_never_quotes_the_exchange_output(self):
        # stdout of a successful exchange IS the secret bundle. If a failure
        # path ever folds it into the reason string it goes straight to the
        # terminal — so no path may echo it, even the ones where the output
        # "isn't valid anyway".
        from remote_compose.dev_host import vault

        poisoned = f"garbage {FAKE_TOKEN} not-json"
        with (
            patch.object(vault.shutil, "which", return_value="/usr/bin/simpli"),
            patch("subprocess.run", return_value=_completed(poisoned, returncode=2)),
        ):
            result = vault.resolve_gh_token(env_slug="dev-box")

        assert FAKE_TOKEN not in result.reason
        assert FAKE_TOKEN not in repr(result)

    def test_repr_redacts_the_token(self):
        # rc-h40: anything holding a live PAT gets printed eventually — by an
        # exception, a debugger, a stray click.echo. Redact at the type.
        from remote_compose.dev_host.vault import VaultLookup

        result = VaultLookup(token=FAKE_TOKEN, key="GH_TOKEN", env="dev-box")

        assert FAKE_TOKEN not in repr(result)
        assert "<redacted>" in repr(result)
        assert result.token == FAKE_TOKEN  # ...but still usable

    def test_origin_is_printable(self):
        from remote_compose.dev_host.vault import VaultLookup

        origin = VaultLookup(token=FAKE_TOKEN, key="GH_TOKEN", env="dev-box").origin

        assert FAKE_TOKEN not in origin
        assert origin == "StartSimpli vault (dev-box:GH_TOKEN)"


def _ctx(source="ENVIRONMENT"):
    """Fake click context reporting where --gh-token came from."""
    from click.core import ParameterSource

    ctx = MagicMock()
    ctx.get_parameter_source.return_value = getattr(ParameterSource, source)
    return ctx


def _source(url="https://github.com/owner/repo.git"):
    from remote_compose.dev_host.bootstrap import GitSource

    return GitSource(url=url, ref="main")


class TestGhTokenPrecedence:
    """--gh-token flag > vault > $GH_TOKEN."""

    def _resolve(
        self, ctx, gh_token, vault_result, no_vault=False, can_read=(True, "")
    ):
        from remote_compose.cli_commands import dev
        from remote_compose.dev_host import vault as vault_mod

        with (
            patch.object(vault_mod, "resolve_gh_token", return_value=vault_result),
            patch.object(vault_mod, "token_can_read", return_value=can_read),
        ):
            return dev._resolve_gh_token(ctx, _source(), gh_token, None, no_vault)

    def test_vault_beats_an_ambient_gh_token_env_var(self):
        from remote_compose.dev_host.vault import VaultLookup

        token, origin = self._resolve(
            _ctx("ENVIRONMENT"),
            "ghp_stale_ambient_export",
            VaultLookup(token=FAKE_TOKEN, key="GH_TOKEN", env="dev-box"),
        )

        assert token == FAKE_TOKEN
        assert "vault" in origin
        assert "ghp_stale_ambient_export" not in origin

    def test_explicit_flag_beats_the_vault(self):
        from remote_compose.dev_host.vault import VaultLookup

        token, origin = self._resolve(
            _ctx("COMMANDLINE"),
            FAKE_TOKEN,
            VaultLookup(token="ghp_vault_value", key="GH_TOKEN", env="dev-box"),
        )

        assert token == FAKE_TOKEN
        assert origin == "--gh-token flag"

    def test_env_var_used_when_the_vault_has_nothing(self):
        # Hard requirement: no simpli, no vault access -> still provisions.
        from remote_compose.dev_host.vault import VaultLookup

        token, origin = self._resolve(
            _ctx("ENVIRONMENT"),
            FAKE_TOKEN,
            VaultLookup(reason="simpli CLI not on PATH"),
        )

        assert token == FAKE_TOKEN
        assert "$GH_TOKEN" in origin
        assert "simpli CLI not on PATH" in origin
        assert FAKE_TOKEN not in origin

    def test_no_token_anywhere_is_not_an_error(self):
        from remote_compose.dev_host.vault import VaultLookup

        token, origin = self._resolve(
            _ctx("DEFAULT"), None, VaultLookup(reason="simpli CLI not on PATH")
        )

        assert token == ""
        assert "private clones will fail" in origin

    def test_no_vault_flag_skips_the_lookup_entirely(self):
        from remote_compose.cli_commands import dev
        from remote_compose.dev_host import vault as vault_mod

        with patch.object(vault_mod, "resolve_gh_token") as lookup:
            token, origin = dev._resolve_gh_token(
                _ctx("ENVIRONMENT"), _source(), FAKE_TOKEN, None, True
            )

        lookup.assert_not_called()
        assert token == FAKE_TOKEN
        assert "--no-vault" in origin

    def test_vault_token_that_cannot_see_the_repos_is_not_used(self):
        """The measured case, and the reason the preflight exists.

        The GitHub PAT in the debugg-ai dev vault is fine-grained and cannot
        see the debugg-ai repos `rc dev up` clones — verified against
        api.github.com. Preferring it over a working $GH_TOKEN on the strength
        of its name would swap a working provision for a clone that 403s inside
        cloud-init six minutes later, with nothing on the terminal to say why.
        """
        from remote_compose.dev_host.vault import VaultLookup

        token, origin = self._resolve(
            _ctx("ENVIRONMENT"),
            FAKE_TOKEN,
            VaultLookup(token="ghp_wrongscope", key="GITHUB_VERCEL_PAT", env="dev-box"),
            can_read=(False, "cannot read owner/repo (HTTP 404)"),
        )

        assert token == FAKE_TOKEN
        assert "$GH_TOKEN" in origin
        assert "cannot read owner/repo (HTTP 404)" in origin
        assert "ghp_wrongscope" not in origin

    def test_preflight_checks_every_repo_being_cloned(self):
        from remote_compose.cli_commands import dev
        from remote_compose.dev_host.bootstrap import MultiGitSource
        from remote_compose.dev_host.vault import VaultLookup
        from remote_compose.dev_host import vault as vault_mod

        source = MultiGitSource(
            repos=[
                {"url": "https://github.com/debugg-ai/debuggai-api"},
                {"url": "https://github.com/debugg-ai/react-web-app.git"},
                {"url": "git@github.com:debugg-ai/browser-mgr.git"},
                {"url": "https://gitlab.example.com/x/y.git"},  # not github: skipped
            ],
            compose_filenames=["x.yml"],
        )

        with (
            patch.object(
                vault_mod,
                "resolve_gh_token",
                return_value=VaultLookup(token=FAKE_TOKEN, key="GH_TOKEN", env="d"),
            ),
            patch.object(
                vault_mod, "token_can_read", return_value=(True, "")
            ) as can_read,
        ):
            dev._resolve_gh_token(_ctx("DEFAULT"), source, None, None, False)

        assert can_read.call_args.args[1] == [
            "debugg-ai/debuggai-api",
            "debugg-ai/react-web-app",
            "debugg-ai/browser-mgr",
        ]


class TestTokenCanRead:
    def test_unreachable_github_does_not_veto_the_token(self):
        # This machine's network says nothing about the box's. Refusing a
        # perfectly good credential because a laptop is offline would be a
        # self-inflicted outage.
        import urllib.error

        from remote_compose.dev_host.vault import token_can_read

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")):
            ok, reason = token_can_read(FAKE_TOKEN, ["owner/repo"])

        assert ok is True
        assert reason == ""

    def test_repo_the_token_cannot_see_is_rejected(self):
        import urllib.error

        from remote_compose.dev_host.vault import token_can_read

        # GitHub answers 404 rather than 403 — it won't confirm a repo exists
        # to a token that can't read it.
        err = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo", 404, "Not Found", {}, None
        )
        with patch("urllib.request.urlopen", side_effect=err):
            ok, reason = token_can_read(FAKE_TOKEN, ["owner/repo"])

        assert ok is False
        assert "owner/repo" in reason and "404" in reason

    def test_no_repos_to_check_is_a_pass(self):
        from remote_compose.dev_host.vault import token_can_read

        assert token_can_read(FAKE_TOKEN, []) == (True, "")

    def test_token_is_sent_as_a_bearer_and_never_in_the_url(self):
        from remote_compose.dev_host.vault import token_can_read

        with patch("urllib.request.urlopen") as urlopen:
            token_can_read(FAKE_TOKEN, ["owner/repo"])

        request = urlopen.call_args.args[0]
        assert FAKE_TOKEN not in request.full_url
        assert request.get_header("Authorization") == f"Bearer {FAKE_TOKEN}"


class TestGithubSlug:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://github.com/owner/repo/", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
            ("https://gitlab.example.com/owner/repo.git", ""),
            ("https://github.com/owner", ""),
        ],
    )
    def test_slug_extraction(self, url, expected):
        from remote_compose.dev_host.vault import github_slug

        assert github_slug(url) == expected
