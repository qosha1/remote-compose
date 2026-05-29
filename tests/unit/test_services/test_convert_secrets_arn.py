"""Tests for ComposeToECSConverter._convert_secrets ARN generation
(remote-compose-9yo).

Earlier behavior wrote literal ``arn:aws:secretsmanager:REGION:ACCOUNT:
secret:<name>`` placeholders that ECS register-task-definition rejects
at deploy time. The fix: the converter takes account_id at construction,
_convert_secrets takes region per-call, and an explicit
ComposeConversionError fires when either is missing.
"""

from __future__ import annotations

import pytest

from remote_compose.exceptions import ComposeConversionError
from remote_compose.services.compose_converter import ComposeToECSConverter

# ---------------------------------------------------------------------------
# Happy path: account_id + region produce a real ARN
# ---------------------------------------------------------------------------


class TestSecretsArnGeneration:
    def test_dict_secret_produces_full_arn(self):
        c = ComposeToECSConverter(account_id="123456789012")
        out = c._convert_secrets(
            [{"source": "db-password", "target": "DB_PASSWORD"}],
            region="us-west-2",
        )
        assert out == [
            {
                "name": "DB_PASSWORD",  # upper + dashes-to-underscores
                "valueFrom": (
                    "arn:aws:secretsmanager:us-west-2:123456789012:"
                    "secret:db-password"
                ),
            }
        ]

    def test_secret_name_falls_back_to_name_field(self):
        # Compose accepts both ``source: foo`` and ``name: foo`` shapes.
        c = ComposeToECSConverter(account_id="123456789012")
        out = c._convert_secrets(
            [{"name": "api_key"}],
            region="us-east-1",
        )
        assert len(out) == 1
        assert "api_key" in out[0]["valueFrom"]

    def test_string_secret_emits_warning_no_arn(self):
        # Compose's bare-string form (``secrets: ['db-password']``) doesn't
        # carry enough info to build an ARN — surfaces as a warning, not
        # a conversion entry.
        c = ComposeToECSConverter(account_id="123456789012")
        out = c._convert_secrets(["bare-string-secret"], region="us-west-1")
        assert out == []
        assert any("manual configuration" in w for w in c._conversion_warnings)


# ---------------------------------------------------------------------------
# Error paths: missing account_id / region surface clearly
# ---------------------------------------------------------------------------


class TestSecretsArnErrors:
    def test_missing_account_id_raises_when_secrets_present(self):
        c = ComposeToECSConverter()  # default account_id=None
        with pytest.raises(ComposeConversionError, match="account_id"):
            c._convert_secrets(
                [{"source": "db-password"}],
                region="us-west-2",
            )

    def test_missing_region_raises_when_secrets_present(self):
        c = ComposeToECSConverter(account_id="123456789012")
        with pytest.raises(ComposeConversionError, match="region"):
            c._convert_secrets(
                [{"source": "db-password"}],
                region=None,
            )

    def test_no_secrets_no_account_id_is_a_noop(self):
        # An empty secrets list when account_id is missing must NOT raise
        # — converters get constructed widely, often without secrets in
        # the path.
        c = ComposeToECSConverter()
        assert c._convert_secrets([], region=None) == []
        assert c._convert_secrets([], region="us-west-2") == []

    def test_arn_does_not_contain_literal_placeholder(self):
        c = ComposeToECSConverter(account_id="999999999999")
        out = c._convert_secrets(
            [{"source": "x"}],
            region="eu-west-1",
        )
        rendered = out[0]["valueFrom"]
        assert "REGION" not in rendered
        assert "ACCOUNT" not in rendered
        assert ":eu-west-1:" in rendered
        assert ":999999999999:" in rendered


# ---------------------------------------------------------------------------
# Constructor accepts account_id + remains backward-compatible without it
# ---------------------------------------------------------------------------


class TestConverterConstructor:
    def test_account_id_default_is_none(self):
        c = ComposeToECSConverter()
        assert c._account_id is None

    def test_account_id_kwarg_stored(self):
        c = ComposeToECSConverter(account_id="123456789012")
        assert c._account_id == "123456789012"
