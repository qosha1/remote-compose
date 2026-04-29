"""Default secret-deletion behavior preserves the AWS 30-day recovery
window (remote-compose-myw).

The earlier `rc destroy --infra` path called delete_secret with
ForceDeleteWithoutRecovery=True for every Secrets Manager entry,
bypassing AWS's standard 30-day grace period. A mistaken destroy was
unrecoverable. The fix makes ForceDeleteWithoutRecovery opt-in via the
new --force-delete-secrets flag; default leaves the recovery window
intact.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from remote_compose.cli import _teardown_infrastructure


@pytest.fixture
def mock_cluster():
    cluster = MagicMock()
    cluster.aws_region = "us-west-1"
    cluster.aws_credential = None
    return cluster


def _patches(secrets):
    """Common patches: SecretConfig queryset, AWS factory, no other side effects."""
    secret_qs = MagicMock()
    secret_qs.exists.return_value = bool(secrets)
    secret_qs.count.return_value = len(secrets)
    secret_qs.__iter__ = lambda self: iter(secrets)

    sm_client = MagicMock()
    factory = MagicMock()
    factory.get_client.return_value = sm_client
    return secret_qs, sm_client, factory


def _make_secret_record(arn: str):
    s = MagicMock()
    s.secret_arn = arn
    return s


class TestForceDeleteSecretsFlag:
    def test_default_uses_30day_recovery_window(self, mock_cluster):
        """Default path: delete_secret called WITHOUT
        ForceDeleteWithoutRecovery, so AWS keeps the 30-day grace
        period."""
        secret = _make_secret_record("arn:secret:foo")
        secret_qs, sm, factory = _patches([secret])

        SecretConfig = MagicMock()
        SecretConfig.objects.filter.return_value = secret_qs
        with patch.dict(
            "sys.modules",
            {"remote_compose.models": MagicMock(SecretConfig=SecretConfig)},
        ), patch(
            "remote_compose.services.aws_client_factory.get_aws_client_factory",
            return_value=factory,
        ), patch(
            "remote_compose.services.vpc_service.VPCService"
        ), patch(
            "remote_compose.cli.click.echo"
        ):
            _teardown_infrastructure(mock_cluster)

        # delete_secret was invoked but WITHOUT the recovery-bypass kwarg.
        sm.delete_secret.assert_called_once()
        kwargs = sm.delete_secret.call_args.kwargs
        assert kwargs.get("SecretId") == "arn:secret:foo"
        assert "ForceDeleteWithoutRecovery" not in kwargs

    def test_force_flag_bypasses_recovery_window(self, mock_cluster):
        """With force_delete_secrets=True, the call passes
        ForceDeleteWithoutRecovery=True (opt-in unsafe path)."""
        secret = _make_secret_record("arn:secret:foo")
        secret_qs, sm, factory = _patches([secret])

        SecretConfig = MagicMock()
        SecretConfig.objects.filter.return_value = secret_qs
        with patch.dict(
            "sys.modules",
            {"remote_compose.models": MagicMock(SecretConfig=SecretConfig)},
        ), patch(
            "remote_compose.services.aws_client_factory.get_aws_client_factory",
            return_value=factory,
        ), patch(
            "remote_compose.services.vpc_service.VPCService"
        ), patch(
            "remote_compose.cli.click.echo"
        ):
            _teardown_infrastructure(mock_cluster, force_delete_secrets=True)

        sm.delete_secret.assert_called_once()
        kwargs = sm.delete_secret.call_args.kwargs
        assert kwargs.get("SecretId") == "arn:secret:foo"
        assert kwargs.get("ForceDeleteWithoutRecovery") is True

    def test_no_secrets_no_call(self, mock_cluster):
        """Empty secrets queryset → delete_secret not invoked, no
        side effect."""
        secret_qs, sm, factory = _patches([])

        SecretConfig = MagicMock()
        SecretConfig.objects.filter.return_value = secret_qs
        with patch.dict(
            "sys.modules",
            {"remote_compose.models": MagicMock(SecretConfig=SecretConfig)},
        ), patch(
            "remote_compose.services.aws_client_factory.get_aws_client_factory",
            return_value=factory,
        ), patch(
            "remote_compose.services.vpc_service.VPCService"
        ), patch(
            "remote_compose.cli.click.echo"
        ):
            _teardown_infrastructure(mock_cluster)

        sm.delete_secret.assert_not_called()
