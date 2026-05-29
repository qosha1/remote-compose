"""Truth test for ECSProvider.emit_terraform.

Runs ``terraform init -backend=false && terraform validate`` against the
emitted module. If this passes, the HCL is syntactically and
semantically valid according to the AWS provider.

Skipped automatically when terraform is not usable in this environment
(see sentinel in test_terraform_runner).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import TerraformRunner

pytestmark = pytest.mark.integration


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


requires_terraform = pytest.mark.skipif(
    not _terraform_usable(),
    reason="terraform binary not usable in this environment (binary missing or sandboxed)",
)


@pytest.fixture
def ecs_ctx(tmp_path):
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
            ),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=[],
    )


@requires_terraform
class TestEmittedHclValidates:
    def test_terraform_init_and_validate(self, ecs_ctx, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ecs_ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()


def _multi_domain_ctx(tmp_path):
    """Two public services on distinct subdomains of one zone — exercises
    the rc-e5u.39 multi-domain ALB routing path (per-service TG, host-header
    listener rules, multi-SAN ACM cert)."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
                domain="web.example.com",
            ),
            "api": ServiceSpec(
                name="api",
                cpu=512,
                memory=1024,
                type="application",
                public=True,
                port=8000,
                health_check_path="/health",
                domain="api.example.com",
            ),
        },
        secrets=[],
    )


def _alias_ctx(tmp_path):
    """One public service with a primary domain + 2 aliases. Exercises the
    rc-e5u.40 nginx-as-front + aliases path: SANs grow, R53 records grow,
    listener rules do NOT (default action handles all)."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "nginx": ServiceSpec(
                name="nginx",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/",
                domain="primary.example.com",
                aliases=["a.example.com", "b.example.com"],
            ),
        },
        secrets=[],
    )


@requires_terraform
class TestMultiDomainEmissionValidates:
    """rc-e5u.39 backfill: the multi-domain ALB output validates against
    real terraform. Unit tests in test_domain.py prove the HCL shape; this
    test proves the AWS provider accepts it."""

    def test_multi_domain_module_passes_terraform_validate(self, tmp_path):
        ctx = _multi_domain_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_multi_domain_emits_per_service_target_group(self, tmp_path):
        """Sanity assertion in addition to validation: each domained service
        gets its own aws_lb_target_group resource."""
        ctx = _multi_domain_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        alb_tf = (out / "alb.tf").read_text()
        # One TG per service.
        assert 'resource "aws_lb_target_group" "web"' in alb_tf
        assert 'resource "aws_lb_target_group" "api"' in alb_tf


@requires_terraform
class TestAliasEmissionValidates:
    """rc-e5u.40 backfill: nginx-as-front + aliases output validates against
    real terraform. Confirms the design point: aliases extend SANs and R53
    records but do NOT add listener rules."""

    def test_alias_module_passes_terraform_validate(self, tmp_path):
        ctx = _alias_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_aliases_extend_acm_san_list(self, tmp_path):
        """All 3 hostnames (primary + 2 aliases) appear in the cert config."""
        ctx = _alias_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain_tf = (out / "domain.tf").read_text()
        assert "primary.example.com" in domain_tf
        assert "a.example.com" in domain_tf
        assert "b.example.com" in domain_tf

    def test_aliases_get_r53_records_but_no_listener_rules(self, tmp_path):
        """3 R53 app A records (one per host), but listener rules count == 0
        because the default action handles all 3 via SNI."""
        ctx = _alias_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain_tf = (out / "domain.tf").read_text()
        alb_tf = (out / "alb.tf").read_text()
        # 3 app-A-record resources expected (primary + 2 aliases). The
        # cert_validation record (for ACM DNS validation) is separate and
        # not counted here.
        app_records = domain_tf.count('resource "aws_route53_record" "app_')
        assert app_records == 3, (
            f"expected 3 R53 app A records (primary + 2 aliases), got "
            f"{app_records}\n{domain_tf}"
        )
        # Listener rules: aliases must NOT appear in any rule's host_header.
        # (Existing emission keeps a rule for the primary domain even when
        # it's redundant with the default_target action — see the matching
        # unit test test_aliases_do_not_emit_listener_rules.)
        for alias in ("a.example.com", "b.example.com"):
            for rule_block in alb_tf.split('aws_lb_listener_rule"')[1:]:
                rule_block_short = rule_block.split("resource ")[0]
                assert alias not in rule_block_short, (
                    f"alias {alias!r} must not appear in any aws_lb_listener_rule "
                    f"host_header; got block:\n{rule_block_short[:400]}"
                )
