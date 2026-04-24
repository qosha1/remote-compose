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
