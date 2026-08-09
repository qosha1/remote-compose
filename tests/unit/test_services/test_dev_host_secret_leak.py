"""rc-bd7 — the GitHub PAT must not reach any persisted artifact.

The original leak put one token in four places at once:

  1. EC2 user-data           — readable from the box's own IMDS, and off the
                               box with ec2:DescribeInstanceAttribute
  2. terraform.tfvars.json   — gzip+base64 inside user_data_base64
  3. terraform.tfstate       — same, and it outlives the instance
  4. .rc/dev-hosts.yml       — plaintext, in the user's working tree

These tests assert each surface stays clean, and that the token still reaches
the box by the one channel that is actually private (SSH). They are written
against the real rendered output rather than mocks, because the whole class of
bug is "a value ends up somewhere nobody looked".
"""

from __future__ import annotations

import base64
import gzip

import pytest

from remote_compose.dev_host.bootstrap import (
    GitSource,
    MultiGitSource,
    is_secret_env_key,
    split_env_lines,
)
from remote_compose.dev_host.service import _compress_user_data, _source_to_dict

pytestmark = pytest.mark.unit

TOKEN = "gho_TESTONLYnotarealtoken0123456789ab"
API_KEY = "sk-ant-TESTONLYnotarealkey"


def _sources():
    """One of each source type that accepts credentials."""
    return [
        GitSource(
            url="https://github.com/owner/repo.git",
            gh_token=TOKEN,
            extra_env={"ANTHROPIC_API_KEY": API_KEY, "TZ": "UTC"},
        ),
        MultiGitSource(
            repos=[{"url": "https://github.com/owner/repo.git"}],
            compose_filenames=["docker-compose.yml"],
            gh_token=TOKEN,
            extra_env={"ANTHROPIC_API_KEY": API_KEY, "TZ": "UTC"},
        ),
    ]


@pytest.mark.parametrize("src", _sources(), ids=["git", "multi-git"])
class TestSecretsNeverPersist:
    def test_absent_from_user_data(self, src):
        rendered = src.render_user_data()
        assert TOKEN not in rendered
        assert API_KEY not in rendered

    def test_absent_from_the_compressed_blob_terraform_stores(self, src):
        """user_data_base64 is what lands in tfvars.json and tfstate. Checking
        the compressed form too, because base64 of gzip hides a plain substring
        search — that is exactly why this leak went unnoticed."""
        blob = _compress_user_data(src.render_user_data())
        assert TOKEN not in blob
        inflated = gzip.decompress(base64.b64decode(blob)).decode()
        assert TOKEN not in inflated
        assert API_KEY not in inflated

    def test_absent_from_the_state_file_record(self, src):
        record = _source_to_dict(src)
        assert TOKEN not in str(record)
        assert API_KEY not in str(record)
        assert record["gh_token"] == ""
        assert record["extra_env"]["ANTHROPIC_API_KEY"] == ""

    def test_non_secret_config_survives_redaction(self, src):
        """Redaction must not eat ordinary settings — a destroy re-renders
        user-data from this record."""
        record = _source_to_dict(src)
        assert record["extra_env"]["TZ"] == "UTC"
        assert record["type"] in ("git", "multi-git")

    def test_still_delivered_over_ssh(self, src):
        """Closing the leak is only correct if the box still gets the value."""
        payload = src.secret_env_content()
        assert f"export GH_TOKEN='{TOKEN}'" in payload
        assert API_KEY in payload
        # Non-secrets are not duplicated onto the slower channel.
        assert "TZ" not in payload

    def test_bootstrap_blocks_on_delivery_before_cloning(self, src):
        """Without the wait, cloud-init would race the SSH delivery and fail
        the clone on a private repo."""
        rendered = src.render_user_data()
        assert "/tmp/rc-dev-secrets" in rendered
        secrets_at = rendered.index("/tmp/rc-dev-secrets")
        clone_at = rendered.index("git clone")
        assert secrets_at < clone_at, "secrets wait must precede the clone"


class TestRoundTrip:
    def test_redacted_record_still_rebuilds_a_usable_source(self):
        """`rc dev destroy` reconstructs the source from state to regenerate
        tfvars. That must keep working without the token — and must not
        reintroduce it."""
        from remote_compose.dev_host.bootstrap import source_from_dict

        original = GitSource(url="https://github.com/owner/repo.git", gh_token=TOKEN)
        rebuilt = source_from_dict(_source_to_dict(original))

        assert rebuilt.url == original.url
        assert rebuilt.gh_token == ""
        assert TOKEN not in rebuilt.render_user_data()
        # No token means no reason to wait for one on a rebuild.
        assert "/tmp/rc-dev-secrets" not in rebuilt.render_user_data()


class TestClassification:
    @pytest.mark.parametrize(
        "key",
        [
            "GH_TOKEN",
            "gh_token",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "DB_PASSWORD",
            "SOME_CREDENTIAL",
            "GITHUB_AUTH",
        ],
    )
    def test_credential_shaped_keys_are_secret(self, key):
        assert is_secret_env_key(key)

    @pytest.mark.parametrize("key", ["TZ", "LANG", "DEBUG", "PORT", "NODE_ENV"])
    def test_ordinary_keys_are_not(self, key):
        assert not is_secret_env_key(key)

    def test_gh_token_is_secret_regardless_of_extra_env(self):
        public, secret = split_env_lines("tok", {})
        assert public == []
        assert secret == ["export GH_TOKEN='tok'"]

    def test_empty_token_produces_no_line(self):
        public, secret = split_env_lines("", {"TZ": "UTC"})
        assert secret == []
        assert public == ["export TZ='UTC'"]


class TestLegacyStateFiles:
    """A state file written before this fix still holds live tokens.

    Closing the leak going forward does nothing for those, so rc redacts them
    on read (nothing re-persists or prints the value) and says once that only
    rotation actually fixes an already-leaked credential.
    """

    LEGACY = """
hosts:
  oldbox:
    name: oldbox
    instance_type: t4g.large
    region: us-west-2
    status: running
    source:
      type: git
      url: https://github.com/owner/repo
      ref: main
      gh_token: {token}
      extra_env:
        ANTHROPIC_API_KEY: {key}
        TZ: UTC
"""

    @pytest.fixture
    def legacy_state(self, tmp_path):
        from remote_compose.dev_host.service import DevHostService

        path = tmp_path / "dev-hosts.yml"
        path.write_text(self.LEGACY.format(token=TOKEN, key=API_KEY))
        # Class-level latch, so reset it or a prior test suppresses the warning.
        DevHostService._legacy_secret_warned = False
        return path, DevHostService(state_path=path)

    def test_secrets_are_redacted_on_load(self, legacy_state):
        _, svc = legacy_state
        hosts = svc._load_state()
        assert TOKEN not in str(hosts)
        assert API_KEY not in str(hosts)
        assert hosts["oldbox"]["source"]["gh_token"] == ""

    def test_non_secret_state_survives(self, legacy_state):
        _, svc = legacy_state
        hosts = svc._load_state()
        src = hosts["oldbox"]["source"]
        assert src["url"] == "https://github.com/owner/repo"
        assert src["extra_env"]["TZ"] == "UTC"
        assert hosts["oldbox"]["status"] == "running"

    def test_warns_once_naming_the_host_and_the_remedy(self, legacy_state, capsys):
        _, svc = legacy_state
        svc._load_state()
        err = capsys.readouterr().err
        assert "SECURITY" in err
        assert "oldbox" in err
        assert "github.com/settings/tokens" in err
        # The warning must not itself print the credential.
        assert TOKEN not in err
        assert API_KEY not in err

        svc._load_state()
        assert "SECURITY" not in capsys.readouterr().err

    def test_display_never_shows_a_legacy_token(self, legacy_state):
        from remote_compose.cli_commands.dev import _sanitized_source_dict

        _, svc = legacy_state
        raw = {
            "type": "git",
            "gh_token": TOKEN,
            "extra_env": {"ANTHROPIC_API_KEY": API_KEY, "TZ": "UTC"},
        }
        shown = _sanitized_source_dict(raw)
        assert TOKEN not in str(shown)
        assert API_KEY not in str(shown)
        assert shown["gh_token"] == "<redacted>"
        assert shown["extra_env"]["TZ"] == "UTC"

    def test_display_distinguishes_absent_from_redacted(self):
        """An empty value must not render as '<redacted>' — that would imply a
        credential is present when it is not."""
        from remote_compose.cli_commands.dev import _sanitized_source_dict

        shown = _sanitized_source_dict(
            {"gh_token": "", "extra_env": {"ANTHROPIC_API_KEY": ""}}
        )
        assert shown["gh_token"] == ""
        assert shown["extra_env"]["ANTHROPIC_API_KEY"] == ""
