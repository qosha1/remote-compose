"""Pytest fixtures for E2E tests that deploy real AWS infrastructure.

Guardrails:
  * Entire suite is gated on RC_E2E=1 + the `e2e` marker. Missing → skip.
  * AWS region locked to us-east-1. Mutating outside the test region is
    impossible because every emitted resource lands there by construction.
  * Every run uses a fresh project name `rc-test-<short_uuid>` so runs
    never collide.
  * Teardown calls provider.destroy; a post-teardown reap runs the
    standalone scripts/reap_test_region.py to catch anything terraform left
    behind (e.g., a destroy mid-failure).

Environment variables:
  RC_E2E=1                  — required to run the suite at all
  AWS_PROFILE=<profile>     — cred source (defaults to whatever boto3 picks up)
  RC_E2E_KEEP_ON_FAIL=1     — skip teardown on test failure (for forensics)
  RC_E2E_REGION=us-east-1   — override (not recommended)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

REGION = os.environ.get("RC_E2E_REGION", "us-east-1")
REAP_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "reap_test_region.py"


def pytest_collection_modifyitems(config, items):
    """Skip all e2e tests unless RC_E2E=1 is set."""
    if os.environ.get("RC_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="E2E suite requires RC_E2E=1")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


def _terraform_usable() -> bool:
    if not shutil.which("terraform"):
        return False
    try:
        result = subprocess.run(
            ["terraform", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _aws_creds_ok() -> bool:
    try:
        import boto3

        sts = boto3.client("sts", region_name=REGION)
        sts.get_caller_identity()
        return True
    except Exception:
        return False


def _test_region_is_empty() -> tuple[bool, str]:
    """Pre-flight: make sure us-east-1 has no rc-test-* resources already."""
    try:
        import boto3

        ecs = boto3.client("ecs", region_name=REGION)
        for arn in ecs.list_clusters().get("clusterArns", []):
            details = ecs.describe_clusters(clusters=[arn], include=["TAGS"])[
                "clusters"
            ]
            if details and any(
                t.get("Key") == "Project"
                and (t.get("Value") or "").startswith("rc-test-")
                for t in details[0].get("tags", [])
            ):
                return False, f"found existing rc-test-* cluster: {arn}"
        return True, ""
    except Exception as exc:
        return False, f"pre-flight check failed: {exc}"


@pytest.fixture(scope="session")
def e2e_preconditions():
    if os.environ.get("RC_E2E") != "1":
        pytest.skip("RC_E2E=1 not set")
    if not _terraform_usable():
        pytest.skip("terraform binary required for e2e")
    if not _aws_creds_ok():
        pytest.skip(f"AWS creds not usable for region {REGION}")
    ok, reason = _test_region_is_empty()
    if not ok:
        pytest.skip(
            f"test region not clean: {reason} — run scripts/reap_test_region.py"
        )


@pytest.fixture
def test_project_name() -> str:
    return f"rc-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def e2e_ctx(e2e_preconditions, test_project_name, tmp_path) -> DeployContext:
    return DeployContext(
        project=test_project_name,
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={"version": 2, "project": test_project_name},
        provider_config={
            "ecs": {
                "region": REGION,
                "cluster": f"{test_project_name}-cluster",
                "vpc_cidr": "10.99.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                replicas=1,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/",
            ),
        },
        secrets=[],
    )


@pytest.fixture
def provider() -> ECSProvider:
    return ECSProvider()


@pytest.fixture
def e2e_lifecycle(e2e_ctx: DeployContext, provider: ECSProvider):
    """Yield the context, then teardown: destroy + reap."""
    yield e2e_ctx

    keep = os.environ.get("RC_E2E_KEEP_ON_FAIL") == "1"
    try:
        provider.destroy(e2e_ctx)
    except Exception as exc:
        print(f"provider.destroy failed: {exc}", file=sys.stderr)
        if keep:
            print(
                "RC_E2E_KEEP_ON_FAIL=1 — leaving resources for inspection",
                file=sys.stderr,
            )
            return

    # Belt-and-suspenders: run the reap script regardless.
    result = subprocess.run(
        [sys.executable, str(REAP_SCRIPT), "--region", REGION],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        print(f"reap script exited {result.returncode}", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
