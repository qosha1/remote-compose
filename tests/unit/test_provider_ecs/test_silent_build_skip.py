"""Failing-RED tests for remote-compose-8q4.

When _build_and_push_images skips images for any reason — no service has
build_context, terraform outputs missing ecr_repositories, or per-service
repo_url not in outputs — the provider must emit a VISIBLE progress message
so the user knows builds didn't run. Today these paths silently return / only
append to result.warnings, which gets lost in 65-resource apply output. See
sentinal incident (remote-compose-47z): ECR repos stayed empty after rc up
because the build phase silently skipped, no breadcrumb told the user why,
tasks then failed with CannotPullContainerError 49 minutes later.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import RecordingTerraformRunner


def _ctx(tmp_path: Path, services: dict) -> DeployContext:
    return DeployContext(
        project="silent-skip-test",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "silent-skip-test",
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
def empty_outputs_runner(tmp_path):
    runner = RecordingTerraformRunner(tmp_path / "terraform")
    runner.script("output", "{}")
    return runner


@pytest.fixture
def healthy_runner(tmp_path):
    runner = RecordingTerraformRunner(tmp_path / "terraform")
    runner.script(
        "output",
        '{"ecr_repositories": {"value": {'
        '"api": "111.dkr.ecr.us-east-1.amazonaws.com/silent-skip-test/api"'
        "}}}",
    )
    return runner


class TestSilentBuildSkipEmitsVisibleProgress:
    """When the build phase produces zero pushes, the user MUST see a line
    explaining why, not just a tucked-away result.warning."""

    def test_no_build_context_emits_progress_line(
        self,
        tmp_path,
        mock_session,
        healthy_runner,
    ):
        ctx = _ctx(
            tmp_path,
            {
                "image-only": ServiceSpec(
                    name="image-only",
                    cpu=256,
                    memory=512,
                    type="application",
                    image="nginx:alpine",
                ),
            },
        )
        progress_lines: list[str] = []
        provider = ECSProvider(
            runner_factory=lambda d: healthy_runner,
            session_factory=lambda c: mock_session,
            progress=progress_lines.append,
        )
        provider.deploy(ctx)
        # Today nothing is emitted; this assertion fails until 8q4 is fixed.
        assert any(
            "build" in line.lower()
            and (
                "no images" in line.lower()
                or "0 images" in line.lower()
                or "skipped" in line.lower()
            )
            for line in progress_lines
        ), (
            "expected a visible progress line about no images being built; "
            f"got: {progress_lines!r}"
        )

    def test_missing_terraform_outputs_emits_progress_line(
        self,
        tmp_path,
        mock_session,
        empty_outputs_runner,
    ):
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
        progress_lines: list[str] = []
        provider = ECSProvider(
            runner_factory=lambda d: empty_outputs_runner,
            session_factory=lambda c: mock_session,
            progress=progress_lines.append,
        )
        provider.deploy(ctx)
        assert any(
            "ecr_repositories" in line.lower()
            or "missing" in line.lower()
            and "build" in line.lower()
            for line in progress_lines
        ), (
            "expected a visible progress line that the build phase was "
            f"skipped due to missing tf outputs; got: {progress_lines!r}"
        )

    def test_per_service_missing_repo_url_emits_stderr_warn(
        self,
        tmp_path,
        mock_session,
    ):
        # Service builds, but its name isn't in the outputs map. Today this
        # appends to result.warnings silently. After 8q4 it must call
        # self._emit so the user sees it during the deploy.
        api_ctx = tmp_path / "ghost"
        api_ctx.mkdir()
        (api_ctx / "Dockerfile").write_text("FROM alpine\n")
        ctx = _ctx(
            tmp_path,
            {
                "ghost": ServiceSpec(
                    name="ghost",
                    cpu=256,
                    memory=512,
                    type="application",
                    build_context=api_ctx,
                ),
            },
        )
        runner = RecordingTerraformRunner(tmp_path / "terraform")
        runner.script(
            "output",
            '{"ecr_repositories": {"value": {'
            '"different-svc": "111.dkr.ecr.us-east-1.amazonaws.com/x/y"'
            "}}}",
        )
        progress_lines: list[str] = []
        provider = ECSProvider(
            runner_factory=lambda d: runner,
            session_factory=lambda c: mock_session,
            progress=progress_lines.append,
        )
        provider.deploy(ctx)
        assert any(
            "ghost" in line and ("no ECR repo" in line or "skipping" in line)
            for line in progress_lines
        ), (
            "expected a per-service stderr WARN that ghost was skipped; "
            f"got: {progress_lines!r}"
        )
