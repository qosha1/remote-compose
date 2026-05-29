"""Translate Copilot service.type → rc.yml v2 service shape hints.

Mapping (per Copilot docs https://aws.github.io/copilot-cli/docs/manifest/):
    Backend Service              → private (no public), default type=application
    Worker Service               → type=worker, no public, no port
    Load Balanced Web Service    → public=true, port from image.port, http.alias→domain
    Request-Driven Web Service   → AWS App Runner runtime; mark as unsupported on ECS
    Static Site                  → CloudFront+S3; mark as unsupported on ECS

Tests are corpus-driven where possible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.copilot.discover import CopilotService, discover
from remote_compose.copilot.translate import (
    UnsupportedServiceTypeWarning,
    translate_service_type,
)

CORPUS = Path(__file__).parent.parent.parent / "fixtures" / "copilot"


def _svc(raw: dict) -> CopilotService:
    return CopilotService(
        name=raw.get("name", "x"),
        type=raw["type"],
        manifest_path=Path("/dev/null"),
        raw=raw,
    )


# ---------------------------------------------------------------------
# Backend Service: private, no public flag
# ---------------------------------------------------------------------


class TestBackendService:
    def test_no_public_flag(self):
        out, warnings = translate_service_type(
            _svc(
                {
                    "name": "api",
                    "type": "Backend Service",
                    "image": {"port": 8001},
                }
            )
        )
        assert out.get("public") is None or out["public"] is False
        assert warnings == []

    def test_port_carried_through_when_present(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "api",
                    "type": "Backend Service",
                    "image": {"port": 5000},
                }
            )
        )
        assert out["port"] == 5000

    def test_no_port_when_image_has_none(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "api",
                    "type": "Backend Service",
                    "image": {},
                }
            )
        )
        assert out.get("port") is None

    def test_health_check_path_extracted_from_image_healthcheck_when_present(self):
        # Copilot 'image.healthcheck.command' is for container HC, but
        # a Backend Service can also expose 'http.healthcheck.path' for
        # the ALB. Both should be honored when present.
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "api",
                    "type": "Backend Service",
                    "image": {"port": 8001},
                    "http": {"healthcheck": {"path": "/api/health"}},
                }
            )
        )
        assert out["health_check_path"] == "/api/health"


# ---------------------------------------------------------------------
# Worker Service: never public, no port, type=worker
# ---------------------------------------------------------------------


class TestWorkerService:
    def test_marked_as_worker(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "celery",
                    "type": "Worker Service",
                    "image": {},
                }
            )
        )
        assert out["type"] == "worker"
        assert out.get("public") is None or out["public"] is False
        assert out.get("port") is None

    def test_port_ignored_even_if_set(self):
        # Workers shouldn't expose ports even if Copilot has one in
        # the manifest (Copilot itself ignores it for Worker Service).
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "celery",
                    "type": "Worker Service",
                    "image": {"port": 9999},
                }
            )
        )
        assert out.get("port") is None


# ---------------------------------------------------------------------
# Load Balanced Web Service: public, port required, http.alias → domain
# ---------------------------------------------------------------------


class TestLoadBalancedWebService:
    def test_marked_public_with_port(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "front-end",
                    "type": "Load Balanced Web Service",
                    "image": {"port": 80},
                }
            )
        )
        assert out["public"] is True
        assert out["port"] == 80

    def test_http_alias_becomes_domain(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "front-end",
                    "type": "Load Balanced Web Service",
                    "image": {"port": 80},
                    "http": {"alias": "app.example.com"},
                }
            )
        )
        assert out["domain"] == "app.example.com"

    def test_http_alias_list_first_becomes_domain_rest_aliases(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "front-end",
                    "type": "Load Balanced Web Service",
                    "image": {"port": 80},
                    "http": {"alias": ["app.example.com", "www.example.com"]},
                }
            )
        )
        assert out["domain"] == "app.example.com"
        assert out["aliases"] == ["www.example.com"]

    def test_http_path_becomes_health_check_path(self):
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "front-end",
                    "type": "Load Balanced Web Service",
                    "image": {"port": 80},
                    "http": {"path": "/", "healthcheck": "/health"},
                }
            )
        )
        assert out["health_check_path"] == "/health"

    def test_default_target_set_for_lbws_when_no_alias(self):
        # An LBWS with no alias should still be reachable as the catch-
        # all default target.
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "front-end",
                    "type": "Load Balanced Web Service",
                    "image": {"port": 80},
                }
            )
        )
        assert out["default_target"] is True

    def test_missing_port_raises_clear_error(self):
        # LBWS without a port can't be ALB-fronted. Error fast.
        with pytest.raises(ValueError, match="Load Balanced Web Service.*port"):
            translate_service_type(
                _svc(
                    {
                        "name": "front-end",
                        "type": "Load Balanced Web Service",
                        "image": {},
                    }
                )
            )


# ---------------------------------------------------------------------
# Request-Driven Web Service: App Runner, not ECS
# ---------------------------------------------------------------------


class TestRequestDrivenWebService:
    def test_emits_unsupported_warning(self):
        out, warnings = translate_service_type(
            _svc(
                {
                    "name": "rdws",
                    "type": "Request-Driven Web Service",
                    "image": {"port": 8080},
                }
            )
        )
        assert len(warnings) == 1
        assert isinstance(warnings[0], UnsupportedServiceTypeWarning)
        assert "Request-Driven" in warnings[0].message
        assert "App Runner" in warnings[0].message

    def test_returns_partial_translation_for_user_to_review(self):
        # Don't drop the service silently — emit a config they can
        # review and adapt. Mark as public+port from image like LBWS.
        out, _ = translate_service_type(
            _svc(
                {
                    "name": "rdws",
                    "type": "Request-Driven Web Service",
                    "image": {"port": 8080},
                }
            )
        )
        assert out["public"] is True
        assert out["port"] == 8080


# ---------------------------------------------------------------------
# Static Site: CloudFront + S3, distinct runtime
# ---------------------------------------------------------------------


class TestStaticSite:
    def test_emits_unsupported_warning_and_no_service(self):
        out, warnings = translate_service_type(
            _svc(
                {
                    "name": "site",
                    "type": "Static Site",
                }
            )
        )
        assert len(warnings) == 1
        assert isinstance(warnings[0], UnsupportedServiceTypeWarning)
        assert "Static Site" in warnings[0].message
        # Static Sites have no ECS analogue; emit no rc.yml service.
        assert out == {} or out.get("_skip") is True


# ---------------------------------------------------------------------
# Unknown / future Copilot types
# ---------------------------------------------------------------------


class TestUnknownType:
    def test_unknown_type_is_warned_not_crashed(self):
        out, warnings = translate_service_type(
            _svc(
                {
                    "name": "x",
                    "type": "Future Service Type",
                }
            )
        )
        assert any("Future Service Type" in w.message for w in warnings)


# ---------------------------------------------------------------------
# Corpus integration: every fixture's services translate without crash
# ---------------------------------------------------------------------


class TestCorpusGenerality:
    @pytest.mark.parametrize(
        "fixture,subdir",
        [
            ("sentinal", ""),
            ("external-shanikaediriweera", ""),
            ("aws-cli-app-with-domain", "copilot"),
            ("aws-cli-static-site", "copilot"),
        ],
    )
    def test_every_corpus_service_translates_or_warns(self, fixture, subdir):
        path = CORPUS / fixture
        if subdir:
            path = path / subdir
        if not path.exists():
            pytest.skip(f"missing fixture {fixture}")
        app = discover(path)
        for svc in app.services:
            out, warnings = translate_service_type(svc)
            # Either a usable rc.yml dict OR an UnsupportedServiceType
            # warning. Never a crash.
            assert isinstance(out, dict)
            assert all(isinstance(w, UnsupportedServiceTypeWarning) for w in warnings)
