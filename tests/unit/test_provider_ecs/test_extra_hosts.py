"""Per-service ``extra_hosts`` — host-header routing only (startsim-bwdx.3.2).

On a SHARED adopted listener (``existing_alb``) the default action belongs to
nobody, so a hostname that today reaches a service only because "the default
action catches it" — e.g. a ``*.287a.com`` wildcard CNAME'd from Cloudflare —
needs an explicit host rule. ``aliases`` cannot carry it: every alias also
becomes an ACM SAN and a Route53 record in ``route53_zone``, and such zones are
not in Route53. ``extra_hosts`` adds ONLY host_header values to the service's
listener rule: no cert, no DNS.
"""

from __future__ import annotations

import pytest

from remote_compose.config._schema_types import ConfigError, ServiceV2
from remote_compose.provider import ServiceSpec

from .test_existing_alb import _ctx, _emit, EXISTING_ALB  # noqa: F401
from remote_compose.provider.ecs import ECSProvider


def _svc(**over):
    kw = dict(
        name="django", cpu=512, memory=1024, type="application", public=True,
        port=5000, domain="browser-mgr.debugg.ai", health_check_path="/api/health/",
    )
    kw.update(over)
    return {"django": ServiceSpec(**kw)}


def test_extra_hosts_join_the_listener_rule(tmp_path):
    alb = (_emit(tmp_path, services=_svc(extra_hosts=["*.287a.com"])) / "alb.tf").read_text()
    assert 'values = ["browser-mgr.debugg.ai", "*.287a.com"]' in alb


def test_extra_hosts_add_no_cert_san_and_no_dns(tmp_path):
    out = _emit(tmp_path, services=_svc(extra_hosts=["*.287a.com"]))
    text = "".join(p.read_text() for p in out.glob("*.tf"))
    assert "287a.com" not in (out / "domain.tf").read_text()
    assert text.count("287a.com") == 1   # the rule condition, nowhere else


def test_without_extra_hosts_the_rule_is_unchanged(tmp_path):
    alb = (_emit(tmp_path) / "alb.tf").read_text()
    assert 'values = ["browser-mgr.debugg.ai"]' in alb


def test_extra_hosts_also_route_on_an_owned_alb(tmp_path):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services=_svc(extra_hosts=["*.x.io"])), out)
    assert 'values = ["browser-mgr.debugg.ai", "*.x.io"]' in (out / "alb.tf").read_text()


def _cfg(**over):
    kw = dict(name="nginx", cpu=256, memory=512, public=True, port=80, domain="a.example.com")
    kw.update(over)
    return ServiceV2(**kw)


@pytest.mark.parametrize("bad", [
    "*.287a.com",                       # not a list
    ["not a host"],
    ["a.example.com"],                  # duplicates the domain
    ["h1.x.io", "h2.x.io", "h3.x.io", "h4.x.io", "h5.x.io"],  # >4: AWS caps a rule at 5 values
])
def test_invalid_extra_hosts_refused(bad):
    with pytest.raises(ConfigError, match="extra_hosts"):
        _cfg(extra_hosts=bad).validate()


def test_extra_hosts_need_a_domain():
    with pytest.raises(ConfigError, match="extra_hosts"):
        _cfg(domain=None, extra_hosts=["*.x.io"]).validate()


def test_valid_extra_hosts_accepted():
    _cfg(extra_hosts=["*.287a.com", "b.example.com"]).validate()
