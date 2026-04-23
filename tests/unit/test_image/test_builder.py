"""Unit tests for remote_compose.image.builder."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.image.builder import (
    ImageBuildError,
    ImageBuildSpec,
    ImageBuilder,
)


@pytest.fixture
def spec(tmp_path):
    ctx = tmp_path / "app"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM alpine\n")
    return ImageBuildSpec(
        service="web",
        context=ctx,
        tags=["myapp/web:abc123", "myapp/web:latest"],
    )


class TestImageBuilder:
    def test_build_invokes_docker_with_all_tags(self, spec):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            tags = ImageBuilder(docker_bin="docker").build(spec)
        assert tags == spec.tags
        called_cmd = run.call_args.args[0]
        assert "-t" in called_cmd
        for tag in spec.tags:
            assert tag in called_cmd

    def test_build_missing_tags_raises(self, tmp_path):
        with pytest.raises(ImageBuildError, match="no tags"):
            ImageBuilder().build(
                ImageBuildSpec(service="w", context=tmp_path, tags=[])
            )

    def test_build_nonzero_exit_raises(self, spec):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="oops")
            with pytest.raises(ImageBuildError, match="oops"):
                ImageBuilder(docker_bin="docker").build(spec)

    def test_build_args_passed(self, spec):
        spec.build_args = {"VERSION": "1.2.3", "ARG2": "val"}
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(docker_bin="docker").build(spec)
        cmd = run.call_args.args[0]
        assert "VERSION=1.2.3" in cmd
        assert "ARG2=val" in cmd

    def test_platform_passed(self, spec):
        spec.platform = "linux/amd64"
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(docker_bin="docker").build(spec)
        cmd = run.call_args.args[0]
        assert "--platform" in cmd
        assert "linux/amd64" in cmd

    def test_dockerfile_override(self, spec, tmp_path):
        spec.dockerfile = tmp_path / "Dockerfile.prod"
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(docker_bin="docker").build(spec)
        cmd = run.call_args.args[0]
        assert "-f" in cmd
        assert str(spec.dockerfile) in cmd

    def test_relative_dockerfile_resolved_against_context(self, spec):
        # docker resolves a relative -f against the caller's cwd, NOT the
        # build context. Builder must join a relative dockerfile path to the
        # context so compose-style "compose/foo/Dockerfile" works.
        spec.dockerfile = Path("compose/production/django/Dockerfile")
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(docker_bin="docker").build(spec)
        cmd = run.call_args.args[0]
        f_idx = cmd.index("-f")
        passed = cmd[f_idx + 1]
        assert passed == str(spec.context / spec.dockerfile)
        assert Path(passed).is_absolute()

    def test_target_stage(self, spec):
        spec.target = "production"
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(docker_bin="docker").build(spec)
        cmd = run.call_args.args[0]
        assert "--target" in cmd
        assert "production" in cmd

    def test_progress_callback_invoked(self, spec):
        events: list[str] = []
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(docker_bin="docker", progress=events.append).build(spec)
        assert any("docker build" in e for e in events)
