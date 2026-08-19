"""Existing-ALB adopt support (rc-adopt D4).

rc normally creates an ALB + listeners + alb security group. When
``provider_config.ecs.existing_alb`` is set, rc references a live ALB + its
HTTPS listener instead — adding host-based listener RULES + per-service target
groups onto the existing listener. Required to adopt a stack already fronted by
an ALB (e.g. browser-mgr's Copilot ALB behind a Namecheap CNAME) without a DNS
flip.

GENERAL + opt-in + strictly ADDITIVE: with no ``existing_alb`` the emitted
terraform is byte-identical (guarded by test_golden.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider

_ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:"
    "loadbalancer/app/browse-Publi-zc7tlO4ZlkmK/abc123"
)
_LISTENER_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:"
    "listener/app/browse-Publi-zc7tlO4ZlkmK/abc123/def456"
)
EXISTING_ALB = {
    "arn": _ALB_ARN,
    "https_listener_arn": _LISTENER_ARN,
}


def _ctx(tmp_path: Path, ecs_overrides: dict | None = None, **over) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "browser-mgr-prod",
        "vpc_id": "vpc-0b6967",
        "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
        "security_group_ids": ["sg-013b"],
        "route53_zone": "debugg.ai",
    }
    ecs_cfg.update(ecs_overrides or {})
    services = over.pop("services", None) or {
        "django": ServiceSpec(
            name="django",
            cpu=512,
            memory=1024,
            type="application",
            public=True,
            port=5000,
            domain="browser-mgr.debugg.ai",
            health_check_path="/api/health/",
        ),
    }
    return DeployContext(
        project="browser-mgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={
            "tls": {"mode": "manual", "certificate_arn": "arn:aws:acm:x:y:cert/z"}
        },
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _emit(tmp_path, **over):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(
        _ctx(tmp_path, {"existing_alb": EXISTING_ALB}, **over), out
    )
    return out


class TestExistingAlbEmission:
    def test_data_sources_emitted_no_lb_resource(self, tmp_path):
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        assert 'data "aws_lb" "main"' in alb
        assert f'arn = "{_ALB_ARN}"' in alb
        assert 'data "aws_lb_listener" "https"' in alb
        assert f'arn = "{_LISTENER_ARN}"' in alb
        # rc creates no ALB and no rc-managed listeners.
        assert 'resource "aws_lb" "main"' not in alb
        assert 'resource "aws_lb_listener"' not in alb

    def test_listener_rule_attaches_to_existing_listener(self, tmp_path):
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        # Per-service TG + rule still created, but onto the existing listener.
        assert 'resource "aws_lb_target_group" "django"' in alb
        assert "listener_arn = data.aws_lb_listener.https.arn" in alb
        assert 'values = ["browser-mgr.debugg.ai"]' in alb

    def test_no_alb_security_group_tasks_ingress_from_existing(self, tmp_path):
        sg = (_emit(tmp_path) / "security_groups.tf").read_text()
        assert 'resource "aws_security_group" "alb"' not in sg
        # tasks ingress comes from the existing ALB's own security groups.
        assert "security_groups = data.aws_lb.main.security_groups" in sg

    def test_outputs_and_r53_use_data_source(self, tmp_path):
        out = _emit(tmp_path)
        assert "value = data.aws_lb.main.dns_name" in (out / "outputs.tf").read_text()
        domain = (out / "domain.tf").read_text()
        assert "name                   = data.aws_lb.main.dns_name" in domain
        assert "zone_id                = data.aws_lb.main.zone_id" in domain

    def test_service_depends_on_listener_rule(self, tmp_path):
        services = (_emit(tmp_path) / "services.tf").read_text()
        assert "depends_on = [aws_lb_listener_rule.django]" in services


class TestExistingAlbValidation:
    def test_requires_arn_and_listener(self, tmp_path):
        with pytest.raises(
            ProviderConfigError, match="arn.*https_listener_arn|requires both"
        ):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, {"existing_alb": {"arn": _ALB_ARN}}), tmp_path / "tf"
            )

    def test_ec2_launch_type_admits_existing_alb_security_groups(self, tmp_path):
        svcs = {
            "django": ServiceSpec(
                name="django",
                cpu=512,
                memory=1024,
                type="application",
                public=True,
                port=5000,
                domain="browser-mgr.debugg.ai",
                health_check_path="/api/health/",
                launch_type="EC2",
            ),
        }
        out = _emit(tmp_path, services=svcs)
        capacity = (out / "capacity.tf").read_text()
        # ec2_instances SG ingress admits the existing ALB's own security
        # groups (read off the data source) -- there is no rc-created
        # aws_security_group.alb to reference in existing_alb mode.
        assert "security_groups = data.aws_lb.main.security_groups" in capacity
        assert "aws_security_group.alb.id" not in capacity

    def test_public_service_without_domain_rejected(self, tmp_path):
        svcs = {
            "django": ServiceSpec(
                name="django",
                cpu=512,
                memory=1024,
                type="application",
                public=True,
                port=5000,
                health_check_path="/api/health/",
            ),  # public but no domain
        }
        with pytest.raises(
            ProviderConfigError, match="existing_alb requires every public"
        ):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, {"existing_alb": EXISTING_ALB}, services=svcs),
                tmp_path / "tf",
            )


class TestExistingAlbAcmCertificateAttachment:
    """rc-4tkc: with an ADOPTED listener, rc must still attach the cert it minted.

    Every existing test in this file runs tls mode=manual, where the operator's
    certificate is already sitting on the listener they told rc to adopt. That
    hid a gap in the ACM path: rc emits aws_acm_certificate +
    aws_acm_certificate_validation for the service's domain, but the only place
    a certificate is ever bound to a listener is `certificate_arn` on the
    rc-created `aws_lb_listener "https"` — which the existing_alb branch
    deliberately does not emit.

    Net effect: the cert is requested, DNS-validated, and then never attached.
    TLS to that hostname serves whatever default certificate the adopted
    listener carries, so the client gets a name mismatch.

    This blocks putting several tenants behind one shared ALB, which is the
    whole point of adopting a listener: each tenant needs its own SNI cert on
    the shared listener.
    """

    def _acm_ctx(self, tmp_path, **over):
        ctx = _ctx(tmp_path, {"existing_alb": EXISTING_ALB}, **over)
        ctx.rc_yml_v2 = {"tls": {"mode": "acm"}}  # rc mints the cert itself
        return ctx

    def _emit_acm(self, tmp_path, **over):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._acm_ctx(tmp_path, **over), out)
        return out

    def test_minted_cert_is_attached_to_the_adopted_listener(self, tmp_path):
        out = self._emit_acm(tmp_path)
        alb = (out / "alb.tf").read_text()

        assert 'resource "aws_lb_listener_certificate"' in alb, (
            "rc minted an ACM cert for the domain but never attached it to the "
            "adopted listener — TLS serves the listener's default cert instead"
        )
        assert "listener_arn    = data.aws_lb_listener.https.arn" in alb
        assert (
            "certificate_arn = aws_acm_certificate_validation.main.certificate_arn"
            in alb
        )

    def test_still_no_rc_managed_listener(self, tmp_path):
        """The attachment must not smuggle back a listener resource."""
        alb = (self._emit_acm(tmp_path) / "alb.tf").read_text()
        assert 'resource "aws_lb_listener" "https"' not in alb
        assert 'resource "aws_lb" "main"' not in alb

    def test_manual_tls_is_unchanged(self, tmp_path):
        """Manual mode must stay byte-identical. The operator's cert is already
        on the listener they adopted; adding it again as an SNI cert fails in
        AWS when it is that listener's DEFAULT certificate."""
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        assert 'resource "aws_lb_listener_certificate"' not in alb

    def test_not_emitted_without_existing_alb(self, tmp_path):
        """Without adoption rc owns the listener and sets certificate_arn on it
        directly — a separate attachment would be redundant."""
        out = tmp_path / "tf2"
        ctx = _ctx(tmp_path)
        ctx.rc_yml_v2 = {"tls": {"mode": "acm"}}
        ECSProvider().emit_terraform(ctx, out)
        alb = (out / "alb.tf").read_text()
        assert 'resource "aws_lb_listener_certificate"' not in alb
        assert (
            "certificate_arn   = aws_acm_certificate_validation.main.certificate_arn"
            in alb
        )
