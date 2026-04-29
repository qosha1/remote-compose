"""SetupIAMRolesStep fails the pipeline on policy update error
(remote-compose-j06).

Earlier behavior returned ``StepResult.ok()`` even when
``iam.put_role_policy`` raised. Pipeline kept going, created services
referencing secrets, services crashed at startup with
ResourceInitializationError because the execution role couldn't read
SecretsManager values. The user got a crash-loop with no breadcrumb
pointing at IAM.

Fix: return ``StepResult.fail()`` when secrets are configured AND the
policy update raises. Other paths (no secrets, no execution role,
dry-run) still return ok().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from remote_compose.services.deployment_pipeline.steps.infrastructure import (
    SetupIAMRolesStep,
)


@pytest.fixture
def step():
    return SetupIAMRolesStep()


def _ctx(*, dry_run=False, secrets=None, role_arn=None):
    """Minimal pipeline context. AWS calls are mocked at the boto3
    factory layer so the test exercises the success/failure branches
    without touching the network."""
    ctx = MagicMock()
    ctx.dry_run = dry_run
    ctx.secrets_arns = secrets or {}
    ctx.cluster.task_execution_role_arn = role_arn
    ctx.cluster.name = "test-cluster"
    ctx.cluster.aws_region = "us-west-1"
    ctx.cluster.aws_credential = None
    ctx.add_warning = MagicMock()
    return ctx


class TestIAMStepHappyPaths:
    def test_dry_run_returns_ok(self, step):
        result = step.execute(_ctx(dry_run=True))
        assert result.success is True

    def test_no_execution_role_returns_ok(self, step):
        result = step.execute(_ctx(role_arn=None))
        assert result.success is True

    def test_no_secrets_returns_ok(self, step):
        result = step.execute(_ctx(role_arn="arn:role/foo", secrets={}))
        assert result.success is True


class TestIAMStepFailsOnPolicyError:
    def test_put_role_policy_raises_returns_fail_not_ok(self, step):
        # remote-compose-j06: the regression test. With secrets
        # configured + IAM policy update raising, the step MUST return
        # fail() so the pipeline aborts before creating broken services.
        ctx = _ctx(
            role_arn="arn:aws:iam::123:role/test-role",
            secrets={"DB_URL": "arn:aws:secretsmanager:us-west-1:123:secret:db"},
        )
        with patch(
            "remote_compose.services.aws_client_factory.get_aws_client_factory"
        ) as factory_fn:
            iam = MagicMock()
            iam.put_role_policy.side_effect = RuntimeError(
                "AccessDenied: iam:PutRolePolicy"
            )
            factory = MagicMock()
            factory.get_client.return_value = iam
            factory_fn.return_value = factory

            result = step.execute(ctx)

        assert result.success is False, (
            "IAM policy update failure must fail the pipeline so "
            "services depending on the secrets aren't created with "
            "a broken execution role (remote-compose-j06)."
        )
        # Warning was still logged for the user-facing context.
        ctx.add_warning.assert_called_once()
        # Error message is informative — names the role + the
        # underlying exception.
        msg = result.message or ""
        assert "test-role" in msg
        assert "iam:PutRolePolicy" in msg or "AccessDenied" in msg

    def test_successful_put_role_policy_returns_ok(self, step):
        ctx = _ctx(
            role_arn="arn:aws:iam::123:role/test-role",
            secrets={"DB_URL": "arn:aws:secretsmanager:us-west-1:123:secret:db"},
        )
        with patch(
            "remote_compose.services.aws_client_factory.get_aws_client_factory"
        ) as factory_fn:
            iam = MagicMock()
            iam.put_role_policy.return_value = {}
            factory = MagicMock()
            factory.get_client.return_value = iam
            factory_fn.return_value = factory

            result = step.execute(ctx)

        assert result.success is True
        iam.put_role_policy.assert_called_once()
