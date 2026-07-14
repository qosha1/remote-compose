"""rc-8j7.5: AWS CodeBuild build backend.

Builds off the GitHub runner inside AWS CodeBuild near ECR: tar the context →
S3, ensure the CodeBuild project (create-if-missing, referenced IAM role),
start a build whose generated buildspec runs the SAME `docker buildx` per
image, stream CloudWatch logs, poll to completion. No real AWS — the boto3
session is a fake whose clients are MagicMocks (the pattern the provider suite
uses). buildspec generation + context tarring are pure functions exercised
directly.
"""

from __future__ import annotations

import zipfile
import io
from pathlib import Path
from unittest import mock

import pytest
import yaml

from remote_compose.image.backend import (
    AwsCodeBuildBackend,
    CodeBuildConfig,
    CodeBuildError,
    create_build_backend,
    generate_codebuild_buildspec,
    zip_build_context,
)
from remote_compose.image.builder import ImageBuildSpec

HOST = "111111111111.dkr.ecr.us-east-2.amazonaws.com"
CACHE = f"{HOST}/proj/buildcache"
ROLE = "arn:aws:iam::111111111111:role/rc-codebuild"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal boto3.Session stand-in: hands back a pre-built MagicMock per
    service name (matches how the provider unit tests fake AWS)."""

    def __init__(self, clients: dict):
        self._clients = clients
        self.client_calls: list[tuple[str, dict]] = []

    def client(self, name, **kwargs):
        self.client_calls.append((name, kwargs))
        return self._clients[name]


def _repo_tree(tmp_path: Path) -> Path:
    """A repo root with a Django-style layout: django builds from `.`, nginx
    from a subdir under compose/production."""
    root = tmp_path / "repo"
    (root / "compose" / "production" / "django").mkdir(parents=True)
    (root / "compose" / "production" / "nginx").mkdir(parents=True)
    (root / "backend").mkdir()
    (root / "backend" / "app.py").write_text("print('hi')\n")
    (root / "compose" / "production" / "django" / "Dockerfile").write_text(
        "FROM python:3.12\n"
    )
    (root / "compose" / "production" / "nginx" / "Dockerfile").write_text(
        "FROM nginx\n"
    )
    return root


def _django_spec(root: Path) -> ImageBuildSpec:
    return ImageBuildSpec(
        service="django",
        context=root,
        dockerfile=Path("compose/production/django/Dockerfile"),
        build_args={"BUILD_ENV": "production"},
        tags=[f"{HOST}/proj/django:latest"],
        platform="linux/amd64",
        cache_from=[f"{CACHE}:django-cache"],
        cache_to=[f"{CACHE}:django-cache"],
    )


def _nginx_spec(root: Path) -> ImageBuildSpec:
    return ImageBuildSpec(
        service="nginx",
        context=root / "compose" / "production" / "nginx",
        tags=[f"{HOST}/proj/nginx:latest"],
        platform="linux/amd64",
        cache_from=[f"{CACHE}:nginx-cache"],
        cache_to=[f"{CACHE}:nginx-cache"],
    )


# ---------------------------------------------------------------------------
# Buildspec generation
# ---------------------------------------------------------------------------


class TestBuildspec:
    def _spec_doc(self, root):
        specs = [_django_spec(root), _nginx_spec(root)]
        text = generate_codebuild_buildspec(specs, root=root, region="us-east-2")
        return yaml.safe_load(text), text

    def test_version_and_phases(self, tmp_path):
        doc, _ = self._spec_doc(_repo_tree(tmp_path))
        assert doc["version"] == 0.2
        assert set(doc["phases"]) == {"pre_build", "build"}

    def test_pre_build_logs_into_ecr_and_creates_builder(self, tmp_path):
        doc, _ = self._spec_doc(_repo_tree(tmp_path))
        pre = doc["phases"]["pre_build"]["commands"]
        assert any(
            "aws ecr get-login-password --region us-east-2" in c and HOST in c
            for c in pre
        )
        # One login per distinct registry host (tags + cache share one host).
        logins = [c for c in pre if "get-login-password" in c]
        assert len(logins) == 1
        assert any(
            "docker buildx create" in c and "docker-container" in c and "--use" in c
            for c in pre
        )

    def test_one_build_command_per_image(self, tmp_path):
        doc, _ = self._spec_doc(_repo_tree(tmp_path))
        cmds = doc["phases"]["build"]["commands"]
        assert len(cmds) == 2

    def test_django_command_uses_same_buildx_args(self, tmp_path):
        doc, _ = self._spec_doc(_repo_tree(tmp_path))
        cmds = doc["phases"]["build"]["commands"]
        dj = next(c for c in cmds if "django:latest" in c)
        # Remote always pushes (CodeBuild auths to ECR directly) — never --load.
        assert "docker buildx build" in dj
        assert "--push" in dj and "--load" not in dj
        # Same cache refs + mode + ECR manifest flags as the local backend.
        assert f"--cache-from type=registry,ref={CACHE}:django-cache" in dj
        assert (
            f"--cache-to type=registry,ref={CACHE}:django-cache,mode=max,"
            "image-manifest=true,oci-mediatypes=true"
        ) in dj
        assert f"-t {HOST}/proj/django:latest" in dj
        assert "--build-arg BUILD_ENV=production" in dj
        assert "--platform linux/amd64" in dj

    def test_context_and_dockerfile_paths_are_relative_to_root(self, tmp_path):
        root = _repo_tree(tmp_path)
        doc, _ = self._spec_doc(root)
        cmds = doc["phases"]["build"]["commands"]
        dj = next(c for c in cmds if "django:latest" in c)
        ng = next(c for c in cmds if "nginx:latest" in c)
        # django builds from the repo root with a subdir Dockerfile.
        assert "-f compose/production/django/Dockerfile" in dj
        assert dj.rstrip().endswith(" .")
        # nginx builds from its own subdir (default Dockerfile → no -f).
        assert ng.rstrip().endswith(" compose/production/nginx")
        assert "-f " not in ng

    def test_no_cache_spec_emits_no_cache_flag(self, tmp_path):
        root = _repo_tree(tmp_path)
        spec = _nginx_spec(root)
        spec.no_cache = True
        text = generate_codebuild_buildspec([spec], root=root, region="us-east-2")
        cmd = yaml.safe_load(text)["phases"]["build"]["commands"][0]
        assert "--no-cache" in cmd


# ---------------------------------------------------------------------------
# Context tarring honoring .dockerignore
# ---------------------------------------------------------------------------


class TestZipContext:
    def _members(self, data: bytes) -> set[str]:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return set(zf.namelist())

    def test_zips_files_with_posix_paths(self, tmp_path):
        root = _repo_tree(tmp_path)
        members = self._members(zip_build_context(root))
        assert "backend/app.py" in members
        assert "compose/production/django/Dockerfile" in members

    def test_honors_dockerignore(self, tmp_path):
        root = _repo_tree(tmp_path)
        (root / "node_modules").mkdir()
        (root / "node_modules" / "big.js").write_text("x")
        (root / "secret.env").write_text("TOKEN=1")
        (root / ".dockerignore").write_text("node_modules\n*.env\n")
        members = self._members(zip_build_context(root))
        assert not any(m.startswith("node_modules") for m in members)
        assert "secret.env" not in members
        assert "backend/app.py" in members

    def test_keep_overrides_ignore(self, tmp_path):
        root = _repo_tree(tmp_path)
        (root / ".dockerignore").write_text("compose\n")
        # Without keep, the Dockerfile under compose/ would be dropped.
        keep = {"compose/production/django/Dockerfile"}
        members = self._members(zip_build_context(root, keep=keep))
        assert "compose/production/django/Dockerfile" in members


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_missing_role_raises_at_build(self, tmp_path):
        root = _repo_tree(tmp_path)
        backend = AwsCodeBuildBackend(
            session=FakeSession({}),
            codebuild=CodeBuildConfig(service_role_arn=None),
            project="proj",
        )
        with pytest.raises(CodeBuildError, match="service role"):
            backend.build_and_push([_nginx_spec(root)])

    def test_missing_session_raises(self, tmp_path):
        root = _repo_tree(tmp_path)
        backend = AwsCodeBuildBackend(
            session=None,
            codebuild=CodeBuildConfig(service_role_arn=ROLE),
            project="proj",
        )
        with pytest.raises(CodeBuildError, match="session"):
            backend.build_and_push([_nginx_spec(root)])

    def test_empty_specs_short_circuits(self):
        backend = AwsCodeBuildBackend(
            session=FakeSession({}), codebuild=CodeBuildConfig()
        )
        assert backend.build_and_push([]) == []


# ---------------------------------------------------------------------------
# Full build_and_push flow (mocked AWS)
# ---------------------------------------------------------------------------


def _clients(*, project_exists=False, build_statuses=("IN_PROGRESS", "SUCCEEDED")):
    s3 = mock.MagicMock()
    s3.head_bucket.side_effect = RuntimeError("404")  # bucket missing → create
    cb = mock.MagicMock()
    cb.batch_get_projects.return_value = {
        "projects": [{"name": "rc-build-proj"}] if project_exists else []
    }
    cb.start_build.return_value = {"build": {"id": "rc-build-proj:abc123"}}
    cb.batch_get_builds.side_effect = [
        {
            "builds": [
                {
                    "buildStatus": status,
                    "logs": {"groupName": "/aws/codebuild/rc", "streamName": "s1"},
                }
            ]
        }
        for status in build_statuses
    ]
    logs = mock.MagicMock()
    logs.get_log_events.return_value = {
        "events": [{"message": "STEP 1/5 building\n"}],
        "nextForwardToken": "f/1",
    }
    return {"s3": s3, "codebuild": cb, "logs": logs}


@pytest.fixture(autouse=True)
def _no_sleep():
    with mock.patch("remote_compose.image.backend.time.sleep"):
        yield


class TestBuildAndPushFlow:
    def _backend(self, clients, **cfg_kw):
        cfg = CodeBuildConfig(service_role_arn=ROLE, **cfg_kw)
        return AwsCodeBuildBackend(
            session=FakeSession(clients), codebuild=cfg, project="proj"
        )

    def test_tars_uploads_starts_and_returns_service_names(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients()
        backend = self._backend(clients)
        pushed = backend.build_and_push([_django_spec(root), _nginx_spec(root)])
        # contract: service names in input order.
        assert pushed == ["django", "nginx"]
        # context uploaded exactly once (one tar covers all images).
        assert clients["s3"].put_object.call_count == 1
        put = clients["s3"].put_object.call_args.kwargs
        assert put["Key"].endswith(".zip")
        assert isinstance(put["Body"], (bytes, bytearray))

    def test_start_build_targets_project_and_s3_source(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients()
        backend = self._backend(clients)
        backend.build_and_push([_nginx_spec(root)])
        sb = clients["codebuild"].start_build.call_args.kwargs
        assert sb["projectName"] == "rc-build-proj"
        assert sb["sourceTypeOverride"] == "S3"
        # source location = <bucket>/<key> matching the uploaded object.
        put = clients["s3"].put_object.call_args.kwargs
        assert sb["sourceLocationOverride"].endswith("/" + put["Key"])
        assert "docker buildx build" in sb["buildspecOverride"]
        env = {e["name"]: e["value"] for e in sb["environmentVariablesOverride"]}
        assert env["AWS_DEFAULT_REGION"] == "us-east-2"
        assert env["RC_BUILDCACHE_REPO"] == CACHE

    def test_ensure_project_creates_when_missing(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients(project_exists=False)
        backend = self._backend(clients)
        backend.build_and_push([_nginx_spec(root)])
        cb = clients["codebuild"]
        cb.create_project.assert_called_once()
        cp = cb.create_project.call_args.kwargs
        assert cp["name"] == "rc-build-proj"
        assert cp["serviceRole"] == ROLE
        assert cp["environment"]["computeType"] == "BUILD_GENERAL1_LARGE"
        assert cp["environment"]["privilegedMode"] is True
        assert cp["source"]["type"] == "S3"

    def test_ensure_project_reuses_when_present(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients(project_exists=True)
        backend = self._backend(clients)
        backend.build_and_push([_nginx_spec(root)])
        clients["codebuild"].create_project.assert_not_called()

    def test_configured_project_and_compute_override_defaults(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients(project_exists=False)
        backend = self._backend(
            clients, project_name="custom-proj", compute_type="BUILD_GENERAL1_2XLARGE"
        )
        backend.build_and_push([_nginx_spec(root)])
        cp = clients["codebuild"].create_project.call_args.kwargs
        assert cp["name"] == "custom-proj"
        assert cp["environment"]["computeType"] == "BUILD_GENERAL1_2XLARGE"
        assert clients["codebuild"].start_build.call_args.kwargs[
            "projectName"
        ] == "custom-proj"

    def test_configured_source_bucket_skips_derivation(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients()
        backend = self._backend(clients, source_bucket="my-bucket")
        backend.build_and_push([_nginx_spec(root)])
        sb = clients["codebuild"].start_build.call_args.kwargs
        assert sb["sourceLocationOverride"].startswith("my-bucket/")

    def test_streams_logs_to_progress(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients()
        events: list[str] = []
        cfg = CodeBuildConfig(service_role_arn=ROLE)
        backend = AwsCodeBuildBackend(
            session=FakeSession(clients),
            codebuild=cfg,
            project="proj",
            progress=events.append,
        )
        backend.build_and_push([_nginx_spec(root)])
        assert any("STEP 1/5 building" in e for e in events)
        assert any("started build rc-build-proj:abc123" in e for e in events)

    def test_failed_build_raises_with_log_tail(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients(build_statuses=("IN_PROGRESS", "FAILED"))
        clients["logs"].get_log_events.return_value = {
            "events": [{"message": "ERROR: pip install failed\n"}],
            "nextForwardToken": "f/1",
        }
        backend = self._backend(clients)
        with pytest.raises(CodeBuildError, match="FAILED"):
            backend.build_and_push([_nginx_spec(root)])

    def test_region_resolved_from_ecr_host_when_unset(self, tmp_path):
        # No region on config or backend — parsed from the ECR tag host.
        root = _repo_tree(tmp_path)
        clients = _clients()
        cfg = CodeBuildConfig(service_role_arn=ROLE)
        backend = AwsCodeBuildBackend(
            session=FakeSession(clients), codebuild=cfg, project="proj"
        )
        backend.build_and_push([_nginx_spec(root)])
        env = {
            e["name"]: e["value"]
            for e in clients["codebuild"].start_build.call_args.kwargs[
                "environmentVariablesOverride"
            ]
        }
        assert env["AWS_DEFAULT_REGION"] == "us-east-2"


class TestFactoryThreadsRemoteInputs:
    def test_create_build_backend_passes_session_and_config(self):
        sess = object()
        cfg = CodeBuildConfig(service_role_arn=ROLE)
        backend = create_build_backend(
            "aws-codebuild",
            session=sess,
            codebuild=cfg,
            project="proj",
            region="us-east-2",
        )
        assert isinstance(backend, AwsCodeBuildBackend)
        assert backend._session is sess
        assert backend._config is cfg
        assert backend._project == "proj"
        assert backend._region == "us-east-2"

    def test_local_backend_ignores_remote_inputs(self):
        # LocalBuildBackend must accept (and ignore) the remote kwargs so the
        # factory constructs every backend uniformly.
        backend = create_build_backend(
            "local", session=object(), codebuild=CodeBuildConfig(), project="p"
        )
        assert backend.name == "local"
