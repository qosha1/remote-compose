"""Unit tests for rc.yml v2 schema parsing and validation."""

from __future__ import annotations

import pytest

from remote_compose.config.v2_schema import ConfigError, parse


def _minimal() -> dict:
    return {
        "version": 2,
        "project": "myapp",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "services": {
            "web": {"cpu": 256, "memory": 512, "type": "proxy",
                    "public": True, "port": 80},
        },
    }


class TestParse:
    def test_minimal_config_parses(self):
        cfg = parse(_minimal())
        assert cfg.version == 2
        assert cfg.project == "myapp"
        assert cfg.provider == "ecs"
        assert "web" in cfg.services

    def test_missing_version_rejected(self):
        raw = _minimal()
        del raw["version"]
        with pytest.raises(ConfigError, match="version"):
            parse(raw)

    def test_wrong_version_rejected(self):
        raw = _minimal()
        raw["version"] = 1
        with pytest.raises(ConfigError, match="version"):
            parse(raw)

    def test_missing_project_rejected(self):
        raw = _minimal()
        del raw["project"]
        with pytest.raises(ConfigError, match="project"):
            parse(raw)

    def test_missing_provider_rejected(self):
        raw = _minimal()
        del raw["provider"]
        with pytest.raises(ConfigError, match="provider"):
            parse(raw)

    def test_invalid_service_type_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["type"] = "bogus"
        with pytest.raises(ConfigError, match="type"):
            parse(raw)

    def test_invalid_launch_type_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["launch_type"] = "BOGUS"
        with pytest.raises(ConfigError, match="launch_type"):
            parse(raw)

    def test_public_without_port_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["public"] = True
        raw["services"]["web"].pop("port", None)
        with pytest.raises(ConfigError, match="port"):
            parse(raw)

    def test_launch_type_fargate_accepted(self):
        raw = _minimal()
        raw["services"]["web"]["launch_type"] = "FARGATE"
        parse(raw)

    def test_launch_type_ec2_accepted(self):
        raw = _minimal()
        raw["services"]["web"]["launch_type"] = "EC2"
        parse(raw)


class TestSecrets:
    def _with_secrets(self, secrets: list) -> dict:
        raw = _minimal()
        raw["secrets"] = secrets
        return raw

    def test_file_secret_ok(self):
        parse(self._with_secrets([
            {"name": "django", "source": "file", "path": "/etc/secrets/.django"},
        ]))

    def test_aws_sm_secret_ok(self):
        parse(self._with_secrets([
            {"name": "db", "source": "aws_sm",
             "arn": "arn:aws:secretsmanager:us-west-2:1:secret:db"},
        ]))

    def test_k8s_secret_ok(self):
        parse(self._with_secrets([
            {"name": "app", "source": "k8s_secret", "ref": "app-creds"},
        ]))

    def test_file_secret_without_path_rejected(self):
        with pytest.raises(ConfigError, match="path"):
            parse(self._with_secrets([
                {"name": "x", "source": "file"},
            ]))

    def test_aws_sm_without_arn_rejected(self):
        with pytest.raises(ConfigError, match="arn"):
            parse(self._with_secrets([
                {"name": "x", "source": "aws_sm"},
            ]))

    def test_unknown_source_rejected(self):
        with pytest.raises(ConfigError, match="source"):
            parse(self._with_secrets([
                {"name": "x", "source": "bogus"},
            ]))


class TestTls:
    def test_acm_default(self):
        raw = _minimal()
        raw["tls"] = {"mode": "acm"}
        cfg = parse(raw)
        assert cfg.tls is not None
        assert cfg.tls.mode == "acm"

    def test_manual_requires_arn(self):
        raw = _minimal()
        raw["tls"] = {"mode": "manual"}
        with pytest.raises(ConfigError, match="certificate_arn"):
            parse(raw)

    def test_invalid_mode_rejected(self):
        raw = _minimal()
        raw["tls"] = {"mode": "letsencrypt"}
        with pytest.raises(ConfigError, match="tls.mode"):
            parse(raw)


class TestServiceDomain:
    """Per-service domain enables ALB host-based routing (e.g. api.foo.com
    -> django, docs.foo.com -> docs). Validation: domain requires
    public=true; must look like an FQDN."""

    def test_domain_parses_when_set(self):
        raw = _minimal()
        raw["services"]["web"]["domain"] = "api.example.com"
        cfg = parse(raw)
        assert cfg.services["web"].domain == "api.example.com"

    def test_no_domain_means_none(self):
        cfg = parse(_minimal())
        assert cfg.services["web"].domain is None

    def test_domain_on_private_service_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["public"] = False
        raw["services"]["web"].pop("port", None)
        raw["services"]["web"]["domain"] = "api.example.com"
        with pytest.raises(ConfigError, match="public=true"):
            parse(raw)

    def test_invalid_fqdn_rejected(self):
        for bad in ["not a domain", "no..dots.com", "trailing.dot.", "-bad.com"]:
            raw = _minimal()
            raw["services"]["web"]["domain"] = bad
            with pytest.raises(ConfigError, match="domain"):
                parse(raw)

    def test_apex_domain_accepted(self):
        raw = _minimal()
        raw["services"]["web"]["domain"] = "example.com"
        cfg = parse(raw)
        assert cfg.services["web"].domain == "example.com"

    def test_two_services_can_have_distinct_domains(self):
        raw = _minimal()
        raw["services"]["api"] = {
            "cpu": 256, "memory": 512, "type": "application",
            "public": True, "port": 8080,
            "domain": "api.example.com",
        }
        raw["services"]["web"]["domain"] = "example.com"
        cfg = parse(raw)
        assert cfg.services["api"].domain == "api.example.com"
        assert cfg.services["web"].domain == "example.com"

    def test_duplicate_domain_across_services_rejected(self):
        raw = _minimal()
        raw["services"]["api"] = {
            "cpu": 256, "memory": 512, "type": "application",
            "public": True, "port": 8080,
            "domain": "shared.example.com",
        }
        raw["services"]["web"]["domain"] = "shared.example.com"
        with pytest.raises(ConfigError, match="duplicate"):
            parse(raw)


class TestComposeBlock:
    """rc.yml v2 'compose' top-level block: include/exclude lists for
    auto-importing services from the docker-compose file."""

    def test_no_compose_block_means_default(self):
        cfg = parse(_minimal())
        assert cfg.compose is None

    def test_exclude_list_parses(self):
        raw = _minimal()
        raw["compose"] = {"exclude": ["ngrok", "eval-app"]}
        cfg = parse(raw)
        assert cfg.compose.exclude == ["ngrok", "eval-app"]
        assert cfg.compose.include is None

    def test_include_list_parses(self):
        raw = _minimal()
        raw["compose"] = {"include": ["django", "postgres"]}
        cfg = parse(raw)
        assert cfg.compose.include == ["django", "postgres"]
        assert cfg.compose.exclude is None

    def test_both_include_and_exclude_rejected(self):
        raw = _minimal()
        raw["compose"] = {"include": ["a"], "exclude": ["b"]}
        with pytest.raises(ConfigError, match="mutually exclusive"):
            parse(raw)

    def test_exclude_must_be_list(self):
        raw = _minimal()
        raw["compose"] = {"exclude": "not-a-list"}
        with pytest.raises(ConfigError, match="exclude.*list"):
            parse(raw)

    def test_include_must_be_list(self):
        raw = _minimal()
        raw["compose"] = {"include": "not-a-list"}
        with pytest.raises(ConfigError, match="include.*list"):
            parse(raw)

    def test_unknown_compose_keys_rejected(self):
        raw = _minimal()
        raw["compose"] = {"exclude": [], "mystery": True}
        with pytest.raises(ConfigError, match="unknown compose key"):
            parse(raw)


class TestServiceAliases:
    """services[*].aliases: extra hostnames for the SAME service. Used
    when a single fronting service (nginx, traefik) handles multiple
    hostnames application-side. Aliases get cert SANs + R53 records but
    no ALB listener rules — the default action catches them."""

    def test_aliases_parses(self):
        raw = _minimal()
        raw["services"]["web"]["domain"] = "foo.example.com"
        raw["services"]["web"]["aliases"] = ["api.example.com", "docs.example.com"]
        cfg = parse(raw)
        assert cfg.services["web"].aliases == ["api.example.com", "docs.example.com"]

    def test_no_aliases_means_empty_list(self):
        cfg = parse(_minimal())
        assert cfg.services["web"].aliases == []

    def test_aliases_on_private_service_rejected(self):
        raw = _minimal()
        raw["services"]["worker"] = {
            "cpu": 256, "memory": 512, "type": "worker",
            "aliases": ["alt.example.com"],
        }
        with pytest.raises(ConfigError, match="aliases.*public=true"):
            parse(raw)

    def test_alias_overlapping_own_domain_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["domain"] = "foo.example.com"
        raw["services"]["web"]["aliases"] = ["foo.example.com"]
        with pytest.raises(ConfigError, match="alias.*own domain"):
            parse(raw)

    def test_alias_overlapping_other_service_domain_rejected(self):
        raw = _minimal()
        raw["services"]["api"] = {
            "cpu": 256, "memory": 512, "type": "application",
            "public": True, "port": 8080, "domain": "api.example.com",
        }
        raw["services"]["web"]["domain"] = "web.example.com"
        raw["services"]["web"]["aliases"] = ["api.example.com"]
        with pytest.raises(ConfigError, match="duplicate"):
            parse(raw)

    def test_alias_overlapping_other_service_alias_rejected(self):
        raw = _minimal()
        raw["services"]["api"] = {
            "cpu": 256, "memory": 512, "type": "application",
            "public": True, "port": 8080, "domain": "api.example.com",
            "aliases": ["shared.example.com"],
        }
        raw["services"]["web"]["domain"] = "web.example.com"
        raw["services"]["web"]["aliases"] = ["shared.example.com"]
        with pytest.raises(ConfigError, match="duplicate"):
            parse(raw)

    def test_malformed_alias_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["domain"] = "foo.example.com"
        raw["services"]["web"]["aliases"] = ["not a domain"]
        with pytest.raises(ConfigError, match="alias"):
            parse(raw)

    def test_aliases_must_be_list(self):
        raw = _minimal()
        raw["services"]["web"]["domain"] = "foo.example.com"
        raw["services"]["web"]["aliases"] = "single.example.com"
        with pytest.raises(ConfigError, match="aliases.*list"):
            parse(raw)


class TestLifecycle:
    def test_no_lifecycle_block_is_empty_dict(self):
        cfg = parse(_minimal())
        assert cfg.services["web"].lifecycle == {}

    def test_basic_lifecycle_hook_parses(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {
            "migrate": {"command": ["python", "manage.py", "migrate"]},
        }
        cfg = parse(raw)
        hook = cfg.services["web"].lifecycle["migrate"]
        assert hook.command == ["python", "manage.py", "migrate"]
        assert hook.auto_on_deploy is False
        assert hook.run_once is False
        assert hook.interactive is False
        assert hook.probe is None

    def test_auto_on_deploy_flag(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {
            "migrate": {"command": ["./bin/migrate"], "auto_on_deploy": True},
        }
        cfg = parse(raw)
        assert cfg.services["web"].lifecycle["migrate"].auto_on_deploy is True

    def test_run_once_with_probe_parses(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {
            "createsuperuser": {
                "command": ["python", "manage.py", "createsuperuser", "--noinput"],
                "run_once": True,
                "probe": ["sh", "-c", "test $(...) -gt 0"],
            },
        }
        cfg = parse(raw)
        hook = cfg.services["web"].lifecycle["createsuperuser"]
        assert hook.run_once is True
        assert hook.probe == ["sh", "-c", "test $(...) -gt 0"]

    def test_interactive_hook(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {
            "shell": {"command": ["python", "manage.py", "shell"], "interactive": True},
        }
        cfg = parse(raw)
        assert cfg.services["web"].lifecycle["shell"].interactive is True

    def test_empty_command_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {"x": {"command": []}}
        with pytest.raises(ConfigError, match="non-empty list"):
            parse(raw)

    def test_non_list_command_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {"x": {"command": "echo hi"}}
        with pytest.raises(ConfigError, match="non-empty list"):
            parse(raw)

    def test_run_once_without_probe_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {
            "x": {"command": ["true"], "run_once": True},
        }
        with pytest.raises(ConfigError, match="run_once requires"):
            parse(raw)

    def test_auto_on_deploy_with_interactive_rejected(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = {
            "x": {"command": ["true"], "auto_on_deploy": True, "interactive": True},
        }
        with pytest.raises(ConfigError, match="cannot be interactive"):
            parse(raw)

    def test_lifecycle_must_be_mapping(self):
        raw = _minimal()
        raw["services"]["web"]["lifecycle"] = ["migrate", "shell"]
        with pytest.raises(ConfigError, match="must be a mapping"):
            parse(raw)
