"""Unit tests for the CLI → Provider v2 dispatcher."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import (
    build_deploy_context,
    dispatch_if_v2,
    load_rc_yml,
    resolve_provider,
)


V2_SAMPLE = {
    "version": 2,
    "project": "cli-test",
    "compose_file": "docker-compose.yml",
    "provider": "fake",
    "provider_config": {},
    "services": {
        "web": {"cpu": 256, "memory": 512, "type": "proxy",
                "public": True, "port": 80},
    },
    "terraform": {"backend": {"type": "local"}},
}


V1_SAMPLE = {
    "cluster": "old",
    "region": "us-west-2",
    "compose_file": "docker-compose.yml",
    "project_name": "legacy",
    "services": {"web": {"cpu": 256, "memory": 512, "type": "proxy"}},
}


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data))


class TestLoadRcYml:
    def test_v2_parses(self, tmp_path):
        p = tmp_path / "rc.yml"
        _write(p, V2_SAMPLE)
        version, raw, v2 = load_rc_yml(p)
        assert version == 2
        assert v2 is not None
        assert v2.project == "cli-test"

    def test_v1_detected_without_parse(self, tmp_path):
        p = tmp_path / "rc.yml"
        _write(p, V1_SAMPLE)
        version, raw, v2 = load_rc_yml(p)
        assert version == 1
        assert v2 is None

    def test_missing_version_treated_as_v1(self, tmp_path):
        p = tmp_path / "rc.yml"
        _write(p, {"project_name": "x", "compose_file": "c.yml", "services": {}})
        version, _, v2 = load_rc_yml(p)
        assert version == 1
        assert v2 is None


class TestBuildDeployContext:
    def test_context_mirrors_v2_schema(self, tmp_path):
        p = tmp_path / "rc.yml"
        _write(p, V2_SAMPLE)
        _, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert ctx.project == "cli-test"
        assert "web" in ctx.services
        assert ctx.services["web"].cpu == 256
        assert ctx.services["web"].public is True
        assert ctx.working_dir == tmp_path.resolve()

    def test_relative_compose_path_resolved(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("services:\n  web: {image: nginx}\n")
        p = tmp_path / "rc.yml"
        _write(p, V2_SAMPLE)
        _, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert ctx.compose_path == (tmp_path / "docker-compose.yml").resolve()

    def test_none_backend_fields_stripped(self, tmp_path):
        cfg = dict(V2_SAMPLE)
        cfg["terraform"] = {"backend": {"type": "s3", "bucket": "b", "key": "k.tfstate",
                                         "region": "us-west-2", "dynamodb_table": None}}
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        _, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert "dynamodb_table" not in ctx.tf_backend_config


class TestDispatcher:
    def test_returns_false_for_v1(self, tmp_path, monkeypatch):
        p = tmp_path / "rc.yml"
        _write(p, V1_SAMPLE)
        assert dispatch_if_v2(p, "deploy") is False

    def test_returns_false_when_no_rc_yml(self, tmp_path):
        assert dispatch_if_v2(tmp_path / "missing.yml", "deploy") is False

    def test_v2_plan_dispatches(self, tmp_path, capsys):
        p = tmp_path / "rc.yml"
        _write(p, V2_SAMPLE)
        ok = dispatch_if_v2(p, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "provider=fake" in out
        assert "Terraform plan" in out


class TestResolveProvider:
    def test_fake_resolves(self, tmp_path):
        p = tmp_path / "rc.yml"
        _write(p, V2_SAMPLE)
        _, _, v2 = load_rc_yml(p)
        prov = resolve_provider(v2)
        assert prov.name == "fake"

    def test_unknown_provider_raises_with_hint(self, tmp_path):
        cfg = dict(V2_SAMPLE)
        cfg["provider"] = "azure"
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        _, _, v2 = load_rc_yml(p)
        from remote_compose.provider import ProviderNotFoundError
        with pytest.raises(ProviderNotFoundError, match="azure"):
            resolve_provider(v2)


class TestV2LegacyFlatten:
    """_load_config must surface v2 rc.yml in the flat shape that
    backup/restore/list (and other legacy helpers) expect. This is how
    v2-migrated projects keep access to the existing db backup tooling
    without duplicating the code path per provider."""

    def _v2(self) -> dict:
        return {
            "version": 2,
            "project": "ss-debuggai",
            "compose_file": "docker-compose.ecs.yml",
            "provider": "ecs",
            "provider_config": {"ecs": {
                "cluster": "ss-debuggai-prod",
                "region": "us-west-2",
                "aws_profile": "debuggai",
            }},
            "services": {"django": {"cpu": 1024, "memory": 4096,
                                     "type": "application"}},
            "backup": {"bucket": "ss-debuggai-db-dumps", "service": "django"},
        }

    def test_flatten_exposes_legacy_keys(self):
        from remote_compose.cli import _flatten_v2_to_legacy
        flat = _flatten_v2_to_legacy(self._v2())
        assert flat["project_name"] == "ss-debuggai"
        assert flat["compose_file"] == "docker-compose.ecs.yml"
        assert flat["cluster"] == "ss-debuggai-prod"
        assert flat["region"] == "us-west-2"
        assert flat["aws_profile"] == "debuggai"
        assert flat["backup"] == {"bucket": "ss-debuggai-db-dumps",
                                   "service": "django"}

    def test_load_config_accepts_v2(self, tmp_path, monkeypatch):
        from remote_compose import cli as cli_mod
        p = tmp_path / "rc.yml"
        _write(p, self._v2())
        monkeypatch.chdir(tmp_path)
        cfg = cli_mod._load_config()
        # Legacy commands must be able to pull these without knowing about v2.
        assert cfg["project_name"] == "ss-debuggai"
        assert cfg["cluster"] == "ss-debuggai-prod"
        assert cfg["backup"]["bucket"] == "ss-debuggai-db-dumps"

    def test_load_config_still_accepts_v1(self, tmp_path, monkeypatch):
        from remote_compose import cli as cli_mod
        p = tmp_path / "rc.yml"
        _write(p, V1_SAMPLE)
        monkeypatch.chdir(tmp_path)
        cfg = cli_mod._load_config()
        assert cfg["project_name"] == "legacy"
        assert cfg["cluster"] == "old"


class TestComposeBuildTarget:
    """Compose 'build: { target: dev }' must pass --target dev to docker
    build. Multi-stage Dockerfiles are common; ignoring target ships the
    wrong image."""

    def test_build_target_extracted(self, tmp_path):
        from remote_compose.cli_v2 import _service_build_info
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  web:\n    build:\n      context: .\n      target: dev\n")
        svc = {"build": {"context": ".", "target": "dev"}}
        _bc, _args, dfile, img, target = _service_build_info_full(svc, compose)
        assert target == "dev"

    def test_build_target_absent_returns_none(self, tmp_path):
        from remote_compose.cli_v2 import _service_build_info
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("")
        svc = {"build": {"context": "."}}
        _bc, _args, dfile, img, target = _service_build_info_full(svc, compose)
        assert target is None

    def test_short_build_string_no_target(self, tmp_path):
        from remote_compose.cli_v2 import _service_build_info
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("")
        svc = {"build": "."}
        _bc, _args, dfile, img, target = _service_build_info_full(svc, compose)
        assert target is None

    def test_target_flows_to_service_spec(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n  api:\n    build:\n      context: .\n      target: production\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        # ServiceSpec exposes 'target' so ImageBuildSpec gets it.
        assert ctx.services["api"].target == "production"


# Helper that asserts the new 5-tuple shape of _service_build_info.
def _service_build_info_full(svc_compose, compose_path):
    from remote_compose.cli_v2 import _service_build_info
    result = _service_build_info(svc_compose, compose_path)
    # New shape: (build_context, build_args, dockerfile, image, target).
    assert len(result) == 5, f"_service_build_info must return 5-tuple, got {len(result)}"
    return result


class TestComposeEnvFile:
    """Compose 'env_file: [path1, path2]' values are auto-promoted to
    SM secrets per rc-12d (no longer plaintext env). Each file becomes
    a secret entry in DeployContext.secrets, scoped to the services
    that reference it via env_file_secret_names."""

    def test_single_env_file_routes_to_secrets_not_plaintext(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        env = tmp_path / ".env.api"
        env.write_text("FOO=1\nBAR=two\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            f"services:\n  api:\n    image: busybox\n    env_file:\n      - {env}\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        # rc-12d: env_file keys MUST NOT be plaintext.
        assert "FOO" not in ctx.services["api"].env
        assert "BAR" not in ctx.services["api"].env
        # They land in ctx.secrets as a file-source secret.
        file_secrets = [s for s in ctx.secrets if s.source == "file"]
        assert len(file_secrets) == 1
        # And the service is wired to that secret.
        assert len(ctx.services["api"].env_file_secret_names) == 1

    def test_multiple_env_files_each_become_their_own_secret(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        e1 = tmp_path / ".env.1"; e1.write_text("FOO=from1\nA=alpha\n")
        e2 = tmp_path / ".env.2"; e2.write_text("FOO=from2\nB=beta\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            f"services:\n  api:\n    image: busybox\n    env_file:\n      - {e1}\n      - {e2}\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        # rc-12d: no plaintext leaks.
        for k in ("FOO", "A", "B"):
            assert k not in ctx.services["api"].env
        # Two distinct file secrets created (one per unique env_file).
        file_secrets = [s for s in ctx.secrets if s.source == "file"]
        assert len(file_secrets) == 2
        assert len(ctx.services["api"].env_file_secret_names) == 2

    def test_environment_map_overrides_env_file_in_plaintext(self, tmp_path, monkeypatch):
        # When compose has BOTH env_file: (now SM-secret-routed) AND
        # environment: { FOO: ... } (still plaintext), the plaintext
        # `environment:` entry wins for FOO. The env_file's FOO
        # resolves to the same SM secret, but the per-service plaintext
        # filter strips its key from secrets[] (rc-z30), letting the
        # plaintext `environment:` value land verbatim.
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        e1 = tmp_path / ".env"; e1.write_text("FOO=from_file\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            f"services:\n  api:\n    image: busybox\n    env_file: [{e1}]\n"
            f"    environment:\n      FOO: from_environment\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert ctx.services["api"].env["FOO"] == "from_environment"

    def test_env_file_string_form_routes_to_secrets(self, tmp_path, monkeypatch):
        # Compose accepts 'env_file: ./path' as a shortcut for a single file.
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        e = tmp_path / ".env"; e.write_text("FOO=ok\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            f"services:\n  api:\n    image: busybox\n    env_file: {e}\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert "FOO" not in ctx.services["api"].env
        assert any(s.source == "file" for s in ctx.secrets)

    def test_env_file_relative_path_resolves_and_routes_to_secret(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        envs = tmp_path / ".envs" / ".local"; envs.mkdir(parents=True)
        (envs / ".django").write_text("DJANGO_SETTING=ok\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n  api:\n    image: busybox\n    env_file:\n"
            "      - ./.envs/.local/.django\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert "DJANGO_SETTING" not in ctx.services["api"].env
        # And the resolved path matches what the env_file pointed at.
        file_secrets = [s for s in ctx.secrets if s.source == "file"]
        assert len(file_secrets) == 1
        assert ".django" in file_secrets[0].path


class TestComposePortsArray:
    """Compose 'ports: [7788:7788, 5901:5901, 9222:9222]' must produce
    multiple containerPort entries. Today only the first one survives."""

    def test_multiple_ports_extracted(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n  vnc:\n    image: busybox\n    ports:\n"
            "      - '7788:7788'\n      - '5901:5901'\n      - '9222:9222'\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        # ServiceSpec.extra_ports lists ALL ports beyond the primary
        # public port, so the task def can expose every one.
        spec = ctx.services["vnc"]
        all_ports = sorted({spec.port} | set(spec.extra_ports or []))
        assert all_ports == [5901, 7788, 9222], f"got {all_ports}"

    def test_no_ports_means_empty_extras(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  worker:\n    image: busybox\n")
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake", "services": {},
        })
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert ctx.services["worker"].extra_ports == []


class TestComposeAutoImport:
    """build_deploy_context auto-includes compose services that rc.yml
    doesn't list, so adding a service in docker-compose.yml deploys
    automatically. rc.yml services[] becomes overrides on top."""

    def _write_compose(self, tmp_path, services_yaml: str) -> Path:
        p = tmp_path / "docker-compose.yml"
        p.write_text("services:\n" + services_yaml)
        return p

    def _v2(self, **overrides) -> dict:
        base = {
            "version": 2, "project": "auto",
            "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {},
        }
        base.update(overrides)
        return base

    def test_compose_only_service_auto_included(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        self._write_compose(tmp_path, "  api:\n    image: busybox\n")
        p = tmp_path / "rc.yml"
        _write(p, self._v2())
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert "api" in ctx.services, "compose-only service should auto-deploy"

    def test_rc_yml_service_overrides_compose_defaults(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        self._write_compose(tmp_path, "  api:\n    image: busybox\n")
        p = tmp_path / "rc.yml"
        _write(p, self._v2(services={
            "api": {"cpu": 1024, "memory": 4096, "type": "application"},
        }))
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert ctx.services["api"].cpu == 1024
        assert ctx.services["api"].memory == 4096

    def test_exclude_skips_compose_services(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        self._write_compose(
            tmp_path,
            "  api:\n    image: busybox\n  ngrok:\n    image: busybox\n  worker:\n    image: busybox\n",
        )
        p = tmp_path / "rc.yml"
        _write(p, self._v2(compose={"exclude": ["ngrok"]}))
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert "api" in ctx.services
        assert "worker" in ctx.services
        assert "ngrok" not in ctx.services

    def test_include_narrows_to_whitelist(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        self._write_compose(
            tmp_path,
            "  api:\n    image: busybox\n  worker:\n    image: busybox\n  flower:\n    image: busybox\n",
        )
        p = tmp_path / "rc.yml"
        _write(p, self._v2(compose={"include": ["api", "worker"]}))
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert set(ctx.services.keys()) == {"api", "worker"}

    def test_rc_yml_service_not_in_compose_still_deploys(self, tmp_path, monkeypatch):
        # Sometimes a service has no compose definition (pre-built image
        # only, no build context). rc.yml should still deploy it.
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        self._write_compose(tmp_path, "  api:\n    image: busybox\n")
        p = tmp_path / "rc.yml"
        _write(p, self._v2(services={
            "redis": {"cpu": 256, "memory": 512, "type": "infrastructure"},
        }))
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(p)
        ctx = build_deploy_context(v2, raw, p)
        assert "api" in ctx.services
        assert "redis" in ctx.services

    def test_include_unknown_service_rejected(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        self._write_compose(tmp_path, "  api:\n    image: busybox\n")
        p = tmp_path / "rc.yml"
        _write(p, self._v2(compose={"include": ["api", "ghost"]}))
        monkeypatch.chdir(tmp_path)
        version, raw, v2 = load_rc_yml(p)
        with pytest.raises(Exception, match="ghost"):
            build_deploy_context(v2, raw, p)


class TestSecretsPushV2:
    """rc secrets push for v2 rc.yml: parse each .env file, upload as
    JSON to SM, force redeploy. boto3 mocked end-to-end."""

    def _v2_cfg(self, env_path: str) -> dict:
        return {
            "version": 2, "project": "testproj",
            "compose_file": "docker-compose.yml",
            "provider": "ecs",
            "provider_config": {"ecs": {
                "cluster": "testproj-cluster", "region": "us-west-1",
            }},
            "services": {"django": {"cpu": 256, "memory": 512,
                                     "type": "application"}},
            "secrets": [{"name": "django", "source": "file", "path": env_path}],
        }

    def test_push_uploads_json_and_triggers_rollout(self, tmp_path, monkeypatch):
        import json as _json
        from unittest import mock
        from remote_compose import cli as cli_mod

        env_file = tmp_path / ".envs" / ".test" / ".django"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("SECRET_KEY=abc\nDATABASE_URL=postgres://x\n")

        p = tmp_path / "rc.yml"
        _write(p, self._v2_cfg(".envs/.test/.django"))
        monkeypatch.chdir(tmp_path)

        sm = mock.Mock()
        ecs = mock.Mock()
        session = mock.Mock()
        session.client.side_effect = lambda name: {"secretsmanager": sm, "ecs": ecs}[name]
        with mock.patch("boto3.Session", return_value=session):
            handled = cli_mod._secrets_push_v2(str(p), rollout=True)

        assert handled is True
        sm.put_secret_value.assert_called_once()
        call_kwargs = sm.put_secret_value.call_args.kwargs
        assert call_kwargs["SecretId"] == "testproj/django"
        body = _json.loads(call_kwargs["SecretString"])
        assert body == {"SECRET_KEY": "abc", "DATABASE_URL": "postgres://x"}
        ecs.update_service.assert_called_once_with(
            cluster="testproj-cluster", service="django", forceNewDeployment=True,
        )

    def test_push_skips_rollout_when_flagged(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli as cli_mod

        env_file = tmp_path / ".django"
        env_file.write_text("K=v\n")
        p = tmp_path / "rc.yml"
        _write(p, self._v2_cfg(str(env_file)))
        monkeypatch.chdir(tmp_path)

        sm = mock.Mock(); ecs = mock.Mock()
        session = mock.Mock()
        session.client.side_effect = lambda name: {"secretsmanager": sm, "ecs": ecs}[name]
        with mock.patch("boto3.Session", return_value=session):
            cli_mod._secrets_push_v2(str(p), rollout=False)
        ecs.update_service.assert_not_called()

    def test_push_returns_false_for_v1(self, tmp_path, monkeypatch):
        from remote_compose import cli as cli_mod
        p = tmp_path / "rc.yml"
        _write(p, V1_SAMPLE)
        monkeypatch.chdir(tmp_path)
        assert cli_mod._secrets_push_v2(str(p), rollout=True) is False


class TestAutoOnDeployHooks:
    """rc deploy must run all auto_on_deploy hooks after the rollout
    completes, in declaration order, honoring run_once probes."""

    def _v2_with_hooks(self, hooks: dict) -> dict:
        return {
            "version": 2, "project": "p",
            "compose_file": "docker-compose.yml",
            "provider": "fake",
            "provider_config": {},
            "terraform": {"backend": {"type": "local"}},
            "services": {
                "django": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "public": True, "port": 80,
                    "lifecycle": hooks,
                },
            },
        }

    def test_auto_on_deploy_hook_runs_after_deploy(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli_v2 as v2mod
        cfg = self._v2_with_hooks({
            "migrate": {"command": ["./bin/migrate"], "auto_on_deploy": True},
        })
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        monkeypatch.chdir(tmp_path)

        provider = mock.Mock()
        provider.deploy.return_value = mock.Mock(
            revision_id="r1", services=["django"], duration_s=1.0,
            terraform_outputs={}, warnings=[],
        )
        provider.exec.return_value = mock.Mock(
            exit_code=0, stdout="ok", stderr="",
        )
        with mock.patch.object(v2mod, "resolve_provider", return_value=provider):
            assert v2mod.dispatch_if_v2(str(p), "deploy") is True
        # exec called once for the migrate hook with the right command.
        provider.exec.assert_called_once()
        args = provider.exec.call_args
        assert args.args[1] == "django"
        assert args.args[2] == ["./bin/migrate"]

    def test_auto_on_deploy_runs_in_declaration_order(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli_v2 as v2mod
        cfg = self._v2_with_hooks({
            "first": {"command": ["./first"], "auto_on_deploy": True},
            "second": {"command": ["./second"], "auto_on_deploy": True},
            "third": {"command": ["./third"], "auto_on_deploy": True},
        })
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        monkeypatch.chdir(tmp_path)

        provider = mock.Mock()
        provider.deploy.return_value = mock.Mock(
            revision_id="r1", services=["django"], duration_s=1.0,
            terraform_outputs={}, warnings=[],
        )
        provider.exec.return_value = mock.Mock(exit_code=0, stdout="", stderr="")
        with mock.patch.object(v2mod, "resolve_provider", return_value=provider):
            v2mod.dispatch_if_v2(str(p), "deploy")
        called_cmds = [c.args[2] for c in provider.exec.call_args_list]
        assert called_cmds == [["./first"], ["./second"], ["./third"]]

    def test_run_once_probe_skips_when_satisfied(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli_v2 as v2mod
        cfg = self._v2_with_hooks({
            "createsuperuser": {
                "command": ["./csu"], "auto_on_deploy": True,
                "run_once": True, "probe": ["./check"],
            },
        })
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        monkeypatch.chdir(tmp_path)

        provider = mock.Mock()
        provider.deploy.return_value = mock.Mock(
            revision_id="r1", services=["django"], duration_s=1.0,
            terraform_outputs={}, warnings=[],
        )
        # Probe returns exit 0 — already done.
        probe_result = mock.Mock(exit_code=0, stdout="", stderr="")
        run_result = mock.Mock(exit_code=0, stdout="", stderr="")
        provider.exec.side_effect = [probe_result, run_result]
        with mock.patch.object(v2mod, "resolve_provider", return_value=provider):
            v2mod.dispatch_if_v2(str(p), "deploy")
        # Only the probe ran; createsuperuser command itself was skipped.
        assert provider.exec.call_count == 1
        assert provider.exec.call_args.args[2] == ["./check"]

    def test_run_once_probe_runs_command_when_not_satisfied(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli_v2 as v2mod
        cfg = self._v2_with_hooks({
            "csu": {
                "command": ["./csu"], "auto_on_deploy": True,
                "run_once": True, "probe": ["./check"],
            },
        })
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        monkeypatch.chdir(tmp_path)

        provider = mock.Mock()
        provider.deploy.return_value = mock.Mock(
            revision_id="r1", services=["django"], duration_s=1.0,
            terraform_outputs={}, warnings=[],
        )
        # Probe nonzero -> not done -> run command.
        provider.exec.side_effect = [
            mock.Mock(exit_code=1, stdout="", stderr=""),
            mock.Mock(exit_code=0, stdout="", stderr=""),
        ]
        with mock.patch.object(v2mod, "resolve_provider", return_value=provider):
            v2mod.dispatch_if_v2(str(p), "deploy")
        cmds = [c.args[2] for c in provider.exec.call_args_list]
        assert cmds == [["./check"], ["./csu"]]

    def test_hook_failure_does_not_fail_deploy(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli_v2 as v2mod
        cfg = self._v2_with_hooks({
            "migrate": {"command": ["./fail"], "auto_on_deploy": True},
        })
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        monkeypatch.chdir(tmp_path)

        provider = mock.Mock()
        provider.deploy.return_value = mock.Mock(
            revision_id="r1", services=["django"], duration_s=1.0,
            terraform_outputs={}, warnings=[],
        )
        provider.exec.return_value = mock.Mock(
            exit_code=1, stdout="", stderr="boom",
        )
        with mock.patch.object(v2mod, "resolve_provider", return_value=provider):
            # Should NOT raise; hook failures are warnings.
            assert v2mod.dispatch_if_v2(str(p), "deploy") is True

    def test_no_auto_hooks_short_circuits(self, tmp_path, monkeypatch):
        from unittest import mock
        from remote_compose import cli_v2 as v2mod
        cfg = self._v2_with_hooks({
            "shell": {"command": ["./shell"], "interactive": True},
        })
        p = tmp_path / "rc.yml"
        _write(p, cfg)
        monkeypatch.chdir(tmp_path)

        provider = mock.Mock()
        provider.deploy.return_value = mock.Mock(
            revision_id="r1", services=["django"], duration_s=1.0,
            terraform_outputs={}, warnings=[],
        )
        with mock.patch.object(v2mod, "resolve_provider", return_value=provider):
            v2mod.dispatch_if_v2(str(p), "deploy")
        provider.exec.assert_not_called()


class TestDbPushFormatDetection:
    def test_dump_extension(self):
        from remote_compose.cli import _detect_dump_format
        assert _detect_dump_format("foo.dump") == "pg_restore"
        assert _detect_dump_format("backup.pgdump") == "pg_restore"

    def test_targz_extension(self):
        from remote_compose.cli import _detect_dump_format
        assert _detect_dump_format("dir.tar.gz") == "tar+pg_restore"
        assert _detect_dump_format("dir.tgz") == "tar+pg_restore"

    def test_sql_extension(self):
        from remote_compose.cli import _detect_dump_format
        assert _detect_dump_format("seed.sql") == "psql"

    def test_unknown_extension_rejected(self):
        from remote_compose.cli import _detect_dump_format
        import click
        with pytest.raises(click.exceptions.UsageError, match="cannot detect"):
            _detect_dump_format("data.bak")


class TestDbPushRestoreScript:
    def test_pg_restore_script_uses_curl_then_pg_restore(self):
        from remote_compose.cli import _build_restore_script
        s = _build_restore_script("x.dump", "https://signed", "pg_restore")
        assert "curl -fsSL" in s
        assert "pg_restore" in s
        assert "https://signed" in s
        assert "--no-owner --clean --if-exists" in s

    def test_targz_script_extracts_then_restores_directory(self):
        from remote_compose.cli import _build_restore_script
        s = _build_restore_script("x.tar.gz", "https://signed", "tar+pg_restore")
        assert "tar -xzf" in s
        assert "pg_restore -Fd" in s

    def test_psql_script_uses_psql_f(self):
        from remote_compose.cli import _build_restore_script
        s = _build_restore_script("seed.sql", "https://signed", "psql")
        assert "psql " in s
        assert "-f /tmp/_rcpush.sql" in s

    def test_script_cleans_up_on_exit(self):
        from remote_compose.cli import _build_restore_script
        for fmt, name in [("pg_restore", "x.dump"), ("psql", "x.sql"),
                          ("tar+pg_restore", "x.tar.gz")]:
            s = _build_restore_script(name, "https://signed", fmt)
            assert "rm -rf /tmp/_rcpush*" in s


class TestServiceV2EnvMerge:
    """rc-e5u.46.4: services.<svc>.env in rc.yml merges ON TOP of compose's
    environment / env_file. rc.yml wins on collision so the
    scaffolder-injected DJANGO_ALLOWED_HOSTS=* can override an inherited
    value from a long-lived env_file (e.g. a prod-leaning .envs/.local/.django
    that pins ALLOWED_HOSTS to a specific domain).
    """

    def test_rc_yml_env_merges_on_top_of_compose_env(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n  api:\n    image: busybox\n"
            "    environment:\n      FOO: from_compose\n      KEEP: original\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "env": {"FOO": "from_rc_yml", "EXTRA": "added"},
                },
            },
        })
        monkeypatch.chdir(tmp_path)
        _, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert ctx.services["api"].env["FOO"] == "from_rc_yml"  # rc.yml wins
        assert ctx.services["api"].env["KEEP"] == "original"   # compose pass-through
        assert ctx.services["api"].env["EXTRA"] == "added"     # rc.yml adds

    def test_rc_yml_env_overrides_env_file(self, tmp_path, monkeypatch):
        # The .46.4 use case: env_file pins DJANGO_ALLOWED_HOSTS=mydomain
        # but rc.yml's testing-defaults injection wants '*'. rc.yml wins.
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        ef = tmp_path / ".env"
        ef.write_text("DJANGO_ALLOWED_HOSTS=mydomain.com\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            f"services:\n  django:\n    image: busybox\n    env_file: [{ef}]\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {
                "django": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "env": {"DJANGO_ALLOWED_HOSTS": "*"},
                },
            },
        })
        monkeypatch.chdir(tmp_path)
        _, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert ctx.services["django"].env["DJANGO_ALLOWED_HOSTS"] == "*"

    def test_empty_rc_env_is_a_noop(self, tmp_path, monkeypatch):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n  api:\n    image: busybox\n"
            "    environment:\n      FOO: bar\n"
        )
        rc = tmp_path / "rc.yml"
        _write(rc, {
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {
                "api": {"cpu": 256, "memory": 512, "type": "application"},
            },
        })
        monkeypatch.chdir(tmp_path)
        _, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert ctx.services["api"].env == {"FOO": "bar"}

    def test_rc_yml_env_coerces_yaml_bool_to_str(self, tmp_path, monkeypatch):
        # YAML 'False' parses as bool False — schema coerces to "False" so
        # the value can flow into the task-def environment[] (env values
        # must be strings).
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  api:\n    image: busybox\n")
        rc = tmp_path / "rc.yml"
        rc.write_text(yaml.safe_dump({
            "version": 2, "project": "p", "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {
                "api": {
                    "cpu": 256, "memory": 512, "type": "application",
                    "env": {"DJANGO_DEBUG": False, "PORT": 8080},
                },
            },
        }))
        monkeypatch.chdir(tmp_path)
        _, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        assert ctx.services["api"].env["DJANGO_DEBUG"] == "False"
        assert ctx.services["api"].env["PORT"] == "8080"

