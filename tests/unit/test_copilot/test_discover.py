"""Discover + parse a copilot/ tree into a typed CopilotApp model.

Tests are corpus-driven — they run against tests/fixtures/copilot/* so
the parser is proven against real third-party manifests, not just
sentinal's quirks. Adding a new fixture should never require parser
changes for the common shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.copilot.discover import (
    CopilotApp,
    CopilotEnvironment,
    CopilotService,
    DiscoveryError,
    discover,
)

CORPUS = Path(__file__).parent.parent.parent / "fixtures" / "copilot"


def _fixture(name: str) -> Path:
    p = CORPUS / name
    if not p.exists():
        pytest.skip(f"fixture {name!r} not in corpus")
    return p


# ---------------------------------------------------------------------
# discover() returns CopilotApp on a real fixture
# ---------------------------------------------------------------------


class TestDiscoverSentinal:
    """sentinal: 15 services, 3 envs, addons, pipelines."""

    def test_discovers_all_services(self):
        app = discover(_fixture("sentinal"))
        names = sorted(s.name for s in app.services)
        assert "backend-django" in names
        assert "backend-celery-worker" in names
        assert "nginx" in names
        assert "frontend" in names
        # 15 manifests in copilot/<svc>/manifest.yml under sentinal
        assert len(app.services) >= 14

    def test_discovers_environments(self):
        app = discover(_fixture("sentinal"))
        env_names = sorted(e.name for e in app.environments)
        assert env_names == ["dev", "production", "staging"]

    def test_each_service_carries_its_type(self):
        app = discover(_fixture("sentinal"))
        by_name = {s.name: s for s in app.services}
        assert by_name["backend-django"].type == "Backend Service"
        assert by_name["backend-celery-worker"].type == "Worker Service"
        assert by_name["nginx"].type == "Load Balanced Web Service"

    def test_pipelines_detected_but_separated_from_services(self):
        # copilot/pipelines/* are not services; the parser must not
        # treat them as such.
        app = discover(_fixture("sentinal"))
        names = {s.name for s in app.services}
        assert not any(n.startswith("trouvai-web") for n in names)

    def test_addons_indexed_per_service(self):
        # backend-celery-browser has copilot/<svc>/addons/s3-browser-mgr-media.yml
        app = discover(_fixture("sentinal"))
        by_name = {s.name: s for s in app.services}
        addons = by_name["backend-celery-browser"].addons
        assert any(
            "s3-browser-mgr-media" in a.name for a in addons
        ), f"expected an s3-browser-mgr-media addon, got {[a.name for a in addons]}"


# ---------------------------------------------------------------------
# discover() handles a small external real-world app
# ---------------------------------------------------------------------


class TestDiscoverShanika:
    def test_two_services_two_envs_one_pipeline(self):
        app = discover(_fixture("external-shanikaediriweera"))
        assert sorted(s.name for s in app.services) == ["service1", "service2"]
        assert sorted(e.name for e in app.environments) == ["dev", "test"]


# ---------------------------------------------------------------------
# discover() handles aws/copilot-cli e2e fixtures (canonical shapes)
# ---------------------------------------------------------------------


class TestDiscoverCanonicalShapes:
    def test_app_with_domain_lbws_pair(self):
        # aws-cli e2e fixtures snapshot the parent dir, so pass copilot/
        # explicitly.
        app = discover(_fixture("aws-cli-app-with-domain") / "copilot")
        types = sorted(s.type for s in app.services)
        assert "Load Balanced Web Service" in types

    def test_static_site_type_recognized(self):
        app = discover(_fixture("aws-cli-static-site") / "copilot")
        types = [s.type for s in app.services]
        assert "Static Site" in types


# ---------------------------------------------------------------------
# discover() error handling
# ---------------------------------------------------------------------


class TestDiscoveryErrors:
    def test_missing_dir(self, tmp_path):
        with pytest.raises(DiscoveryError, match="not found"):
            discover(tmp_path / "nope")

    def test_empty_dir(self, tmp_path):
        with pytest.raises(DiscoveryError, match="no service manifests"):
            discover(tmp_path)

    def test_service_dir_without_manifest_skipped_with_warning(self, tmp_path):
        # A directory under copilot/ that has no manifest.yml is not a
        # service. Discovery must skip it, not error.
        (tmp_path / "myservice" / "addons").mkdir(parents=True)
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "manifest.yml").write_text(
            "name: real\ntype: Backend Service\n"
        )
        app = discover(tmp_path)
        names = [s.name for s in app.services]
        assert "real" in names
        assert "myservice" not in names

    def test_malformed_manifest_yaml_raises(self, tmp_path):
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "manifest.yml").write_text("not: valid: yaml: at all")
        with pytest.raises(DiscoveryError, match="broken"):
            discover(tmp_path)

    def test_manifest_missing_name_raises(self, tmp_path):
        (tmp_path / "noname").mkdir()
        (tmp_path / "noname" / "manifest.yml").write_text("type: Backend Service\n")
        with pytest.raises(DiscoveryError, match="missing.*name"):
            discover(tmp_path)

    def test_manifest_missing_type_raises(self, tmp_path):
        (tmp_path / "notype").mkdir()
        (tmp_path / "notype" / "manifest.yml").write_text("name: notype\n")
        with pytest.raises(DiscoveryError, match="missing.*type"):
            discover(tmp_path)


# ---------------------------------------------------------------------
# CopilotApp model accessors
# ---------------------------------------------------------------------


class TestCopilotAppModel:
    def test_app_dataclass_shape(self, tmp_path):
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "manifest.yml").write_text(
            "name: svc\ntype: Backend Service\n"
        )
        (tmp_path / "environments" / "prod").mkdir(parents=True)
        (tmp_path / "environments" / "prod" / "manifest.yml").write_text(
            "name: prod\ntype: Environment\n"
        )
        app = discover(tmp_path)
        assert isinstance(app, CopilotApp)
        assert isinstance(app.services[0], CopilotService)
        assert isinstance(app.environments[0], CopilotEnvironment)
        # Both keep their raw dict for translators to consult.
        assert app.services[0].raw["name"] == "svc"
        assert app.environments[0].raw["name"] == "prod"

    def test_service_lookup_by_name(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "manifest.yml").write_text("name: a\ntype: Backend Service\n")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "manifest.yml").write_text("name: b\ntype: Worker Service\n")
        app = discover(tmp_path)
        assert app.service("a").type == "Backend Service"
        assert app.service("b").type == "Worker Service"
        with pytest.raises(KeyError):
            app.service("missing")

    def test_environment_lookup_by_name(self, tmp_path):
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "manifest.yml").write_text(
            "name: svc\ntype: Backend Service\n"
        )
        (tmp_path / "environments" / "dev").mkdir(parents=True)
        (tmp_path / "environments" / "dev" / "manifest.yml").write_text(
            "name: dev\ntype: Environment\n"
        )
        app = discover(tmp_path)
        assert app.environment("dev").name == "dev"
        with pytest.raises(KeyError):
            app.environment("nope")
