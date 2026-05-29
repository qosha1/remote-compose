"""End-to-end lifecycle test against real AWS (us-east-1).

This is the truth gate: terraform really applies, boto3 really talks to
AWS, and the reap script really cleans up.

Prerequisites (enforced by the e2e_preconditions fixture):
  RC_E2E=1, AWS creds, terraform binary, us-east-1 empty of rc-test-*.

Cost: ~$0.05 per run (Fargate task + ALB for ~5-10 minutes).
Runtime: ~6-10 minutes end to end.
"""

from __future__ import annotations

import pytest

from remote_compose.provider import DeployContext
from remote_compose.provider.ecs import ECSProvider

pytestmark = pytest.mark.e2e


class TestECSFullLifecycle:
    def test_deploy_then_status_then_destroy(
        self,
        e2e_lifecycle: DeployContext,
        provider: ECSProvider,
    ) -> None:
        ctx = e2e_lifecycle

        # DEPLOY
        result = provider.deploy(ctx)
        assert result.revision_id
        assert set(result.services) == {"web"}
        assert (
            "alb_dns_name" in result.terraform_outputs
        ), "deploy should populate alb_dns_name in terraform outputs"

        # STATUS — service should be registered with ECS even if not yet
        # running (no image has been pushed to ECR)
        report = provider.status(ctx)
        names = {s.name for s in report.services}
        assert "web" in names, f"status should report web, got {names}"

        # The e2e_lifecycle fixture's teardown destroys everything.

    def test_deploy_is_idempotent(
        self,
        e2e_lifecycle: DeployContext,
        provider: ECSProvider,
    ) -> None:
        ctx = e2e_lifecycle

        first = provider.deploy(ctx)
        # second deploy with unchanged inputs — revision_id derives from
        # emitted module content, so must match
        second = provider.deploy(ctx)
        assert (
            first.revision_id == second.revision_id
        ), "deploy must be idempotent — same inputs must yield same revision id"


class TestECSRedeploy:
    def test_redeploy_returns_cleanly(
        self,
        e2e_lifecycle: DeployContext,
        provider: ECSProvider,
    ) -> None:
        ctx = e2e_lifecycle
        provider.deploy(ctx)
        result = provider.redeploy(ctx)
        assert set(result.services) == {"web"}


class TestECSPlan:
    def test_plan_runs_after_deploy_and_returns_no_changes(
        self,
        e2e_lifecycle: DeployContext,
        provider: ECSProvider,
    ) -> None:
        ctx = e2e_lifecycle
        provider.deploy(ctx)
        summary = provider.plan(ctx)
        # A plan right after deploy with identical inputs should show 0 changes
        assert summary.create + summary.update + summary.destroy == 0, (
            f"plan should show no changes after an idempotent deploy; got "
            f"create={summary.create} update={summary.update} destroy={summary.destroy}"
        )
