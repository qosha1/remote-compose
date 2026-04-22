"""Unit tests for ECSProvider.deploy image build + push orchestration."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.ecr_auth import ECRAuthenticator, ECRAuthError
from remote_compose.terraform.runner import RecordingTerraformRunner


def _ctx(tmp_path: Path, services: dict) -> DeployContext:
    return DeployContext(
        project="img-test",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-east-1",
            "cluster": "img-test",
            "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


@pytest.fixture
def mock_session():
    sess = mock.MagicMock()
    sess.client.return_value = mock.MagicMock()
    return sess


@pytest.fixture
def recording_runner(tmp_path):
    runner = RecordingTerraformRunner(tmp_path / "terraform")
    runner.script(
        "output",
        '{"ecr_repositories": {"value": {'
        '"api":    "111.dkr.ecr.us-east-1.amazonaws.com/img-test/api",'
        '"worker": "111.dkr.ecr.us-east-1.amazonaws.com/img-test/worker"'
        '}}}',
    )
    return runner


class TestBuildPushSkippedWhenNoBuildContext:
    def test_no_builds_no_pushes_no_force_redeploy(
        self, tmp_path, mock_session, recording_runner,
    ):
        ctx = _ctx(tmp_path, {
            "public-image": ServiceSpec(
                name="public-image", cpu=256, memory=512, type="application",
                image="nginx:alpine",
            ),
        })
        provider = ECSProvider(
            runner_factory=lambda d: recording_runner,
            session_factory=lambda c: mock_session,
        )
        with mock.patch("remote_compose.image.ImageBuilder") as builder_cls, \
             mock.patch("remote_compose.image.ImagePusher") as pusher_cls:
            provider.deploy(ctx)
            builder_cls.assert_not_called()
            pusher_cls.assert_not_called()

        mock_session.client.return_value.update_service.assert_not_called()


class TestBuildPushHappyPath:
    def test_build_push_and_force_new_deployment(
        self, tmp_path, mock_session, recording_runner,
    ):
        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        (api_ctx / "Dockerfile").write_text("FROM alpine\n")
        worker_ctx = tmp_path / "worker"
        worker_ctx.mkdir()
        (worker_ctx / "Dockerfile").write_text("FROM alpine\n")

        ctx = _ctx(tmp_path, {
            "api": ServiceSpec(
                name="api", cpu=256, memory=512, type="application",
                build_context=api_ctx, build_args={"VERSION": "1.0"},
            ),
            "worker": ServiceSpec(
                name="worker", cpu=256, memory=512, type="worker",
                build_context=worker_ctx,
            ),
        })
        provider = ECSProvider(
            runner_factory=lambda d: recording_runner,
            session_factory=lambda c: mock_session,
        )
        # Valid ECR token so ECRAuthenticator succeeds.
        token = base64.b64encode(b"AWS:pw").decode()
        mock_session.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [{
                "authorizationToken": token,
                "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
            }],
        }

        # All three modules (builder/pusher/ecr_auth) share the same
        # subprocess module — patching once captures every docker call.
        with mock.patch("subprocess.run") as sub_run:
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = provider.deploy(ctx)

        cmds = [c.args[0] for c in sub_run.call_args_list]
        # shutil.which("docker") resolves to a full path on dev machines, so
        # match the verb (c[1]) rather than the absolute binary path.
        def _is(cmd, verb):
            return len(cmd) >= 2 and cmd[0].rsplit("/", 1)[-1] == "docker" and cmd[1] == verb
        build_cmds = [c for c in cmds if _is(c, "build")]
        push_cmds = [c for c in cmds if _is(c, "push")]
        login_cmds = [c for c in cmds if _is(c, "login")]
        assert len(build_cmds) == 2
        assert len(push_cmds) == 2

        api_tag = "111.dkr.ecr.us-east-1.amazonaws.com/img-test/api:latest"
        worker_tag = "111.dkr.ecr.us-east-1.amazonaws.com/img-test/worker:latest"
        assert any(api_tag in c for c in build_cmds)
        assert any(worker_tag in c for c in build_cmds)
        push_tags = [c[-1] for c in push_cmds]
        assert api_tag in push_tags
        assert worker_tag in push_tags

        # docker login for the ECR registry, called once (cached across pushes)
        assert len(login_cmds) == 1

        # Force new deployment per built service
        update = mock_session.client.return_value.update_service
        assert update.call_count == 2
        services_forced = {c.kwargs["service"] for c in update.call_args_list}
        assert services_forced == {"api", "worker"}

        assert result.revision_id


class TestBuildPushSkipWhenNoEcrOutputs:
    def test_missing_outputs_warns_but_continues(
        self, tmp_path, mock_session,
    ):
        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        ctx = _ctx(tmp_path, {
            "api": ServiceSpec(
                name="api", cpu=256, memory=512, type="application",
                build_context=api_ctx,
            ),
        })
        runner = RecordingTerraformRunner(tmp_path / "terraform")
        runner.script("output", "{}")
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
        )
        result = provider.deploy(ctx)
        assert any("ecr_repositories" in w for w in result.warnings)
        mock_session.client.return_value.update_service.assert_not_called()


class TestECRAuthenticator:
    def test_login_succeeds_and_caches(self):
        sess = mock.MagicMock()
        token = base64.b64encode(b"AWS:secret-password").decode()
        sess.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [{
                "authorizationToken": token,
                "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
            }],
        }
        auth = ECRAuthenticator(session=sess)

        with mock.patch("subprocess.run") as sub_run:
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            auth("111.dkr.ecr.us-east-1.amazonaws.com")
            auth("111.dkr.ecr.us-east-1.amazonaws.com")  # cached — no second call

        assert sub_run.call_count == 1
        call = sub_run.call_args
        assert call.args[0][:3] == ["docker", "login", "--username"]
        assert call.kwargs["input"] == "secret-password"

    def test_docker_login_failure_raises(self):
        sess = mock.MagicMock()
        token = base64.b64encode(b"AWS:pw").decode()
        sess.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [{
                "authorizationToken": token,
                "proxyEndpoint": "https://x",
            }],
        }
        auth = ECRAuthenticator(session=sess)
        with mock.patch("subprocess.run") as sub_run:
            sub_run.return_value = mock.Mock(returncode=1, stdout="", stderr="bad creds")
            with pytest.raises(ECRAuthError, match="bad creds"):
                auth("x")

    def test_missing_authorization_data_raises(self):
        sess = mock.MagicMock()
        sess.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [],
        }
        auth = ECRAuthenticator(session=sess)
        with pytest.raises(ECRAuthError, match="no authorization data"):
            auth("x")
