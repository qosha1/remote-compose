"""Provider contract tests.

Every implementation of :class:`remote_compose.provider.Provider` must pass
this suite. Tests that require real network behavior (public ingress, persistent
volumes surviving a pod restart, cross-service DNS) skip on FakeProvider —
those behaviors are the responsibility of each concrete provider.

The suite is parameterized over all registered providers (see conftest).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from remote_compose.provider import (
    DeployContext,
    DeployResult,
    ExecResult,
    PlanResult,
    Provider,
    SecretRef,
    ServiceSpec,
    StatusReport,
)


def _is_fake(provider: Provider) -> bool:
    return provider.name == "fake"


def _skip_if_fake(provider: Provider, why: str) -> None:
    if _is_fake(provider):
        pytest.skip(f"FakeProvider does not exercise: {why}")


def _terraform_available() -> bool:
    return shutil.which("terraform") is not None


# ---------------------------------------------------------------------------
# Deploy semantics
# ---------------------------------------------------------------------------

def test_deploy_returns_deploy_result(provider: Provider, minimal_ctx: DeployContext) -> None:
    result = provider.deploy(minimal_ctx)
    assert isinstance(result, DeployResult)
    assert result.revision_id
    assert set(result.services) == set(minimal_ctx.services.keys())


def test_deploy_idempotent(provider: Provider, minimal_ctx: DeployContext) -> None:
    """A second deploy with the same context must be a no-op."""
    first = provider.deploy(minimal_ctx)
    second = provider.deploy(minimal_ctx)
    assert first.revision_id == second.revision_id, (
        "deploy must be idempotent — same input must yield same revision id"
    )


def test_deploy_reconciles_after_partial_failure(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    """Retrying deploy after a fault must converge to desired state."""
    if _is_fake(provider):
        provider.inject_fault_once("mid_deploy")  # FakeProvider hook
    with pytest.raises(Exception):
        provider.deploy(minimal_ctx)
    result = provider.deploy(minimal_ctx)
    status = provider.status(minimal_ctx)
    assert all(s.running == s.desired for s in status.services), (
        "deploy must reconcile partial failure — every service should reach desired replicas"
    )
    assert result.revision_id


def test_redeploy_forces_new_revision(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    first = provider.deploy(minimal_ctx)
    forced = provider.redeploy(minimal_ctx)
    assert forced.revision_id != first.revision_id


# ---------------------------------------------------------------------------
# Status, logs, exec
# ---------------------------------------------------------------------------

def test_status_reflects_live_state(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    provider.deploy(minimal_ctx)
    report = provider.status(minimal_ctx)
    assert isinstance(report, StatusReport)
    names = {s.name for s in report.services}
    assert names == set(minimal_ctx.services.keys())
    for s in report.services:
        assert s.desired == minimal_ctx.services[s.name].replicas


def test_logs_returns_recent_output(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    provider.deploy(minimal_ctx)
    lines = list(provider.logs(minimal_ctx, service="api", tail=50))
    assert isinstance(lines, list)


def test_exec_runs_and_returns_exit_code(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    provider.deploy(minimal_ctx)
    result = provider.exec(minimal_ctx, service="api", command=["echo", "hi"])
    assert isinstance(result, ExecResult)
    assert result.exit_code == 0
    assert "hi" in result.stdout


# ---------------------------------------------------------------------------
# Rollback, destroy
# ---------------------------------------------------------------------------

def test_rollback_reverts_to_previous_revision(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    first = provider.deploy(minimal_ctx)
    # mutate: remove a service
    ctx_changed = DeployContext(
        project=minimal_ctx.project,
        compose_path=minimal_ctx.compose_path,
        rc_yml_v2=minimal_ctx.rc_yml_v2,
        provider_config=minimal_ctx.provider_config,
        tf_backend_config=minimal_ctx.tf_backend_config,
        working_dir=minimal_ctx.working_dir,
        services={k: v for k, v in minimal_ctx.services.items() if k != "cache"},
        secrets=minimal_ctx.secrets,
    )
    provider.deploy(ctx_changed)
    rolled = provider.rollback(minimal_ctx)
    status = provider.status(minimal_ctx)
    names = {s.name for s in status.services}
    assert "cache" in names, "rollback should restore the removed service"
    assert rolled.revision_id


def test_destroy_removes_all_created_resources(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    provider.deploy(minimal_ctx)
    provider.destroy(minimal_ctx)
    report = provider.status(minimal_ctx)
    assert report.services == []


# ---------------------------------------------------------------------------
# Terraform portability (FR-7)
# ---------------------------------------------------------------------------

def test_emit_terraform_writes_module(
    provider: Provider, minimal_ctx: DeployContext, tmp_path: Path
) -> None:
    out_dir = tmp_path / "tf_out"
    result = provider.emit_terraform(minimal_ctx, out_dir)
    assert result.exists()
    # at minimum: a main.tf (or equivalent top-level file) must be present
    tf_files = list(result.glob("*.tf"))
    assert tf_files, "emit_terraform must write at least one .tf file"


def test_emit_terraform_is_idempotent(
    provider: Provider, minimal_ctx: DeployContext, tmp_path: Path
) -> None:
    first_dir = tmp_path / "tf_first"
    second_dir = tmp_path / "tf_second"
    provider.emit_terraform(minimal_ctx, first_dir)
    provider.emit_terraform(minimal_ctx, second_dir)
    first = sorted(p.name for p in first_dir.glob("*"))
    second = sorted(p.name for p in second_dir.glob("*"))
    assert first == second
    for name in first:
        a = (first_dir / name).read_bytes()
        b = (second_dir / name).read_bytes()
        assert a == b, f"emit_terraform is not idempotent for {name}"


@pytest.mark.skipif(not _terraform_available(), reason="terraform binary required")
def test_emit_terraform_is_standalone(
    provider: Provider, minimal_ctx: DeployContext, tmp_path: Path
) -> None:
    """The emitted module must `terraform init && terraform validate` without rc (FR-7)."""
    _skip_if_fake(
        provider,
        "FakeProvider emits a placeholder module; standalone validation is "
        "only meaningful for real providers",
    )
    out_dir = tmp_path / "tf_standalone"
    provider.emit_terraform(minimal_ctx, out_dir)
    init = subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false"],
        cwd=out_dir, capture_output=True, text=True,
    )
    assert init.returncode == 0, f"terraform init failed:\n{init.stderr}"
    validate = subprocess.run(
        ["terraform", "validate"],
        cwd=out_dir, capture_output=True, text=True,
    )
    assert validate.returncode == 0, f"terraform validate failed:\n{validate.stderr}"


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------

def test_secret_value_never_leaks_to_terraform(
    provider: Provider, minimal_ctx: DeployContext, tmp_path: Path
) -> None:
    """A secret value passed through rc must never appear in emitted HCL."""
    sentinel = "SECRET_SENTINEL_c7a9f2e4d3"
    ctx = DeployContext(
        project=minimal_ctx.project,
        compose_path=minimal_ctx.compose_path,
        rc_yml_v2=minimal_ctx.rc_yml_v2,
        provider_config=minimal_ctx.provider_config,
        tf_backend_config=minimal_ctx.tf_backend_config,
        working_dir=minimal_ctx.working_dir,
        services=minimal_ctx.services,
        secrets=[SecretRef(name="app", source="file", path=f"/tmp/{sentinel}")],
    )
    out_dir = tmp_path / "tf_secret"
    provider.emit_terraform(ctx, out_dir)
    for tf in out_dir.rglob("*.tf"):
        assert sentinel not in tf.read_text(), (
            f"secret sentinel leaked into {tf}"
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def test_plan_returns_structured_result(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    result = provider.plan(minimal_ctx)
    assert isinstance(result, PlanResult)
    assert result.create >= 0
    assert result.update >= 0
    assert result.destroy >= 0


# ---------------------------------------------------------------------------
# Network behaviors — real providers only
# ---------------------------------------------------------------------------

def test_public_service_receives_traffic(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    _skip_if_fake(provider, "public ingress requires a real load balancer")
    provider.deploy(minimal_ctx)
    report = provider.status(minimal_ctx)
    assert report.ingress_url, "public service must expose an ingress_url"
    # downstream HTTP assertion lives in Tier 4 E2E, not contract tests


def test_persistent_volume_survives_redeploy(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    _skip_if_fake(provider, "persistent volume semantics require real storage")
    # Real test body lives in the provider's own integration suite.
    pytest.skip("covered in provider integration tier")


def test_service_to_service_networking_works(
    provider: Provider, minimal_ctx: DeployContext
) -> None:
    _skip_if_fake(provider, "cross-service DNS requires a real cluster")
    pytest.skip("covered in provider integration tier")
