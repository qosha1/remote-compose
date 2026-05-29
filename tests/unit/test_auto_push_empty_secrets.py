"""Tests for rc-e5u.44.20 — auto-push when SM blob is empty after rc deploy.

The bug: `terraform apply` creates the SM secret resource with a placeholder
empty blob; `rc deploy` finishes "successfully" but every ECS task that
references the secret fails to start with "did not contain json key X".
Today's fix: after provider.deploy() returns, query SM for each file-sourced
secret. If any blob is `{}` or missing keys, auto-run `rc secrets push`.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from remote_compose.cli import _detect_empty_file_secrets

# ---------------------------------------------------------------------------
# _detect_empty_file_secrets — pure SM-querying logic
# ---------------------------------------------------------------------------


class _Secret:
    def __init__(self, name, source="file", path=None):
        self.name = name
        self.source = source
        self.path = path


class _V2:
    def __init__(self, project="myproj"):
        self.project = project


def _stub_session(secret_values: dict, errors: dict = None):
    """Build a boto3.Session stub whose SM client returns SecretString blobs
    keyed by SecretId. errors maps SecretId -> ClientError to raise.
    """
    from botocore.exceptions import ClientError

    errors = errors or {}

    def get_secret_value(*, SecretId):  # noqa: N803 — boto3 kwarg
        if SecretId in errors:
            raise errors[SecretId]
        if SecretId not in secret_values:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ResourceNotFoundException",
                        "Message": "not found",
                    }
                },
                "GetSecretValue",
            )
        return {"SecretString": secret_values[SecretId]}

    sm = MagicMock()
    sm.get_secret_value.side_effect = get_secret_value
    session = MagicMock()
    session.client.return_value = sm
    return session


def test_no_empty_secrets_returns_empty_list():
    v2 = _V2()
    file_secrets = [_Secret("django"), _Secret("postgres")]
    session = _stub_session(
        {
            "myproj/django": json.dumps({"AWS_KEY": "xx", "DB_URL": "yy"}),
            "myproj/postgres": json.dumps({"POSTGRES_HOST": "zz"}),
        }
    )
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert empty == []


def test_empty_blob_is_detected():
    v2 = _V2()
    file_secrets = [_Secret("django"), _Secret("postgres")]
    session = _stub_session(
        {
            "myproj/django": json.dumps({"AWS_KEY": "xx"}),
            "myproj/postgres": "{}",  # placeholder created by terraform
        }
    )
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert empty == ["postgres"]


def test_missing_secret_string_is_empty():
    v2 = _V2()
    file_secrets = [_Secret("django")]
    # Some SM API responses omit SecretString entirely (binary-only secrets,
    # or partial reads). Treat as empty.
    sm = MagicMock()
    sm.get_secret_value.return_value = {}  # no SecretString key
    session = MagicMock()
    session.client.return_value = sm
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert empty == ["django"]


def test_not_found_secret_is_NOT_treated_as_empty():
    """Terraform hasn't applied yet. Caller's deploy will create it.
    Treating as empty would trigger an auto-push that itself would fail."""
    from botocore.exceptions import ClientError

    v2 = _V2()
    file_secrets = [_Secret("django")]
    err = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
        "GetSecretValue",
    )
    session = _stub_session({}, errors={"myproj/django": err})
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert empty == []


def test_pending_deletion_is_NOT_treated_as_empty():
    from botocore.exceptions import ClientError

    v2 = _V2()
    file_secrets = [_Secret("django")]
    err = ClientError(
        {
            "Error": {
                "Code": "InvalidRequestException",
                "Message": "scheduled for deletion",
            }
        },
        "GetSecretValue",
    )
    session = _stub_session({}, errors={"myproj/django": err})
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert empty == []


def test_other_aws_errors_propagate():
    """AccessDenied / throttling MUST propagate so we don't silently mask
    a perms problem (we'd rather fail loud than silently auto-push half)."""
    from botocore.exceptions import ClientError

    v2 = _V2()
    file_secrets = [_Secret("django")]
    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetSecretValue",
    )
    session = _stub_session({}, errors={"myproj/django": err})
    with patch("boto3.Session", return_value=session):
        with pytest.raises(ClientError, match="AccessDenied"):
            _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)


def test_non_json_blob_is_not_treated_as_empty():
    """A user might manually set a raw value (e.g., a TLS cert PEM). We
    don't try to parse + populate over those — leave them alone."""
    v2 = _V2()
    file_secrets = [_Secret("cert")]
    session = _stub_session({"myproj/cert": "-----BEGIN CERTIFICATE-----\n..."})
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert empty == []


def test_multiple_empties_all_returned():
    v2 = _V2()
    file_secrets = [_Secret("a"), _Secret("b"), _Secret("c")]
    session = _stub_session(
        {
            "myproj/a": "{}",
            "myproj/b": json.dumps({"K": "v"}),
            "myproj/c": "{}",
        }
    )
    with patch("boto3.Session", return_value=session):
        empty = _detect_empty_file_secrets(v2, "us-west-1", None, file_secrets)
    assert sorted(empty) == ["a", "c"]


# ---------------------------------------------------------------------------
# _auto_push_empty_secrets_if_any — integration with the deploy dispatcher
# ---------------------------------------------------------------------------


class TestAutoPushIntegration:
    def _setup(self, tmp_path, env_body="POSTGRES_HOST=db\nDB_PORT=5432\n"):
        env_path = tmp_path / ".envs" / ".django"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(env_body)
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(
            "services:\n  django:\n    image: x\n    env_file:\n      - "
            + str(env_path)
            + "\n"
        )
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text(
            "version: 2\nproject: testp\ncompose_file: docker-compose.yml\n"
            "provider: ecs\nprovider_config: {ecs: {region: us-west-1}}\n"
            "terraform: {backend: {type: local}}\n"
            "secrets:\n  - {name: env, source: env_file_auto}\n"
        )
        return rc_path

    def test_auto_push_fires_when_secret_is_empty(self, tmp_path):
        from remote_compose.cli_v2 import (
            _auto_push_empty_secrets_if_any,
            load_rc_yml,
        )

        rc_path = self._setup(tmp_path)
        _, raw, v2 = load_rc_yml(rc_path)

        with (
            patch(
                "remote_compose.cli._detect_empty_file_secrets", return_value=["django"]
            ) as detect,
            patch("remote_compose.cli._secrets_push_v2") as push,
        ):
            _auto_push_empty_secrets_if_any(rc_path, v2, raw)

        assert detect.called
        push.assert_called_once_with(str(rc_path), rollout=True)

    def test_auto_push_skipped_when_no_empty_secrets(self, tmp_path):
        from remote_compose.cli_v2 import (
            _auto_push_empty_secrets_if_any,
            load_rc_yml,
        )

        rc_path = self._setup(tmp_path)
        _, raw, v2 = load_rc_yml(rc_path)

        with (
            patch(
                "remote_compose.cli._detect_empty_file_secrets", return_value=[]
            ) as detect,
            patch("remote_compose.cli._secrets_push_v2") as push,
        ):
            _auto_push_empty_secrets_if_any(rc_path, v2, raw)

        assert detect.called
        push.assert_not_called()

    def test_auto_push_no_op_when_no_secrets_in_rcyml(self, tmp_path):
        """No file-sourced secrets at all → don't even query SM."""
        from remote_compose.cli_v2 import (
            _auto_push_empty_secrets_if_any,
            load_rc_yml,
        )

        rc_path = tmp_path / "rc.yml"
        rc_path.write_text(
            "version: 2\nproject: testp\ncompose_file: docker-compose.yml\n"
            "provider: ecs\nprovider_config: {ecs: {region: us-west-1}}\n"
            "terraform: {backend: {type: local}}\n"
        )
        (tmp_path / "docker-compose.yml").write_text("services: {x: {image: y}}")
        _, raw, v2 = load_rc_yml(rc_path)

        with (
            patch("remote_compose.cli._detect_empty_file_secrets") as detect,
            patch("remote_compose.cli._secrets_push_v2") as push,
        ):
            _auto_push_empty_secrets_if_any(rc_path, v2, raw)

        detect.assert_not_called()
        push.assert_not_called()

    def test_detect_failure_warns_but_does_not_fail_deploy(self, tmp_path, capsys):
        """A flaky SM API call shouldn't abort an otherwise-successful
        deploy — surface a warning so the user can manually push if needed."""
        from remote_compose.cli_v2 import (
            _auto_push_empty_secrets_if_any,
            load_rc_yml,
        )

        rc_path = self._setup(tmp_path)
        _, raw, v2 = load_rc_yml(rc_path)

        with (
            patch(
                "remote_compose.cli._detect_empty_file_secrets",
                side_effect=RuntimeError("transient SM error"),
            ),
            patch("remote_compose.cli._secrets_push_v2") as push,
        ):
            _auto_push_empty_secrets_if_any(rc_path, v2, raw)

        # Did NOT call push (detection failed before we knew what to push)
        push.assert_not_called()
        out = capsys.readouterr()
        assert "WARN" in (out.err + out.out)
        assert "rc secrets push" in (out.err + out.out)
