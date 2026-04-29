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
        # Multi-domain rewrite numbers app records by index.
        assert 'aws_route53_record" "app_1"' in domain
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


class TestRoute53ZoneOverride:
    """The 2-label _zone_from_domain heuristic fails when the R53 zone
    is a delegated subdomain (e.g. api.startsimpli.com delegated, but
    startsimpli.com not held by the account). The route53_zone override
    lets users bypass the heuristic."""

    def test_explicit_zone_wins_over_heuristic(self, tmp_path):
        ctx = _ctx(tmp_path,
                   domain="migration-test.api.startsimpli.com")
        ctx.provider_config["ecs"]["route53_zone"] = "api.startsimpli.com"
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain = (out / "domain.tf").read_text()
        assert 'name         = "api.startsimpli.com"' in domain
        assert 'name         = "startsimpli.com"' not in domain

    def test_no_override_falls_back_to_heuristic(self, tmp_path):
        ctx = _ctx(tmp_path,
                   domain="migration-test.api.startsimpli.com")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain = (out / "domain.tf").read_text()
        # Heuristic strips to last two labels — not what we want for nested
        # zones, hence the override.
        assert 'name         = "startsimpli.com"' in domain


class TestPerServiceDomain:
    """services[*].domain enables ALB host-based routing — each public
    service with a domain gets its own target group, listener rule, R53
    record, and is added to the ACM cert SANs. The catch-all
    default_target service still receives traffic that doesn't match
    any host rule."""

    def _multidomain_ctx(self, tmp_path):
        return DeployContext(
            project="multi",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={"ecs": {
                "region": "us-west-2",
                "cluster": "multi-cluster",
                "vpc_cidr": "10.0.0.0/16",
                "route53_zone": "example.com",
            }},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                "django": ServiceSpec(
                    name="django", cpu=512, memory=1024, type="application",
                    public=True, port=8001, health_check_path="/api/health/",
                    domain="api.example.com",
                ),
                "docs": ServiceSpec(
                    name="docs", cpu=256, memory=512, type="application",
                    public=True, port=9000, health_check_path="/",
                    domain="docs.example.com",
                ),
                "nginx": ServiceSpec(
                    name="nginx", cpu=256, memory=512, type="proxy",
                    public=True, port=80, health_check_path="/",
                    domain="example.com",
                ),
                "worker": ServiceSpec(
                    name="worker", cpu=256, memory=512, type="worker",
                ),
            },
            secrets=[],
        )

    def test_one_target_group_per_domained_service(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        assert 'aws_lb_target_group" "django"' in alb
        assert 'aws_lb_target_group" "docs"' in alb
        assert 'aws_lb_target_group" "nginx"' in alb
        # Worker is not public — no TG.
        assert 'aws_lb_target_group" "worker"' not in alb

    def test_listener_rule_per_service_with_host_header(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        assert 'aws_lb_listener_rule" "django"' in alb
        assert "host_header" in alb
        assert '"api.example.com"' in alb
        assert '"docs.example.com"' in alb
        assert '"example.com"' in alb

    def test_per_service_health_check_path_used(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        # django uses /api/health/, docs+nginx use /
        assert '"/api/health/"' in alb
        django_tg = alb.split('"aws_lb_target_group" "django"')[1].split("resource ")[0]
        assert "/api/health/" in django_tg
        docs_tg = alb.split('"aws_lb_target_group" "docs"')[1].split("resource ")[0]
        assert '"/"' in docs_tg

    def test_route53_record_per_service_domain(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        domain = (out / "domain.tf").read_text()
        # One A-record per service domain.
        for d in ("api.example.com", "docs.example.com", "example.com"):
            assert f'"{d}"' in domain

    def test_acm_cert_includes_subject_alternative_names(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        domain = (out / "domain.tf").read_text()
        assert "subject_alternative_names" in domain
        # Apex picked alphabetically first, others in SANs.
        assert '"api.example.com"' in domain
        assert '"docs.example.com"' in domain
        assert '"example.com"' in domain

    def test_ecs_service_attaches_to_its_own_target_group(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        services_tf = (out / "services.tf").read_text()
        # django service block must point at aws_lb_target_group.django.arn,
        # not the catch-all default.
        django_svc = services_tf.split('aws_ecs_service" "django"')[1].split("resource ")[0]
        assert "aws_lb_target_group.django.arn" in django_svc

    def test_listener_rules_have_distinct_priorities(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._multidomain_ctx(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        import re as _re
        priorities = _re.findall(r"priority\s*=\s*(\d+)", alb)
        assert len(priorities) == len(set(priorities)), (
            f"listener rule priorities must be distinct, got {priorities}"
        )

    def test_default_target_service_with_domain_serves_default_action(self, tmp_path):
        """When the default_target service ALSO declares its own domain,
        the listener's default action must still resolve to a TG that
        actually has the service attached. Regression for sentinal apex
        503: django was attached only to aws_lb_target_group.django,
        leaving aws_lb_target_group.default empty so unmatched hosts
        (the apex) returned 503."""
        ctx = DeployContext(
            project="combo",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={"ecs": {
                "region": "us-west-2", "cluster": "combo",
                "vpc_cidr": "10.0.0.0/16",
                "domain": "apex.example.com",
                "route53_zone": "example.com",
            }},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                # Only public service → automatically the default target.
                "django": ServiceSpec(
                    name="django", cpu=512, memory=1024, type="application",
                    public=True, port=8001,
                    domain="api.example.com",
                ),
            },
            secrets=[],
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        alb = (out / "alb.tf").read_text()
        # Listener default action goes through the local; the local must
        # resolve to django's TG (the one ECS actually registers).
        assert "default_target_group_arn = aws_lb_target_group.django.arn" in alb
        # And we must not emit a separate empty aws_lb_target_group.default
        # since django's TG IS the default.
        assert 'aws_lb_target_group" "default"' not in alb

    def test_backward_compat_single_domain_still_works(self, tmp_path):
        # Old shape: only provider_config.ecs.domain set, no per-service
        # domain. Should still emit the original single-domain ALB+ACM+R53
        # without breaking.
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, domain="legacy.example.com"), out,
        )
        domain = (out / "domain.tf").read_text()
        assert "aws_acm_certificate" in domain
        assert "legacy.example.com" in domain


class TestServiceAliases:
    """Aliases on a service add ACM cert SANs + R53 records but DO NOT
    emit ALB listener rules — the default action handles them. This is
    the nginx-as-front pattern: one fronting service answers to many
    hostnames; routing happens application-side."""

    def _ctx_with_alias_front(self, tmp_path):
        return DeployContext(
            project="front",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={"ecs": {
                "region": "us-west-2", "cluster": "front",
                "vpc_cidr": "10.0.0.0/16",
                "route53_zone": "example.com",
            }},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                "nginx": ServiceSpec(
                    name="nginx", cpu=256, memory=512, type="proxy",
                    public=True, port=80, health_check_path="/health",
                    domain="example.com",
                    aliases=["api.example.com", "docs.example.com"],
                ),
                "django": ServiceSpec(
                    name="django", cpu=512, memory=1024, type="application",
                    # Private — accessed by nginx via service-discovery DNS.
                ),
            },
            secrets=[],
        )

    def test_aliases_add_to_cert_sans(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._ctx_with_alias_front(tmp_path), out)
        domain = (out / "domain.tf").read_text()
        assert "subject_alternative_names" in domain
        for d in ("example.com", "api.example.com", "docs.example.com"):
            assert f'"{d}"' in domain

    def test_aliases_get_r53_records(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._ctx_with_alias_front(tmp_path), out)
        domain = (out / "domain.tf").read_text()
        # Three A-records: primary + 2 aliases.
        import re as _re
        record_count = len(_re.findall(r'aws_route53_record" "app_\d+"', domain))
        assert record_count == 3, f"expected 3 app A-records, got {record_count}"

    def test_aliases_do_not_emit_listener_rules(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._ctx_with_alias_front(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        # Service has aliases but no ALB rule per alias. The single rule
        # for nginx (its own domain) is also redundant since nginx is
        # default_target — verify no listener_rule mentions an alias hostname.
        for alias in ("api.example.com", "docs.example.com"):
            # The alias name MAY appear in the cert/SAN comment, but no
            # aws_lb_listener_rule should host_header on it.
            for rule_block in alb.split('aws_lb_listener_rule"')[1:]:
                rule_block_short = rule_block.split("resource ")[0]
                assert alias not in rule_block_short, (
                    f"alias {alias} should not appear in any listener_rule"
                )

    def test_aliases_count_toward_default_target_logic(self, tmp_path):
        # With nginx as the only public service + aliases, nginx's TG IS
        # the default. No separate aws_lb_target_group.default needed.
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(self._ctx_with_alias_front(tmp_path), out)
        alb = (out / "alb.tf").read_text()
        assert "default_target_group_arn = aws_lb_target_group.nginx.arn" in alb
