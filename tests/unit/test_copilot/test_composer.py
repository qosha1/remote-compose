"""compose_app: wire all the translators together into rc.yml + compose.

The composer is the public entry point for `rc copilot import`. It
takes a parsed CopilotApp + optional env name + optional project
name, runs the translators per service, and returns an ImportResult
with the rc.yml v2 dict, the docker-compose dict, the warnings list,
and a human-readable summary.

Composer never writes files — that's the CLI's job — so it's pure +
fully testable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.copilot.discover import discover
from remote_compose.copilot.translate import (
    ImportResult,
    UnsupportedServiceTypeWarning,
    compose_app,
)

CORPUS = Path(__file__).parent.parent.parent / "fixtures" / "copilot"


class TestComposerBasics:
    def test_returns_importresult(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\nimage:\n  port: 8001\ncpu: 256\nmemory: 512\n"
        )
        result = compose_app(discover(tmp_path), project="myapp")
        assert isinstance(result, ImportResult)
        assert result.rc_yml["project"] == "myapp"
        assert result.rc_yml["version"] == 2
        assert result.rc_yml["provider"] == "ecs"

    def test_default_project_when_unset(self, tmp_path):
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "manifest.yml").write_text(
            "name: svc\ntype: Backend Service\n"
        )
        result = compose_app(discover(tmp_path))
        # Falls back to the parent dir name (or 'myapp' if root-named).
        assert result.rc_yml["project"]


class TestRcYmlServices:
    def test_each_copilot_service_appears_in_rc_yml(self, tmp_path):
        for name, type_ in [("api", "Backend Service"), ("worker", "Worker Service")]:
            (tmp_path / name).mkdir()
            (tmp_path / name / "manifest.yml").write_text(
                f"name: {name}\ntype: {type_}\ncpu: 512\nmemory: 1024\n"
                + ("image:\n  port: 8001\n" if type_ != "Worker Service" else "")
            )
        result = compose_app(discover(tmp_path), project="m")
        assert sorted(result.rc_yml["services"].keys()) == ["api", "worker"]

    def test_worker_marked_type_worker(self, tmp_path):
        (tmp_path / "celery").mkdir()
        (tmp_path / "celery" / "manifest.yml").write_text(
            "name: celery\ntype: Worker Service\ncpu: 512\nmemory: 1024\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        assert result.rc_yml["services"]["celery"]["type"] == "worker"

    def test_lbws_marked_public(self, tmp_path):
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "manifest.yml").write_text(
            "name: web\ntype: Load Balanced Web Service\n"
            "image:\n  port: 80\n"
            "http:\n  alias: app.example.com\n"
            "cpu: 256\nmemory: 512\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        s = result.rc_yml["services"]["web"]
        assert s["public"] is True
        assert s["port"] == 80
        assert s["domain"] == "app.example.com"

    def test_static_site_excluded_via_compose_block(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\nimage:\n  port: 8001\n"
        )
        (tmp_path / "site").mkdir()
        (tmp_path / "site" / "manifest.yml").write_text(
            "name: site\ntype: Static Site\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        # Static Site is _skip=True from translate_service_type → excluded.
        assert "site" not in result.rc_yml["services"]
        # And it's listed in compose.exclude so the user knows it was
        # intentionally skipped (not lost to a parser bug).
        assert "site" in (result.rc_yml.get("compose") or {}).get("exclude", [])
        # An UnsupportedServiceTypeWarning is captured.
        assert any(
            isinstance(w, UnsupportedServiceTypeWarning) for w in result.warnings
        )


class TestComposeFile:
    def test_compose_services_match_rc_services(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\n"
            "image:\n  build: ./api\n  port: 8001\n"
            "variables:\n  FOO: bar\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        assert "api" in result.docker_compose["services"]
        assert result.docker_compose["services"]["api"]["build"] == "./api"
        assert result.docker_compose["services"]["api"]["environment"]["FOO"] == "bar"


class TestSecrets:
    def test_aws_sm_secrets_collected_into_top_level_secrets(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\nimage:\n  port: 8001\n"
            "secrets:\n  DB_PW:\n    secretsmanager: arn:aws:secretsmanager:::secret:dbpw\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        secs = result.rc_yml["secrets"]
        assert any(s["name"] == "DB_PW" and s["source"] == "aws_sm" for s in secs)


class TestEnvironmentSelection:
    def test_env_overrides_applied_when_passed(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\n"
            "image:\n  port: 8001\n"
            "cpu: 256\nmemory: 512\n"
            "environments:\n  production:\n    cpu: 2048\n    memory: 4096\n"
        )
        result = compose_app(discover(tmp_path), project="m", env="production")
        assert result.rc_yml["services"]["api"]["cpu"] == 2048
        assert result.rc_yml["services"]["api"]["memory"] == 4096

    def test_no_env_uses_base_manifest_values(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\nimage:\n  port: 8001\n"
            "cpu: 256\nmemory: 512\n"
            "environments:\n  production:\n    cpu: 2048\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        assert result.rc_yml["services"]["api"]["cpu"] == 256


class TestSummary:
    def test_summary_lists_translated_services(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "manifest.yml").write_text(
            "name: api\ntype: Backend Service\nimage:\n  port: 8001\n"
        )
        (tmp_path / "site").mkdir()
        (tmp_path / "site" / "manifest.yml").write_text(
            "name: site\ntype: Static Site\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        assert "api" in result.summary
        assert "Static Site" in result.summary or "site" in result.summary

    def test_summary_groups_warnings_by_kind(self, tmp_path):
        # Two services trigger UnsupportedServiceTypeWarning; they should
        # appear together in the summary so the user can scan them at once.
        (tmp_path / "site1").mkdir()
        (tmp_path / "site1" / "manifest.yml").write_text(
            "name: site1\ntype: Static Site\n"
        )
        (tmp_path / "site2").mkdir()
        (tmp_path / "site2" / "manifest.yml").write_text(
            "name: site2\ntype: Static Site\n"
        )
        result = compose_app(discover(tmp_path), project="m")
        assert (
            "Unsupported service type" in result.summary
            or "UnsupportedServiceType" in result.summary
        )


class TestCorpusGenerality:
    @pytest.mark.parametrize(
        "fixture,subdir",
        [
            ("sentinal", ""),
            ("external-shanikaediriweera", ""),
            ("aws-cli-app-with-domain", "copilot"),
        ],
    )
    def test_corpus_app_composes_without_crash(self, fixture, subdir):
        path = CORPUS / fixture
        if subdir:
            path = path / subdir
        if not path.exists():
            pytest.skip(f"missing fixture {fixture}")
        result = compose_app(discover(path), project=fixture)
        # Always produces an rc.yml + compose dict.
        assert "version" in result.rc_yml
        assert "services" in result.docker_compose
