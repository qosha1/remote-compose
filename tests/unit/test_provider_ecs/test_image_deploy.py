"""Unit tests for ECSProvider.deploy image build + push orchestration."""

from __future__ import annotations

import base64
import os
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
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "img-test",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
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
        "}}}",
    )
    return runner


class TestBuildPushSkippedWhenNoBuildContext:
    def test_no_builds_no_pushes_no_force_redeploy(
        self,
        tmp_path,
        mock_session,
        recording_runner,
    ):
        ctx = _ctx(
            tmp_path,
            {
                "public-image": ServiceSpec(
                    name="public-image",
                    cpu=256,
                    memory=512,
                    type="application",
                    image="nginx:alpine",
                ),
            },
        )
        provider = ECSProvider(
            runner_factory=lambda d: recording_runner,
            session_factory=lambda c: mock_session,
        )
        with (
            mock.patch("remote_compose.image.ImageBuilder") as builder_cls,
            mock.patch("remote_compose.image.ImagePusher") as pusher_cls,
        ):
            provider.deploy(ctx)
            builder_cls.assert_not_called()
            pusher_cls.assert_not_called()

        mock_session.client.return_value.update_service.assert_not_called()


class TestBuildPushHappyPath:
    def test_build_push_and_force_new_deployment(
        self,
        tmp_path,
        mock_session,
        recording_runner,
    ):
        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        (api_ctx / "Dockerfile").write_text("FROM alpine\n")
        worker_ctx = tmp_path / "worker"
        worker_ctx.mkdir()
        (worker_ctx / "Dockerfile").write_text("FROM alpine\n")

        ctx = _ctx(
            tmp_path,
            {
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    type="application",
                    build_context=api_ctx,
                    build_args={"VERSION": "1.0"},
                ),
                "worker": ServiceSpec(
                    name="worker",
                    cpu=256,
                    memory=512,
                    type="worker",
                    build_context=worker_ctx,
                ),
            },
        )
        provider = ECSProvider(
            runner_factory=lambda d: recording_runner,
            session_factory=lambda c: mock_session,
        )
        # Valid ECR token so ECRAuthenticator succeeds.
        token = base64.b64encode(b"AWS:pw").decode()
        mock_session.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
        }

        # With no terraform buildcache output, rc now derives a cache repo and
        # builds via `docker buildx build` (cache_to → the Popen watchdog path),
        # so capture both subprocess.run and subprocess.Popen.
        popen_cmds: list[list] = []

        def _fake_popen(cmd, *args, **kwargs):
            popen_cmds.append(cmd)
            proc = mock.Mock()
            proc.stdout = mock.Mock()
            proc.stdout.readline.return_value = ""
            proc.stdout.close = mock.Mock()
            proc.stderr = mock.Mock()
            proc.stderr.readline.return_value = ""
            proc.stderr.close = mock.Mock()
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.kill = mock.Mock()
            return proc

        with (
            mock.patch("subprocess.run") as sub_run,
            mock.patch("subprocess.Popen", side_effect=_fake_popen),
        ):
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = provider.deploy(ctx)

        run_cmds = [c.args[0] for c in sub_run.call_args_list]
        cmds = run_cmds + popen_cmds

        # shutil.which("docker") resolves to a full path on dev machines, so
        # match the verb (c[1]) rather than the absolute binary path. Builds go
        # through `docker buildx build`; push/login are plain `docker <verb>`.
        def _is(cmd, verb):
            return (
                len(cmd) >= 2
                and cmd[0].rsplit("/", 1)[-1] == "docker"
                and cmd[1] == verb
            )

        def _is_buildx_build(cmd):
            return (
                len(cmd) >= 3
                and cmd[0].rsplit("/", 1)[-1] == "docker"
                and cmd[1] == "buildx"
                and cmd[2] == "build"
            )

        build_cmds = [c for c in cmds if _is_buildx_build(c)]
        push_cmds = [c for c in run_cmds if _is(c, "push")]
        login_cmds = [c for c in run_cmds if _is(c, "login")]
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
        self,
        tmp_path,
        mock_session,
    ):
        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        ctx = _ctx(
            tmp_path,
            {
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    type="application",
                    build_context=api_ctx,
                ),
            },
        )
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
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
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
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://x",
                }
            ],
        }
        auth = ECRAuthenticator(session=sess)
        with mock.patch("subprocess.run") as sub_run:
            sub_run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="bad creds"
            )
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


# ---------------------------------------------------------------------------
# rc-e5u.45.2 — BuildKit registry cache wiring
# ---------------------------------------------------------------------------


class TestBuildcacheRepoWired:
    """When terraform emits buildcache_repository, the provider feeds
    --cache-from / --cache-to into ImageBuildSpec for every service it
    builds. Cache tag pattern: <buildcache>:<svc>-cache.
    """

    def _ctx_with_builds(self, tmp_path):
        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        (api_ctx / "Dockerfile").write_text("FROM alpine\n")
        return _ctx(
            tmp_path,
            {
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    type="application",
                    build_context=api_ctx,
                ),
            },
        )

    def _runner_with_buildcache(self, tmp_path, *, buildcache: bool):
        runner = mock.MagicMock()
        outputs = {
            "ecr_repositories": {
                "value": {
                    "api": "111.dkr.ecr.us-east-1.amazonaws.com/img-test/api",
                }
            },
        }
        if buildcache:
            outputs["buildcache_repository"] = {
                "value": "111.dkr.ecr.us-east-1.amazonaws.com/img-test/buildcache",
            }
        runner.output.return_value = outputs
        return runner

    def test_cache_args_emitted_when_buildcache_present(
        self,
        tmp_path,
        mock_session,
    ):
        ctx = self._ctx_with_builds(tmp_path)
        runner = self._runner_with_buildcache(tmp_path, buildcache=True)
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
        )
        token = base64.b64encode(b"AWS:pw").decode()
        mock_session.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
        }
        # rc-mtt: with cache_to set, the builder routes through
        # subprocess.Popen + a no-progress watchdog instead of
        # subprocess.run. Mock both so we capture the command no matter
        # which path the builder picks.
        popen_cmds: list[list] = []

        def _fake_popen(cmd, *args, **kwargs):
            popen_cmds.append(cmd)
            proc = mock.Mock()
            proc.stdout = mock.Mock()
            proc.stdout.readline.return_value = ""
            proc.stdout.close = mock.Mock()
            proc.stderr = mock.Mock()
            proc.stderr.readline.return_value = ""
            proc.stderr.close = mock.Mock()
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.kill = mock.Mock()
            return proc

        with (
            mock.patch("subprocess.run") as sub_run,
            mock.patch("subprocess.Popen", side_effect=_fake_popen),
        ):
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            provider.deploy(ctx)

        cmds = popen_cmds + [c.args[0] for c in sub_run.call_args_list]

        def _is_buildx(cmd):
            return (
                len(cmd) >= 3
                and cmd[0].rsplit("/", 1)[-1] == "docker"
                and cmd[1] == "buildx"
                and cmd[2] == "build"
            )

        buildx_cmds = [c for c in cmds if _is_buildx(c)]
        assert (
            len(buildx_cmds) == 1
        ), f"expected one `docker buildx build` invocation, got {buildx_cmds!r}"
        cmd = buildx_cmds[0]
        cache_ref = "111.dkr.ecr.us-east-1.amazonaws.com/img-test/buildcache:api-cache"
        cf_idx = cmd.index("--cache-from")
        ct_idx = cmd.index("--cache-to")
        assert cmd[cf_idx + 1] == f"type=registry,ref={cache_ref}"
        assert cmd[ct_idx + 1] == (
            f"type=registry,ref={cache_ref},mode=max"
            ",image-manifest=true,oci-mediatypes=true"
        )

    def test_cache_repo_derived_when_no_terraform_output(
        self,
        tmp_path,
        mock_session,
    ):
        # No terraform buildcache output and no RC_BUILDCACHE_REPO: rc derives
        # <registry>/<project>/buildcache from the project's ECR registry,
        # ensures it exists, and caches against it (adopted/--no-state stacks).
        ctx = self._ctx_with_builds(tmp_path)
        runner = self._runner_with_buildcache(tmp_path, buildcache=False)
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
        )
        token = base64.b64encode(b"AWS:pw").decode()
        ecr_mock = mock_session.client.return_value
        ecr_mock.get_authorization_token.return_value = {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
        }
        # Derived cache repo doesn't exist yet → describe raises, rc creates it.
        ecr_mock.describe_repositories.side_effect = Exception("RepositoryNotFound")
        popen_cmds: list[list] = []

        def _fake_popen(cmd, *args, **kwargs):
            popen_cmds.append(cmd)
            proc = mock.Mock()
            proc.stdout = mock.Mock()
            proc.stdout.readline.return_value = ""
            proc.stdout.close = mock.Mock()
            proc.stderr = mock.Mock()
            proc.stderr.readline.return_value = ""
            proc.stderr.close = mock.Mock()
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.kill = mock.Mock()
            return proc

        # No RC_BUILDCACHE_REPO in the environment for this test.
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("subprocess.run") as sub_run,
            mock.patch("subprocess.Popen", side_effect=_fake_popen),
        ):
            os.environ.pop("RC_BUILDCACHE_REPO", None)
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            provider.deploy(ctx)

        cmds = popen_cmds + [c.args[0] for c in sub_run.call_args_list]
        buildx_cmds = [
            c
            for c in cmds
            if len(c) >= 3
            and c[0].rsplit("/", 1)[-1] == "docker"
            and c[1] == "buildx"
            and c[2] == "build"
        ]
        assert (
            len(buildx_cmds) == 1
        ), f"expected derived-cache buildx build, got {cmds!r}"
        cmd = buildx_cmds[0]
        # Derived: registry from repos ('111.dkr.ecr...'), project 'img-test'.
        cache_ref = "111.dkr.ecr.us-east-1.amazonaws.com/img-test/buildcache:api-cache"
        cf_idx = cmd.index("--cache-from")
        assert cmd[cf_idx + 1] == f"type=registry,ref={cache_ref}"
        # rc created the derived repo (describe found nothing → create_repository).
        mock_session.client.return_value.create_repository.assert_called_once()

    def test_cache_args_emitted_from_env_when_no_terraform_output(
        self,
        tmp_path,
        mock_session,
    ):
        # Adopted / --no-state stacks have no buildcache_repository output;
        # RC_BUILDCACHE_REPO supplies the cache repo so they still cache.
        ctx = self._ctx_with_builds(tmp_path)
        runner = self._runner_with_buildcache(tmp_path, buildcache=False)
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
        )
        token = base64.b64encode(b"AWS:pw").decode()
        mock_session.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
        }
        popen_cmds: list[list] = []

        def _fake_popen(cmd, *args, **kwargs):
            popen_cmds.append(cmd)
            proc = mock.Mock()
            proc.stdout = mock.Mock()
            proc.stdout.readline.return_value = ""
            proc.stdout.close = mock.Mock()
            proc.stderr = mock.Mock()
            proc.stderr.readline.return_value = ""
            proc.stderr.close = mock.Mock()
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.kill = mock.Mock()
            return proc

        env_repo = "111.dkr.ecr.us-east-1.amazonaws.com/img-test/buildcache"
        with (
            mock.patch.dict(os.environ, {"RC_BUILDCACHE_REPO": env_repo}),
            mock.patch("subprocess.run") as sub_run,
            mock.patch("subprocess.Popen", side_effect=_fake_popen),
        ):
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            provider.deploy(ctx)

        cmds = popen_cmds + [c.args[0] for c in sub_run.call_args_list]
        buildx_cmds = [
            c
            for c in cmds
            if len(c) >= 3
            and c[0].rsplit("/", 1)[-1] == "docker"
            and c[1] == "buildx"
            and c[2] == "build"
        ]
        assert len(buildx_cmds) == 1, (
            f"expected one `docker buildx build` from RC_BUILDCACHE_REPO, "
            f"got {buildx_cmds!r}"
        )
        cmd = buildx_cmds[0]
        cache_ref = f"{env_repo}:api-cache"
        cf_idx = cmd.index("--cache-from")
        ct_idx = cmd.index("--cache-to")
        assert cmd[cf_idx + 1] == f"type=registry,ref={cache_ref}"
        assert cmd[ct_idx + 1] == (
            f"type=registry,ref={cache_ref},mode=max"
            ",image-manifest=true,oci-mediatypes=true"
        )


class TestBuildcacheTerraformEmitted:
    """The terraform template gates the buildcache repo on
    has_build_context_service. Stacks with at least one compose `build:`
    get the repo + lifecycle policy + output; pure-image stacks don't.
    """

    def test_buildcache_repo_emitted_when_a_service_has_build_context(
        self,
        tmp_path,
    ):
        from remote_compose.terraform.runner import RecordingTerraformRunner

        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        (api_ctx / "Dockerfile").write_text("FROM alpine\n")
        ctx = _ctx(
            tmp_path,
            {
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    type="application",
                    build_context=api_ctx,
                ),
            },
        )
        out = tmp_path / "tf"
        provider = ECSProvider(
            runner_factory=lambda d: RecordingTerraformRunner(d),
            session_factory=lambda c: mock.MagicMock(),
        )
        provider.emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        outputs_tf = (out / "outputs.tf").read_text()

        assert 'aws_ecr_repository" "buildcache"' in services_tf
        assert "${var.project}/buildcache" in services_tf
        # 7-day lifecycle expiry on untagged manifests
        assert "aws_ecr_lifecycle_policy" in services_tf
        assert "countNumber = 7" in services_tf
        # Output exposes the repo URL so the provider can derive cache tags
        assert 'output "buildcache_repository"' in outputs_tf

    def test_buildcache_skipped_for_pure_image_stack(self, tmp_path):
        from remote_compose.terraform.runner import RecordingTerraformRunner

        ctx = _ctx(
            tmp_path,
            {
                "proxy": ServiceSpec(
                    name="proxy",
                    cpu=256,
                    memory=512,
                    type="proxy",
                    image="nginx:alpine",
                ),
            },
        )
        out = tmp_path / "tf"
        provider = ECSProvider(
            runner_factory=lambda d: RecordingTerraformRunner(d),
            session_factory=lambda c: mock.MagicMock(),
        )
        provider.emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        outputs_tf = (out / "outputs.tf").read_text()

        assert "buildcache" not in services_tf
        assert "buildcache_repository" not in outputs_tf


# ---------------------------------------------------------------------------
# rc-8j7.2 / .4 — build-backend + cache-mode config flows through the provider
# ---------------------------------------------------------------------------


class TestBuildConfigFlowsThroughProvider:
    def _ctx_with_build(self, tmp_path, build_cfg: dict):
        api_ctx = tmp_path / "api"
        api_ctx.mkdir()
        (api_ctx / "Dockerfile").write_text("FROM alpine\n")
        ctx = _ctx(
            tmp_path,
            {
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    type="application",
                    build_context=api_ctx,
                ),
            },
        )
        ctx.provider_config["ecs"]["build"] = build_cfg
        return ctx

    def _runner(self, tmp_path):
        runner = mock.MagicMock()
        runner.output.return_value = {
            "ecr_repositories": {
                "value": {
                    "api": "111.dkr.ecr.us-east-1.amazonaws.com/img-test/api",
                }
            },
            "buildcache_repository": {
                "value": "111.dkr.ecr.us-east-1.amazonaws.com/img-test/buildcache",
            },
        }
        return runner

    def _auth_token(self, mock_session):
        token = base64.b64encode(b"AWS:pw").decode()
        mock_session.client.return_value.get_authorization_token.return_value = {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://111.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
        }

    def test_cache_mode_min_reaches_buildx_cmd(self, tmp_path, mock_session):
        # provider_config.ecs.build.cache_mode=min must land in --cache-to.
        ctx = self._ctx_with_build(tmp_path, {"cache_mode": "min"})
        runner = self._runner(tmp_path)
        self._auth_token(mock_session)
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
        )
        popen_cmds: list[list] = []

        def _fake_popen(cmd, *args, **kwargs):
            popen_cmds.append(cmd)
            proc = mock.Mock()
            proc.stdout = mock.Mock()
            proc.stdout.readline.return_value = ""
            proc.stdout.close = mock.Mock()
            proc.stderr = mock.Mock()
            proc.stderr.readline.return_value = ""
            proc.stderr.close = mock.Mock()
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.kill = mock.Mock()
            return proc

        with (
            mock.patch("subprocess.run") as sub_run,
            mock.patch("subprocess.Popen", side_effect=_fake_popen),
        ):
            sub_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            provider.deploy(ctx)

        cmds = popen_cmds + [c.args[0] for c in sub_run.call_args_list]
        buildx = [
            c for c in cmds if len(c) >= 3 and c[1] == "buildx" and c[2] == "build"
        ]
        assert len(buildx) == 1
        ct_idx = buildx[0].index("--cache-to")
        assert ",mode=min," in buildx[0][ct_idx + 1]

    def test_unknown_backend_raises_provider_config_error(self, tmp_path, mock_session):
        from remote_compose.provider.base import ProviderConfigError

        ctx = self._ctx_with_build(tmp_path, {"backend": "not-a-backend"})
        runner = self._runner(tmp_path)
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
        )
        with pytest.raises(ProviderConfigError, match="not-a-backend"):
            provider.deploy(ctx)
