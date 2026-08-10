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

import itertools
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
    CODEBUILD_FINAL_DRAIN_SECONDS,
    RegistryAuth,
    RegistrySecretRef,
    create_build_backend,
    generate_codebuild_buildspec,
    parse_registry_auth,
    registry_auth_env_names,
    resolve_build_config,
    zip_build_context,
)
from remote_compose.image.builder import ImageBuildSpec

HOST = "111111111111.dkr.ecr.us-east-2.amazonaws.com"
CACHE = f"{HOST}/proj/buildcache"
ROLE = "arn:aws:iam::111111111111:role/rc-codebuild"
SECRET = "arn:aws:secretsmanager:us-east-2:111111111111:secret:rc/prod-AbCdEf"


def _dockerhub_auth(secret: str = SECRET) -> RegistryAuth:
    return RegistryAuth(
        registry="docker.io",
        username=RegistrySecretRef(secret_arn=secret, key="DOCKERHUB_USERNAME"),
        password=RegistrySecretRef(secret_arn=secret, key="DOCKERHUB_PASSWORD"),
    )


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
        assert (
            clients["codebuild"].start_build.call_args.kwargs["projectName"]
            == "custom-proj"
        )

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


# ---------------------------------------------------------------------------
# Log streaming / poll loop (rc-8j7.7 — CodeBuild log-drain / poll lag)
#
# The build is ~38s of real work but the deploy step measured ~6min: the loop
# drained ONE log page per POLL_INTERVAL sleep so it fell minutes behind a
# chatty stream. These lock the fix: drain every available page each poll, and
# on a terminal status do ONE bounded final drain instead of crawling
# CloudWatch to completion.
# ---------------------------------------------------------------------------


class FakeLogs:
    """A CloudWatch `logs` stand-in that serves paged events by token.

    ``get_log_events`` returns one page per call and advances the forward
    token; once the caller walks past the last page it hands back the SAME
    token with no events — exactly how real CloudWatch signals "caught up".
    """

    def __init__(self, pages: list[list[str]]):
        self._pages = pages
        self.call_count = 0

    def get_log_events(self, **kwargs):
        self.call_count += 1
        token = kwargs.get("nextToken")
        idx = 0 if token is None else int(token.split("/")[1])
        if idx >= len(self._pages):
            # Caught up: same token back, no new events.
            return {"events": [], "nextForwardToken": token or "f/0"}
        events = [{"message": m + "\n"} for m in self._pages[idx]]
        return {"events": events, "nextForwardToken": f"f/{idx + 1}"}


class TestStreamAndWait:
    def _backend(self, logs, progress=None):
        return AwsCodeBuildBackend(
            session=FakeSession({"logs": logs}),
            codebuild=CodeBuildConfig(service_role_arn=ROLE),
            project="proj",
            progress=progress,
        )

    def _cb(self, statuses):
        cb = mock.MagicMock()
        cb.batch_get_builds.side_effect = [
            {
                "builds": [
                    {
                        "buildStatus": s,
                        "logs": {"groupName": "/g", "streamName": "s1"},
                    }
                ]
            }
            for s in statuses
        ]
        return cb

    def test_drains_all_pages_and_stops_promptly_on_success(self):
        # Three pages of logs, then SUCCEEDED. Every line must surface, exactly
        # once, and the loop must NOT keep polling once terminal.
        logs = FakeLogs([["a1", "a2"], ["b1", "b2"], ["c1", "c2"]])
        cb = self._cb(["IN_PROGRESS", "SUCCEEDED"])
        seen: list[str] = []
        with mock.patch("remote_compose.image.backend.time.sleep") as slept:
            self._backend(logs, progress=seen.append)._stream_and_wait(
                cb, "bid", "us-east-2"
            )
        # nothing dropped, nothing duplicated.
        for line in ("a1", "a2", "b1", "b2", "c1", "c2"):
            assert sum(1 for e in seen if line in e) == 1
        # exactly the two status checks — did not loop for extra intervals.
        assert cb.batch_get_builds.call_count == 2
        # slept only while running (once), never after terminal.
        assert slept.call_count == 1
        # bounded log reads: 3 pages + a caught-up probe + the final-drain
        # probes — not an unbounded crawl.
        assert logs.call_count <= 8

    def test_failed_status_raises_with_log_tail(self):
        logs = FakeLogs([["ERROR: pip install failed", "  see trace above"]])
        cb = self._cb(["IN_PROGRESS", "FAILED"])
        with mock.patch("remote_compose.image.backend.time.sleep"):
            with pytest.raises(CodeBuildError) as exc:
                self._backend(logs)._stream_and_wait(cb, "bid", "us-east-2")
        assert "FAILED" in str(exc.value)
        assert "ERROR: pip install failed" in str(exc.value)

    def test_final_drain_is_time_bounded_on_endless_stream(self):
        # A stream that NEVER reports caught up: every page advances the token
        # AND carries an event. The bounded final drain must stop at the time
        # cap instead of crawling CloudWatch forever.
        counter = itertools.count()

        def endless(**kwargs):
            i = next(counter)
            return {
                "events": [{"message": f"line {i}\n"}],
                "nextForwardToken": f"f/{i}",
            }

        logs = mock.MagicMock()
        logs.get_log_events.side_effect = endless
        # Terminal on the first status check → straight into the bounded final
        # drain (skips the unbounded running-phase drain).
        cb = self._cb(["SUCCEEDED"])
        # Fake clock: base 0 sets deadline = CODEBUILD_FINAL_DRAIN_SECONDS; a
        # few ticks later we cross it and the drain must bail.
        ticks = iter(
            [
                0.0,
                1.0,
                2.0,
                CODEBUILD_FINAL_DRAIN_SECONDS + 1,
                CODEBUILD_FINAL_DRAIN_SECONDS + 2,
            ]
        )
        with (
            mock.patch("remote_compose.image.backend.time.sleep"),
            mock.patch(
                "remote_compose.image.backend.time.monotonic",
                side_effect=lambda: next(ticks, CODEBUILD_FINAL_DRAIN_SECONDS + 99),
            ),
        ):
            # returns (does not hang) despite the never-ending stream.
            self._backend(logs)._stream_and_wait(cb, "bid", "us-east-2")
        assert logs.get_log_events.call_count < 50

    def test_running_drain_is_bounded_so_status_is_rechecked(self):
        # Regression (rc-8j7.7 round 2): while IN_PROGRESS, CloudWatch keeps
        # handing back advancing pages (ingestion lag) and never reports caught
        # up. The RUNNING-phase drain must be bounded by the poll interval so the
        # loop re-checks buildStatus and reaches the terminal status — instead of
        # one unbounded drain chasing the stream for minutes past a finished
        # build (the ~5-min live deploy-step regression the first fix missed).
        counter = itertools.count()

        def endless(**kwargs):
            i = next(counter)
            return {
                "events": [{"message": f"line {i}\n"}],
                "nextForwardToken": f"f/{i}",
            }

        logs = mock.MagicMock()
        logs.get_log_events.side_effect = endless
        # Two IN_PROGRESS checks then SUCCEEDED: the loop can only reach the 3rd
        # status if each running drain RETURNS (bounded) instead of swallowing it.
        cb = self._cb(["IN_PROGRESS", "IN_PROGRESS", "SUCCEEDED"])
        clock = itertools.count(0, 1.0)  # monotonic advances 1s per call
        with (
            mock.patch("remote_compose.image.backend.time.sleep"),
            mock.patch(
                "remote_compose.image.backend.time.monotonic",
                side_effect=lambda: next(clock),
            ),
        ):
            # Must return (not hang) and reach the terminal status check.
            self._backend(logs)._stream_and_wait(cb, "bid", "us-east-2")
        # The loop re-checked status all three times — the running drain didn't
        # block it chasing the endless stream.
        assert cb.batch_get_builds.call_count == 3
        assert logs.get_log_events.call_count < 100


# ---------------------------------------------------------------------------
# rc-6ej: registry auth (authenticated base-image pulls)
# ---------------------------------------------------------------------------


class TestRegistryAuthParsing:
    """`registry_auth` config → RegistryAuth entries, with the malformed cases
    failing as config errors (ValueError → ProviderConfigError) before AWS."""

    RAW = [
        {
            "registry": "docker.io",
            "username": {"secret_arn": SECRET, "key": "DOCKERHUB_USERNAME"},
            "password": {"secret_arn": SECRET, "key": "DOCKERHUB_PASSWORD"},
        }
    ]

    def test_absent_is_empty(self):
        for raw in (None, "", [], ()):
            assert parse_registry_auth(raw) == ()

    def test_parses_registry_and_secret_refs(self):
        (auth,) = parse_registry_auth(self.RAW)
        assert auth.registry == "docker.io"
        assert auth.username.secret_arn == SECRET
        assert auth.username.key == "DOCKERHUB_USERNAME"
        assert auth.password.key == "DOCKERHUB_PASSWORD"

    def test_key_is_optional_whole_secret_value(self):
        (auth,) = parse_registry_auth(
            [
                {
                    "registry": "ghcr.io",
                    "username": {"secret_arn": SECRET},
                    "password": {"secret_arn": SECRET},
                }
            ]
        )
        assert auth.username.key is None
        assert auth.username.codebuild_value() == SECRET

    def test_codebuild_value_is_arn_colon_key(self):
        (auth,) = parse_registry_auth(self.RAW)
        assert auth.username.codebuild_value() == f"{SECRET}:DOCKERHUB_USERNAME"

    def test_json_string_accepted_for_env_override(self):
        import json

        (auth,) = parse_registry_auth(json.dumps(self.RAW))
        assert auth.registry == "docker.io"

    def test_unparseable_json_raises(self):
        with pytest.raises(ValueError, match="registry_auth"):
            parse_registry_auth("{not json")

    def test_plaintext_credential_rejected(self):
        # The whole point: a password can only ever be a Secrets Manager ref.
        with pytest.raises(ValueError, match="never a plaintext credential"):
            parse_registry_auth(
                [
                    {
                        "registry": "docker.io",
                        "username": {"secret_arn": SECRET},
                        "password": "hunter2",
                    }
                ]
            )

    def test_missing_registry_raises(self):
        with pytest.raises(ValueError, match="'registry' is required"):
            parse_registry_auth(
                [
                    {
                        "username": {"secret_arn": SECRET},
                        "password": {"secret_arn": SECRET},
                    }
                ]
            )

    def test_missing_secret_arn_raises(self):
        with pytest.raises(ValueError, match="'secret_arn' is required"):
            parse_registry_auth(
                [
                    {
                        "registry": "docker.io",
                        "username": {"key": "U"},
                        "password": {"secret_arn": SECRET},
                    }
                ]
            )

    def test_unknown_keys_raise(self):
        with pytest.raises(ValueError, match="unknown key"):
            parse_registry_auth([dict(self.RAW[0], registy="typo")])
        with pytest.raises(ValueError, match="unknown key"):
            parse_registry_auth(
                [dict(self.RAW[0], username={"secret_arn": SECRET, "jsonKey": "U"})]
            )

    def test_duplicate_registry_raises(self):
        with pytest.raises(ValueError, match="duplicate registry"):
            parse_registry_auth(self.RAW + self.RAW)

    def test_colon_in_key_raises(self):
        # CodeBuild parses a SECRETS_MANAGER value as secret-id:json-key:...,
        # so a ':' in the key would silently become a version selector.
        with pytest.raises(ValueError, match="must not contain ':'"):
            parse_registry_auth(
                [dict(self.RAW[0], username={"secret_arn": SECRET, "key": "a:b"})]
            )

    def test_shell_unsafe_registry_raises(self):
        with pytest.raises(ValueError, match="invalid registry"):
            parse_registry_auth([dict(self.RAW[0], registry='docker.io"; rm -rf /')])

    def test_env_names_are_derived_from_host(self):
        assert registry_auth_env_names("docker.io") == (
            "RC_REGISTRY_USER_DOCKER_IO",
            "RC_REGISTRY_PASS_DOCKER_IO",
        )
        assert registry_auth_env_names("myreg.example.com:5000")[0] == (
            "RC_REGISTRY_USER_MYREG_EXAMPLE_COM_5000"
        )

    def test_distinct_registries_get_distinct_env_vars(self):
        auths = parse_registry_auth(
            [self.RAW[0], dict(self.RAW[0], registry="ghcr.io")]
        )
        names = {n for a in auths for n in a.env_names()}
        assert len(names) == 4


class TestRegistryAuthConfigResolution:
    """registry_auth resolves through the same env > provider_config > rc.yml
    precedence as every other codebuild knob."""

    PC = {
        "ecs": {
            "build": {
                "backend": "aws-codebuild",
                "codebuild": {
                    "service_role_arn": ROLE,
                    "registry_auth": TestRegistryAuthParsing.RAW,
                },
            }
        }
    }

    def test_resolved_from_provider_config(self):
        cfg = resolve_build_config(self.PC, {}, env={})
        assert [a.registry for a in cfg.codebuild.registry_auth] == ["docker.io"]

    def test_default_is_empty_tuple(self):
        cfg = resolve_build_config(
            {
                "ecs": {
                    "build": {
                        "backend": "aws-codebuild",
                        "codebuild": {"service_role_arn": ROLE},
                    }
                }
            },
            {},
            env={},
        )
        assert cfg.codebuild.registry_auth == ()

    def test_env_override_wins(self):
        import json

        cfg = resolve_build_config(
            self.PC,
            {},
            env={
                "RC_CODEBUILD_REGISTRY_AUTH": json.dumps(
                    [
                        {
                            "registry": "ghcr.io",
                            "username": {"secret_arn": SECRET, "key": "U"},
                            "password": {"secret_arn": SECRET, "key": "P"},
                        }
                    ]
                )
            },
        )
        assert [a.registry for a in cfg.codebuild.registry_auth] == ["ghcr.io"]

    def test_malformed_config_raises_value_error(self):
        bad = {
            "ecs": {
                "build": {"codebuild": {"registry_auth": [{"registry": "docker.io"}]}}
            }
        }
        with pytest.raises(ValueError, match="registry_auth"):
            resolve_build_config(bad, {}, env={})


class TestRegistryAuthBuildspec:
    def _pre(self, root, **kw):
        text = generate_codebuild_buildspec(
            [_nginx_spec(root)], root=root, region="us-east-2", **kw
        )
        return yaml.safe_load(text)["phases"]["pre_build"]["commands"], text

    def test_omitted_reproduces_byte_identical_buildspec(self, tmp_path):
        """Back-compat: no registry_auth → the pre-rc-6ej document, exactly."""
        root = _repo_tree(tmp_path)
        specs = [_django_spec(root), _nginx_spec(root)]
        base = generate_codebuild_buildspec(specs, root=root, region="us-east-2")
        explicit_empty = generate_codebuild_buildspec(
            specs, root=root, region="us-east-2", registry_auth=()
        )
        assert base == explicit_empty
        assert "docker login" in base  # the ECR login is still there
        assert "RC_REGISTRY_" not in base

    def test_emits_guarded_login_per_registry(self, tmp_path):
        root = _repo_tree(tmp_path)
        pre, _ = self._pre(root, registry_auth=[_dockerhub_auth()])
        login = next(c for c in pre if "docker login docker.io" in c)
        # Guarded on BOTH vars so an unset/failed secret degrades to anonymous.
        assert '[ -n "${RC_REGISTRY_USER_DOCKER_IO:-}" ]' in login
        assert '[ -n "${RC_REGISTRY_PASS_DOCKER_IO:-}" ]' in login
        # Password over stdin — never on the command line (argv is not secret).
        assert "--password-stdin" in login
        assert 'echo "${RC_REGISTRY_PASS_DOCKER_IO}" | docker login' in login
        assert '--username "${RC_REGISTRY_USER_DOCKER_IO}"' in login

    def test_login_never_fails_the_build(self, tmp_path):
        root = _repo_tree(tmp_path)
        pre, _ = self._pre(root, registry_auth=[_dockerhub_auth()])
        login = next(c for c in pre if "docker login docker.io" in c)
        # A bad credential logs and continues rather than aborting a deploy
        # that would have worked anonymously.
        assert "|| echo" in login and "FAILED" in login
        assert "else echo" in login

    def test_login_precedes_buildx_create(self, tmp_path):
        """`buildx create --bootstrap` pulls moby/buildkit from Docker Hub, so
        it is itself a rate-limited anonymous pull unless the login came first."""
        root = _repo_tree(tmp_path)
        pre, _ = self._pre(root, registry_auth=[_dockerhub_auth()])
        login_at = next(i for i, c in enumerate(pre) if "docker login docker.io" in c)
        create_at = next(i for i, c in enumerate(pre) if "docker buildx create" in c)
        assert login_at < create_at

    def test_ecr_logins_still_emitted(self, tmp_path):
        root = _repo_tree(tmp_path)
        pre, _ = self._pre(root, registry_auth=[_dockerhub_auth()])
        assert any("aws ecr get-login-password" in c and HOST in c for c in pre)

    def test_one_login_per_registry(self, tmp_path):
        root = _repo_tree(tmp_path)
        ghcr = RegistryAuth(
            registry="ghcr.io",
            username=RegistrySecretRef(secret_arn=SECRET, key="GHCR_USER"),
            password=RegistrySecretRef(secret_arn=SECRET, key="GHCR_TOKEN"),
        )
        pre, _ = self._pre(root, registry_auth=[_dockerhub_auth(), ghcr])
        assert len([c for c in pre if "docker login docker.io" in c]) == 1
        assert len([c for c in pre if "docker login ghcr.io" in c]) == 1
        assert "RC_REGISTRY_PASS_GHCR_IO" in "\n".join(pre)

    def test_no_secret_material_in_buildspec(self, tmp_path):
        """Only env-var NAMES and the secret ARN reach the buildspec — the
        values are resolved by CodeBuild inside the build container."""
        root = _repo_tree(tmp_path)
        _, text = self._pre(root, registry_auth=[_dockerhub_auth()])
        assert SECRET not in text
        assert "DOCKERHUB_PASSWORD" not in text


class TestRegistryAuthEnvInjection:
    def _run(self, tmp_path, registry_auth):
        root = _repo_tree(tmp_path)
        clients = _clients()
        backend = AwsCodeBuildBackend(
            session=FakeSession(clients),
            codebuild=CodeBuildConfig(
                service_role_arn=ROLE, registry_auth=tuple(registry_auth)
            ),
            project="proj",
        )
        backend.build_and_push([_nginx_spec(root)])
        return clients["codebuild"].start_build.call_args.kwargs

    def test_injected_as_secrets_manager_type(self, tmp_path):
        sb = self._run(tmp_path, [_dockerhub_auth()])
        env = {e["name"]: e for e in sb["environmentVariablesOverride"]}
        user = env["RC_REGISTRY_USER_DOCKER_IO"]
        password = env["RC_REGISTRY_PASS_DOCKER_IO"]
        # SECRETS_MANAGER (never PLAINTEXT) → CodeBuild resolves + masks it.
        assert user["type"] == "SECRETS_MANAGER"
        assert password["type"] == "SECRETS_MANAGER"
        assert user["value"] == f"{SECRET}:DOCKERHUB_USERNAME"
        assert password["value"] == f"{SECRET}:DOCKERHUB_PASSWORD"

    def test_no_plaintext_env_var_carries_a_credential(self, tmp_path):
        sb = self._run(tmp_path, [_dockerhub_auth()])
        plaintext = [
            e for e in sb["environmentVariablesOverride"] if e["type"] == "PLAINTEXT"
        ]
        assert {e["name"] for e in plaintext} == {
            "AWS_DEFAULT_REGION",
            "RC_BUILDCACHE_REPO",
        }

    def test_omitted_leaves_env_overrides_unchanged(self, tmp_path):
        sb = self._run(tmp_path, [])
        names = [e["name"] for e in sb["environmentVariablesOverride"]]
        assert names == ["AWS_DEFAULT_REGION", "RC_BUILDCACHE_REPO"]
        assert "RC_REGISTRY_" not in sb["buildspecOverride"]

    def test_buildspec_and_env_var_names_agree(self, tmp_path):
        """The buildspec's guard reads exactly the vars start_build injects."""
        sb = self._run(tmp_path, [_dockerhub_auth()])
        for e in sb["environmentVariablesOverride"]:
            if e["type"] == "SECRETS_MANAGER":
                assert f'"${{{e["name"]}:-}}"' in sb["buildspecOverride"]

    def test_progress_reports_registries_without_secrets(self, tmp_path):
        root = _repo_tree(tmp_path)
        clients = _clients()
        events: list[str] = []
        AwsCodeBuildBackend(
            session=FakeSession(clients),
            codebuild=CodeBuildConfig(
                service_role_arn=ROLE, registry_auth=(_dockerhub_auth(),)
            ),
            project="proj",
            progress=events.append,
        ).build_and_push([_nginx_spec(root)])
        assert any("registry auth configured for docker.io" in e for e in events)
        assert not any(SECRET in e for e in events)
