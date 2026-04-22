"""Unit tests for ECS custom domain + ACM + Route 53 (Phase 6b.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import _zone_from_domain


def _ctx(tmp_path: Path, **overrides) -> DeployContext:
    services = overrides.pop("services", None) or {
        "web": ServiceSpec(name="web", cpu=256, memory=512, type="proxy",
                           public=True, port=80, health_check_path="/"),
    }
    rc_yml = overrides.pop("rc_yml_v2", {}) or {}
    domain = overrides.pop("domain", None)
    tls = overrides.pop("tls", None)
    if domain and "domain" not in rc_yml:
        rc_yml["domain"] = domain
    if tls and "tls" not in rc_yml:
        rc_yml["tls"] = tls
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc_yml,
        provider_config={"ecs": {
            "region": "us-west-2", "cluster": "test", "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


class TestZoneFromDomain:
    def test_subdomain(self):
        assert _zone_from_domain("api.example.com") == "example.com"

    def test_apex(self):
        assert _zone_from_domain("example.com") == "example.com"

    def test_deep_subdomain(self):
        assert _zone_from_domain("a.b.c.example.com") == "example.com"


class TestNoDomain:
    def test_domain_tf_empty(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        assert (out / "domain.tf").read_text().strip() == ""

    def test_alb_has_http_only(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        assert 'aws_lb_listener" "http"' in alb
        assert 'aws_lb_listener" "https"' not in alb
        assert "aws_acm_certificate" not in alb


class TestAcmDomain:
    def test_acm_certificate_resource_rendered(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="api.example.com"), out,
        )
        domain = (out / "domain.tf").read_text()
        assert 'aws_acm_certificate" "main"' in domain
        assert 'validation_method = "DNS"' in domain
        assert 'aws_acm_certificate_validation" "main"' in domain

    def test_route53_zone_derived_from_domain(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="api.example.com"), out,
        )
        domain = (out / "domain.tf").read_text()
        assert 'name         = "example.com"' in domain

    def test_route53_a_record_points_at_alb(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="api.example.com"), out,
        )
        domain = (out / "domain.tf").read_text()
        assert 'aws_route53_record" "app"' in domain
        assert "aws_lb.main.dns_name" in domain
        assert "alias {" in domain

    def test_alb_has_https_listener(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="api.example.com"), out,
        )
        alb = (out / "alb.tf").read_text()
        assert 'aws_lb_listener" "https"' in alb
        assert 'port              = 443' in alb
        assert "aws_acm_certificate_validation.main.certificate_arn" in alb

    def test_http_listener_redirects_to_https(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="api.example.com"), out,
        )
        alb = (out / "alb.tf").read_text()
        assert 'aws_lb_listener" "http"' in alb
        assert "type = \"redirect\"" in alb
        assert 'status_code = "HTTP_301"' in alb


class TestManualTls:
    def test_manual_mode_uses_provided_certificate_arn(self, tmp_path):
        arn = "arn:aws:acm:us-west-2:111122223333:certificate/abcd-1234"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="api.example.com",
                 tls={"mode": "manual", "certificate_arn": arn}),
            out,
        )
        alb = (out / "alb.tf").read_text()
        domain = (out / "domain.tf").read_text()
        assert arn in alb
        assert 'aws_acm_certificate" "main"' not in domain  # no ACM resource

    def test_manual_mode_without_arn_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="certificate_arn"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, domain="api.example.com", tls={"mode": "manual"}),
                tmp_path / "tf",
            )


class TestValidation:
    def test_domain_without_public_service_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, domain="api.example.com", services={
            "worker": ServiceSpec(name="worker", cpu=256, memory=512, type="worker"),
        })
        with pytest.raises(ProviderConfigError, match="public"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_unsupported_tls_mode_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="tls.mode"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, domain="api.example.com",
                     tls={"mode": "cert-manager"}),
                tmp_path / "tf",
            )


class TestEcsCfgDomainFallback:
    def test_provider_config_ecs_domain_works(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.provider_config["ecs"]["domain"] = "api.example.com"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain = (out / "domain.tf").read_text()
        assert 'aws_acm_certificate" "main"' in domain

    def test_ecs_cfg_wins_over_top_level(self, tmp_path):
        ctx = _ctx(tmp_path, rc_yml_v2={"domain": "toplevel.example.com"})
        ctx.provider_config["ecs"]["domain"] = "ecs.example.com"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain = (out / "domain.tf").read_text()
        assert '"ecs.example.com"' in domain
        assert '"toplevel.example.com"' not in domain
