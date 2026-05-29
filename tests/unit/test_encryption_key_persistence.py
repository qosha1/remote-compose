"""Encryption key persists across CLI sessions (remote-compose-5d2).

Earlier behavior: when REMOTE_COMPOSE_ENCRYPTION_KEY env wasn't set,
``_bootstrap_django`` generated a new Fernet key in memory on every
invocation — silently rendering credentials encrypted in any prior
session unrecoverable.

Fix: persist the auto-generated key to ``<db_dir>/encryption_key``
(mode 0600), like secret_key already was. Env var still wins over the
file when set (centralized key-management use case).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _bootstrap(db_dir: Path):
    """Drive the encryption-key resolution path in isolation, returning
    the resolved key. Patches Django so the function reaches the key-
    handling block instead of returning early on already-configured."""
    from remote_compose import cli as cli_mod

    captured = {}

    def fake_configure(**kwargs):
        captured["encryption_key"] = kwargs.get("REMOTE_COMPOSE", {}).get(
            "ENCRYPTION_KEY"
        )

    fake_settings = MagicMock()
    fake_settings.configured = False
    fake_settings.configure = fake_configure

    config = {
        "project_name": "p",
        "cluster": "c",
        "region": "us-west-1",
        "compose_file": "docker-compose.yml",
    }

    with (
        patch("django.conf.settings", fake_settings),
        patch("django.setup"),
        patch("django.core.management.call_command"),
        patch("pathlib.Path.home", return_value=db_dir),
    ):
        cli_mod._bootstrap_django(config)
    return captured.get("encryption_key")


# ---------------------------------------------------------------------------
# Persistence behavior
# ---------------------------------------------------------------------------


class TestEncryptionKeyPersistence:
    def test_env_var_wins_over_file(self, tmp_path, monkeypatch):
        """REMOTE_COMPOSE_ENCRYPTION_KEY env var takes precedence."""
        monkeypatch.setenv(
            "REMOTE_COMPOSE_ENCRYPTION_KEY",
            "from-env-var-key=",
        )
        # Even with a stale on-disk file present, env wins.
        db_dir = tmp_path / ".remote-compose" / "p"
        db_dir.mkdir(parents=True)
        (db_dir / "encryption_key").write_text("from-disk-key=")
        (db_dir / "secret_key").write_text("test-secret")

        key = _bootstrap(tmp_path)
        assert key == "from-env-var-key="

    def test_file_persists_across_sessions(self, tmp_path, monkeypatch):
        """First call generates + writes; second call reads the same key."""
        monkeypatch.delenv("REMOTE_COMPOSE_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

        # Pre-create db_dir + secret_key so _bootstrap doesn't blow up
        # generating those (orthogonal to the encryption-key path).
        db_dir = tmp_path / ".remote-compose" / "p"
        db_dir.mkdir(parents=True)
        (db_dir / "secret_key").write_text("test-secret")

        first = _bootstrap(tmp_path)
        assert first
        # File was written with the same key.
        encryption_key_path = db_dir / "encryption_key"
        assert encryption_key_path.exists()
        assert encryption_key_path.read_text().strip() == first
        # Mode 0600 (owner read+write only).
        mode = encryption_key_path.stat().st_mode & 0o777
        assert mode == 0o600

        # Second invocation: same key.
        second = _bootstrap(tmp_path)
        assert second == first
