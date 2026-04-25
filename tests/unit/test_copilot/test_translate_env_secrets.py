"""Translate Copilot variables + secrets → compose env + rc.yml secrets.

Copilot:
  variables: { KEY: literal-value }     → compose service.environment
  secrets:   { KEY: { secretsmanager:'arn::JSON_KEY::' } } → rc.yml secrets list

The Copilot env-var interpolation `${COPILOT_ENVIRONMENT_NAME}` resolves
per environment when --env is given; left as a literal placeholder when
multi-env is requested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.copilot.discover import CopilotService
from remote_compose.copilot.translate import (
    translate_env_and_secrets,
)


def _svc(raw: dict) -> CopilotService:
    return CopilotService(
        name=raw.get("name", "x"),
        type=raw.get("type", "Backend Service"),
        manifest_path=Path("/dev/null"),
        raw=raw,
    )


# ---------------------------------------------------------------------
# variables → compose environment
# ---------------------------------------------------------------------

class TestVariables:
    def test_simple_variables_become_compose_environment(self):
        compose_env, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "variables": {"FOO": "1", "DJANGO_SETTINGS_MODULE": "config.settings.production"},
        }))
        assert compose_env["FOO"] == "1"
        assert compose_env["DJANGO_SETTINGS_MODULE"] == "config.settings.production"
        assert rc_secrets == []

    def test_int_and_bool_variables_stringified(self):
        # Compose environment values must be strings.
        compose_env, _, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "variables": {"COUNT": 3, "FLAG": True},
        }))
        assert compose_env["COUNT"] == "3"
        assert compose_env["FLAG"] in {"True", "true"}

    def test_no_variables_block_returns_empty_dict(self):
        compose_env, rc_secrets, _ = translate_env_and_secrets(_svc({"name": "api"}))
        assert compose_env == {}
        assert rc_secrets == []


# ---------------------------------------------------------------------
# secrets.<KEY>.secretsmanager → rc.yml v2 secrets list (source=aws_sm)
# ---------------------------------------------------------------------

class TestSecrets:
    def test_secretsmanager_pointer_becomes_aws_sm_secret(self):
        _, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "secrets": {
                "DB_PASSWORD": {
                    "secretsmanager": "arn:aws:secretsmanager:us-west-2:123456789012:secret:prod/db-AbCdEf"
                },
            },
        }))
        assert len(rc_secrets) == 1
        s = rc_secrets[0]
        assert s["name"] == "DB_PASSWORD"
        assert s["source"] == "aws_sm"
        assert s["arn"] == "arn:aws:secretsmanager:us-west-2:123456789012:secret:prod/db-AbCdEf"

    def test_ssm_parameter_pointer_supported(self):
        # Copilot also supports `ssm: <arn>` for SSM parameters. We
        # treat them as aws_sm-style external references with arn=<ssm-arn>.
        _, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "secrets": {
                "API_TOKEN": {
                    "ssm": "arn:aws:ssm:us-west-2:123456789012:parameter/myapp/api_token",
                },
            },
        }))
        assert rc_secrets[0]["arn"].startswith("arn:aws:ssm:")

    def test_short_form_string_pointer(self):
        # Some manifests use the short form: `secrets: { KEY: arn:... }`.
        _, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "secrets": {
                "KEY": "arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf",
            },
        }))
        assert len(rc_secrets) == 1
        assert rc_secrets[0]["name"] == "KEY"
        assert rc_secrets[0]["source"] == "aws_sm"

    def test_copilot_environment_name_interpolation_left_as_template(self):
        # Per epic plan: when no --env is given, ${COPILOT_ENVIRONMENT_NAME}
        # stays literal so a downstream env-pinning step can substitute.
        _, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "secrets": {
                "DB_PW": {
                    "secretsmanager": "${COPILOT_ENVIRONMENT_NAME}/myapp/creds:DB_PW::",
                },
            },
        }))
        assert "${COPILOT_ENVIRONMENT_NAME}" in rc_secrets[0]["arn"]

    def test_env_name_resolved_when_passed(self):
        _, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "secrets": {
                "DB_PW": {
                    "secretsmanager": "${COPILOT_ENVIRONMENT_NAME}/myapp/creds:DB_PW::",
                },
            },
        }), env="production")
        assert rc_secrets[0]["arn"] == "production/myapp/creds:DB_PW::"
        assert "${COPILOT_ENVIRONMENT_NAME}" not in rc_secrets[0]["arn"]

    def test_secrets_preserve_declaration_order(self):
        _, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "secrets": {
                "B": "arn:aws:secretsmanager:::secret:b",
                "A": "arn:aws:secretsmanager:::secret:a",
                "C": "arn:aws:secretsmanager:::secret:c",
            },
        }))
        assert [s["name"] for s in rc_secrets] == ["B", "A", "C"]


# ---------------------------------------------------------------------
# Mixed
# ---------------------------------------------------------------------

class TestMixed:
    def test_variables_and_secrets_returned_independently(self):
        compose_env, rc_secrets, _ = translate_env_and_secrets(_svc({
            "name": "api",
            "variables": {"DJANGO_SETTINGS_MODULE": "config.settings.production"},
            "secrets": {
                "DB_PASSWORD": {"secretsmanager": "arn:aws:secretsmanager:::secret:dbpw"},
            },
        }))
        assert compose_env["DJANGO_SETTINGS_MODULE"] == "config.settings.production"
        assert rc_secrets[0]["name"] == "DB_PASSWORD"
