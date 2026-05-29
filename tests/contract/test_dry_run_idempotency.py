"""Provider contract: dry-run (plan) read-only semantics + idempotency.

rc-s4n.4. These assert the cross-provider invariants that make `rc plan`
and `rc deploy --dry-run` safe and that make re-running deploy/destroy/plan
convergent. They run against every provider the suite is parameterized over
(FakeProvider by default; real providers via RC_CONTRACT_PROVIDERS).

The read-only-plan tests are the provider-level guard for GitHub issue #2
(`rc deploy --dry-run` silently ran a real deploy): rc routes --dry-run to
`plan()`, so the safety of dry-run rests entirely on plan() having no side
effects. If a provider's plan() ever mutates live state, these fail.
"""

from __future__ import annotations

import copy

import pytest

from remote_compose.provider import DeployContext, PlanResult, Provider


def _status_snapshot(provider: Provider, ctx: DeployContext):
    """Comparable view of live state: (name, desired, running) per service."""
    report = provider.status(ctx)
    return sorted((s.name, s.desired, s.running) for s in report.services)


# ---------------------------------------------------------------------------
# plan() is read-only — the dry-run safety contract (issue #2)
# ---------------------------------------------------------------------------


def test_plan_on_undeployed_stack_creates_nothing(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    """plan() against a never-deployed stack must not deploy anything.

    This is the exact failure mode of issue #2: a 'dry-run' that actually
    stood the stack up. After plan(), status must still report zero services.
    """
    before = _status_snapshot(provider, minimal_ctx)
    assert before == [], "precondition: stack must start undeployed"

    provider.plan(minimal_ctx)

    after = _status_snapshot(provider, minimal_ctx)
    assert after == [], "plan() must not create any live resources"


def test_plan_does_not_mutate_live_state(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    """plan() on an already-deployed stack must leave running state untouched."""
    provider.deploy(minimal_ctx)
    before = _status_snapshot(provider, minimal_ctx)

    provider.plan(minimal_ctx)

    after = _status_snapshot(provider, minimal_ctx)
    assert after == before, "plan() must be read-only — live state changed"


def test_plan_after_deploy_reports_no_changes(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    """A plan immediately after a deploy of the same ctx must be a no-op diff.

    This is what makes `rc deploy --dry-run` meaningful after a deploy:
    nothing to create/update/destroy. It's also terraform's core idempotency
    guarantee surfaced through the provider contract.
    """
    provider.deploy(minimal_ctx)
    result = provider.plan(minimal_ctx)
    assert isinstance(result, PlanResult)
    assert (result.create, result.update, result.destroy) == (0, 0, 0), (
        "plan after an unchanged deploy must show 0 add / 0 change / 0 destroy; "
        f"got create={result.create} update={result.update} destroy={result.destroy}"
    )


def test_plan_is_idempotent(provider: Provider, minimal_ctx: DeployContext) -> None:
    """Two consecutive plans on the same ctx yield identical counts."""
    first = provider.plan(minimal_ctx)
    second = provider.plan(minimal_ctx)
    assert (first.create, first.update, first.destroy) == (
        second.create,
        second.update,
        second.destroy,
    ), "plan() counts must be stable across repeated calls"


# ---------------------------------------------------------------------------
# destroy() idempotency
# ---------------------------------------------------------------------------


def test_destroy_is_idempotent(provider: Provider, minimal_ctx: DeployContext) -> None:
    """destroy() on already-destroyed state is a no-op, not an error."""
    provider.deploy(minimal_ctx)
    provider.destroy(minimal_ctx)
    # Second destroy must not raise.
    provider.destroy(minimal_ctx)
    assert _status_snapshot(provider, minimal_ctx) == []


def test_plan_on_destroyed_stack_is_safe(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    """plan() after destroy must succeed and not resurrect the stack."""
    provider.deploy(minimal_ctx)
    provider.destroy(minimal_ctx)
    result = provider.plan(minimal_ctx)
    assert isinstance(result, PlanResult)
    assert _status_snapshot(provider, minimal_ctx) == []


# ---------------------------------------------------------------------------
# ctx is read-only input — providers must not mutate it
# ---------------------------------------------------------------------------
# base.Provider docstring: "Methods may read from ctx but should not mutate
# it — the only ctx mutations rc itself performs are dev_mode / expires_at,
# set BEFORE calling the provider." A provider that rewrites ctx.services in
# place would corrupt a caller that reuses the context (e.g. rc up: plan then
# deploy then status all share one ctx).


def test_plan_does_not_mutate_context(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    snapshot = copy.deepcopy(minimal_ctx.services)
    provider.plan(minimal_ctx)
    assert minimal_ctx.services == snapshot, "plan() mutated ctx.services"


def test_deploy_does_not_mutate_context(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    snapshot = copy.deepcopy(minimal_ctx.services)
    provider.deploy(minimal_ctx)
    assert minimal_ctx.services == snapshot, "deploy() mutated ctx.services"


# ---------------------------------------------------------------------------
# rollback error contract
# ---------------------------------------------------------------------------


def test_rollback_with_no_history_raises(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    """rollback() before any deploy must raise, not silently succeed."""
    from remote_compose.provider import ProviderError

    with pytest.raises((ProviderError, Exception)):
        provider.rollback(minimal_ctx)
