"""Addon CFN-template detection in copilot import (rc-e5u.43.8).

Copilot apps frequently declare extra AWS resources (RDS, S3, DynamoDB,
ElastiCache, ...) as CFN templates under copilot/<svc>/addons/*.yml.
First-cut translation does NOT rewrite these into rc.yml — that's a
separate concern per resource type. The import summary must surface them
with per-type guidance so the user knows what to do next.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.copilot.discover import discover
from remote_compose.copilot.translate import (
    _ADDON_RESOURCE_GUIDANCE,
    _addon_resource_types,
    compose_app,
)


def _scaffold(tmp_path: Path, addons: dict[str, str]) -> Path:
    """Create a minimal copilot/ tree with one Backend Service ('api') and
    the given addon files keyed by stem -> CFN template body."""
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "manifest.yml").write_text(
        "name: api\ntype: Backend Service\nimage:\n  port: 8000\n"
    )
    addons_dir = tmp_path / "api" / "addons"
    addons_dir.mkdir()
    for name, body in addons.items():
        (addons_dir / f"{name}.yml").write_text(body)
    return tmp_path


# ---------------------------------------------------------------------------
# _addon_resource_types extractor
# ---------------------------------------------------------------------------


class TestAddonResourceTypes:
    def test_extracts_single_type(self, tmp_path):
        _scaffold(tmp_path, {"db": (
            "Resources:\n  DB:\n    Type: AWS::RDS::DBInstance\n"
        )})
        app = discover(tmp_path)
        addon = app.services[0].addons[0]
        assert _addon_resource_types(addon) == ["AWS::RDS::DBInstance"]

    def test_extracts_multiple_types(self, tmp_path):
        _scaffold(tmp_path, {"mixed": (
            "Resources:\n"
            "  DB:\n    Type: AWS::RDS::DBInstance\n"
            "  Bucket:\n    Type: AWS::S3::Bucket\n"
            "  Policy:\n    Type: AWS::IAM::ManagedPolicy\n"
        )})
        app = discover(tmp_path)
        addon = app.services[0].addons[0]
        types = _addon_resource_types(addon)
        assert "AWS::RDS::DBInstance" in types
        assert "AWS::S3::Bucket" in types
        assert "AWS::IAM::ManagedPolicy" in types

    def test_empty_when_no_resources_block(self, tmp_path):
        _scaffold(tmp_path, {"weird": "Parameters:\n  X:\n    Type: String\n"})
        app = discover(tmp_path)
        addon = app.services[0].addons[0]
        assert _addon_resource_types(addon) == []


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


class TestAddonSummary:
    def test_summary_lists_addon_section(self, tmp_path):
        _scaffold(tmp_path, {"db": (
            "Resources:\n  DB:\n    Type: AWS::RDS::DBInstance\n"
        )})
        result = compose_app(discover(tmp_path))
        assert "Addon templates detected: 1" in result.summary
        assert "manual translation required" in result.summary

    def test_summary_groups_by_aws_resource_type(self, tmp_path):
        _scaffold(tmp_path, {
            "db": "Resources:\n  DB:\n    Type: AWS::RDS::DBInstance\n",
            "media": "Resources:\n  B:\n    Type: AWS::S3::Bucket\n",
        })
        result = compose_app(discover(tmp_path))
        assert "AWS::RDS::DBInstance" in result.summary
        assert "AWS::S3::Bucket" in result.summary
        # Each section names its source addon path.
        assert "api/db.yml" in result.summary
        assert "api/media.yml" in result.summary

    def test_summary_includes_guidance_for_known_types(self, tmp_path):
        _scaffold(tmp_path, {
            "db": "Resources:\n  DB:\n    Type: AWS::RDS::DBInstance\n",
        })
        result = compose_app(discover(tmp_path))
        # The RDS guidance string from _ADDON_RESOURCE_GUIDANCE shows up.
        assert _ADDON_RESOURCE_GUIDANCE["AWS::RDS::DBInstance"] in result.summary

    def test_summary_falls_back_to_generic_for_unknown_types(self, tmp_path):
        _scaffold(tmp_path, {
            "weird": "Resources:\n  X:\n    Type: AWS::WeirdNew::Thing\n",
        })
        result = compose_app(discover(tmp_path))
        assert "AWS::WeirdNew::Thing" in result.summary
        assert "not yet auto-handled" in result.summary

    def test_summary_no_addon_section_when_none(self, tmp_path):
        _scaffold(tmp_path, addons={})  # service exists, no addons
        result = compose_app(discover(tmp_path))
        assert "Addon templates detected" not in result.summary

    def test_summary_handles_addons_with_no_resources_block(self, tmp_path):
        # CFN templates that are pure Parameters/Outputs (no Resources)
        # land in the 'with no parseable Resources' bucket.
        _scaffold(tmp_path, {
            "wat": "Parameters:\n  X:\n    Type: String\n",
        })
        result = compose_app(discover(tmp_path))
        assert "Addon templates detected: 1" in result.summary
        assert "no parseable Resources block" in result.summary
        assert "api/wat.yml" in result.summary


# ---------------------------------------------------------------------------
# Per-type guidance map sanity
# ---------------------------------------------------------------------------


class TestGuidanceMapShape:
    @pytest.mark.parametrize("rt", [
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBCluster",
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::ElastiCache::CacheCluster",
        "AWS::ElastiCache::ReplicationGroup",
        "AWS::SQS::Queue",
        "AWS::SNS::Topic",
        "AWS::SecretsManager::Secret",
        "AWS::IAM::Role",
    ])
    def test_common_resource_types_have_guidance(self, rt):
        assert rt in _ADDON_RESOURCE_GUIDANCE
        assert _ADDON_RESOURCE_GUIDANCE[rt]


# ---------------------------------------------------------------------------
# Real-fixture: sentinal corpus has an IAM addon — verify it surfaces
# ---------------------------------------------------------------------------


CORPUS = Path(__file__).parent.parent.parent / "fixtures" / "copilot"


class TestSentinalCorpusAddon:
    def test_sentinal_browser_iam_addon_in_summary(self):
        sentinal = CORPUS / "sentinal"
        if not sentinal.is_dir():
            pytest.skip("sentinal corpus not present")
        result = compose_app(discover(sentinal))
        # Addon is AWS::IAM::ManagedPolicy on backend-celery-browser.
        assert "Addon templates detected" in result.summary
        assert "AWS::IAM::ManagedPolicy" in result.summary
        assert "backend-celery-browser/s3-browser-mgr-media.yml" in result.summary
