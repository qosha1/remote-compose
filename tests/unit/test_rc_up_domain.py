"""Tests for rc up --domain (rc-e5u.46.7).

Wires a custom FQDN onto the scaffolded rc.yml: services.<public>.domain
+ aliases + provider_config.ecs.route53_zone. Pre-flight verifies the
Route 53 hosted zone exists in the configured aws_profile.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from remote_compose.init_from_compose import (
    _patch_rc_yml_domain,
    _zone_from_domain_drop_leftmost,
)

# ---------------------------------------------------------------------------
# _zone_from_domain_drop_leftmost
# ---------------------------------------------------------------------------


class TestZoneFromDomainDropLeftmost:
    @pytest.mark.parametrize(
        "domain,expected",
        [
            ("startsimpli-test.rctest.ezapps.ai", "rctest.ezapps.ai"),
            ("api.example.com", "example.com"),
            ("example.com", "example.com"),  # apex unchanged
            ("a.b.c.d.e", "b.c.d.e"),  # drops only one label
            ("ezapps.ai.", "ezapps.ai"),  # trailing dot stripped
        ],
    )
    def test_drops_leftmost_label(self, domain, expected):
        assert _zone_from_domain_drop_leftmost(domain) == expected


# ---------------------------------------------------------------------------
# _patch_rc_yml_domain
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, public_service: str = "nginx") -> Path:
    rc = {
        "version": 2,
        "project": "test-46-7",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {"region": "us-west-1"}},
        "terraform": {"backend": {"type": "local"}},
        "services": {
            "django": {"cpu": 256, "memory": 512, "type": "application"},
            public_service: {
                "cpu": 256,
                "memory": 512,
                "type": "proxy",
                "public": True,
                "port": 80,
            },
        },
    }
    rc_path = tmp_path / "rc.yml"
    rc_path.write_text(
        "# header line\n" "# another comment\n\n" + yaml.safe_dump(rc, sort_keys=False)
    )
    return rc_path


class TestPatchRcYmlDomain:
    def test_wires_domain_and_zone_on_public_service(self, tmp_path):
        rc_path = _scaffold(tmp_path)
        wired = _patch_rc_yml_domain(
            rc_path,
            domain="startsimpli-test.rctest.ezapps.ai",
            aliases=[],
        )

        out = yaml.safe_load(rc_path.read_text())
        assert out["services"]["nginx"]["domain"] == "startsimpli-test.rctest.ezapps.ai"
        assert out["provider_config"]["ecs"]["route53_zone"] == "rctest.ezapps.ai"
        assert wired["public_service"] == "nginx"
        assert wired["route53_zone"] == "rctest.ezapps.ai"

    def test_wires_aliases_when_provided(self, tmp_path):
        rc_path = _scaffold(tmp_path)
        _patch_rc_yml_domain(
            rc_path,
            domain="app.example.com",
            aliases=["api.example.com", "www.app.example.com"],
        )
        out = yaml.safe_load(rc_path.read_text())
        assert out["services"]["nginx"]["aliases"] == [
            "api.example.com",
            "www.app.example.com",
        ]

    def test_aliases_dedupes_and_filters_self(self, tmp_path):
        rc_path = _scaffold(tmp_path)
        _patch_rc_yml_domain(
            rc_path,
            domain="app.example.com",
            aliases=["api.example.com", "api.example.com", "app.example.com"],
        )
        out = yaml.safe_load(rc_path.read_text())
        # Self-reference dropped, dup dropped.
        assert out["services"]["nginx"]["aliases"] == ["api.example.com"]

    def test_route53_zone_override_wins(self, tmp_path):
        rc_path = _scaffold(tmp_path)
        _patch_rc_yml_domain(
            rc_path,
            domain="a.b.c.d",
            aliases=[],
            route53_zone="weird-zone.example",
        )
        out = yaml.safe_load(rc_path.read_text())
        assert out["provider_config"]["ecs"]["route53_zone"] == "weird-zone.example"

    def test_preserves_header_comments(self, tmp_path):
        rc_path = _scaffold(tmp_path)
        _patch_rc_yml_domain(rc_path, domain="app.example.com", aliases=[])
        text = rc_path.read_text()
        assert text.startswith("# header line\n# another comment\n")

    def test_idempotent_re_runs_produce_same_output(self, tmp_path):
        rc_path = _scaffold(tmp_path)
        _patch_rc_yml_domain(
            rc_path, domain="app.example.com", aliases=["x.app.example.com"]
        )
        first = rc_path.read_text()
        _patch_rc_yml_domain(
            rc_path, domain="app.example.com", aliases=["x.app.example.com"]
        )
        second = rc_path.read_text()
        assert first == second

    def test_no_public_service_raises(self, tmp_path):
        # Strip public:true from the helper-scaffolded rc.yml.
        rc_path = _scaffold(tmp_path)
        raw = yaml.safe_load(rc_path.read_text())
        del raw["services"]["nginx"]["public"]
        rc_path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError, match="no public service"):
            _patch_rc_yml_domain(rc_path, domain="x.example.com", aliases=[])


# ---------------------------------------------------------------------------
# rc up --domain CLI integration
# ---------------------------------------------------------------------------


class TestRcUpDomainFlag:
    """End-to-end: --domain pre-flight + scaffold-patch sequence inside rc up."""

    def _run_rc_up(self, runner, target_path, *flags, zones=None):
        """Helper to invoke rc up --from-compose with patches in place.

        The rc up command does a lot beyond domain wiring; we patch out the
        downstream steps to keep the test focused on the .46.7 surface.
        """
        from click.testing import CliRunner  # noqa: F401  (fixture-style)
        from remote_compose.cli import cli

        # Mock Route 53 zone lookup.
        zones = (
            zones
            if zones is not None
            else [
                {"Name": "rctest.ezapps.ai."},
                {"Name": "example.com."},
            ]
        )

        with (
            patch("boto3.Session") as boto_session,
            patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True),
            patch("remote_compose.cli._secrets_push_v2", return_value=True),
            patch("remote_compose.cli_v2.run_auto_on_deploy_hooks_for_path"),
        ):
            r53 = MagicMock()
            r53.list_hosted_zones.return_value = {"HostedZones": zones}
            session = MagicMock()
            session.client.return_value = r53
            boto_session.return_value = session
            return runner.invoke(cli, ["-c", str(target_path), "up"] + list(flags))

    def test_alias_without_domain_errors(self, tmp_path):
        from click.testing import CliRunner
        from remote_compose.cli import cli

        runner = CliRunner()
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text("version: 2\nproject: x\n")
        result = runner.invoke(
            cli,
            [
                "-c",
                str(rc_path),
                "up",
                "--alias",
                "extra.example.com",
            ],
        )
        assert result.exit_code != 0
        assert "--alias requires --domain" in result.output

    def test_domain_with_unknown_zone_errors(self, tmp_path, monkeypatch):
        """Pre-flight rejects --domain when its derived zone isn't present
        in the user's Route 53."""
        from click.testing import CliRunner
        from remote_compose.cli import cli

        runner = CliRunner()
        # Pre-existing rc.yml so we don't go through scaffold.
        rc_path = _scaffold(tmp_path)

        with patch("boto3.Session") as boto_session:
            r53 = MagicMock()
            r53.list_hosted_zones.return_value = {
                "HostedZones": [
                    {"Name": "other-zone.example."},
                ]
            }
            session = MagicMock()
            session.client.return_value = r53
            boto_session.return_value = session
            result = runner.invoke(
                cli,
                [
                    "-c",
                    str(rc_path),
                    "up",
                    "--domain",
                    "x.rctest.ezapps.ai",
                ],
            )

        assert result.exit_code != 0
        assert "Route 53 hosted zone" in result.output
        assert "rctest.ezapps.ai" in result.output

    def test_route53_zone_override_bypasses_default_lookup(self, tmp_path):
        """--route53-zone overrides the drop-leftmost default; pre-flight
        verifies the OVERRIDE zone, not the default."""
        from click.testing import CliRunner
        from remote_compose.cli import cli

        runner = CliRunner()
        rc_path = _scaffold(tmp_path)

        with (
            patch("boto3.Session") as boto_session,
            patch("remote_compose.cli_v2.dispatch_if_v2", return_value=True),
            patch("remote_compose.cli._secrets_push_v2", return_value=True),
            patch("remote_compose.cli_v2.run_auto_on_deploy_hooks_for_path"),
        ):
            r53 = MagicMock()
            # Only the OVERRIDE zone is present; the leftmost-drop derivation
            # would have failed.
            r53.list_hosted_zones.return_value = {
                "HostedZones": [
                    {"Name": "weird-zone.example."},
                ]
            }
            session = MagicMock()
            session.client.return_value = r53
            boto_session.return_value = session
            result = runner.invoke(
                cli,
                [
                    "-c",
                    str(rc_path),
                    "up",
                    "--domain",
                    "a.b.c.d.e",
                    "--route53-zone",
                    "weird-zone.example",
                ],
            )

        assert result.exit_code == 0, result.output
        out = yaml.safe_load(rc_path.read_text())
        assert out["provider_config"]["ecs"]["route53_zone"] == "weird-zone.example"
