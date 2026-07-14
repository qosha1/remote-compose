"""rc-8j7.4: configurable cache export mode + optional buildx --push.

The builder keeps mode=max + --load as the zero-config default (the pip
stage needs mode=max; --load feeds the separate docker push). This file
locks the two opt-in variants — mode=min and buildx --push — against the
exact argv the builder emits.
"""

from __future__ import annotations

from unittest import mock

import pytest

from remote_compose.image.builder import ImageBuildSpec, ImageBuilder


@pytest.fixture
def spec(tmp_path):
    ctx = tmp_path / "app"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM alpine\n")
    return ImageBuildSpec(
        service="web",
        context=ctx,
        tags=["111.dkr.ecr.us-east-1.amazonaws.com/p/web:latest"],
    )


def _run(spec):
    """Invoke the builder with subprocess mocked; return the captured argv.

    Mirrors the helper in test_buildkit_cache: cache_to routes through
    Popen + watchdog, everything else through subprocess.run — mock both.
    """
    captured: dict = {}

    def _fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
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
        mock.patch("subprocess.run") as run,
        mock.patch("subprocess.Popen", side_effect=_fake_popen),
    ):
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ImageBuilder(docker_bin="docker").build(spec)
    if "cmd" in captured:
        return captured["cmd"]
    return run.call_args.args[0]


class TestCacheMode:
    def test_default_mode_is_max(self, spec):
        ref = "111.dkr.ecr.us-east-1.amazonaws.com/p/buildcache:web-cache"
        spec.cache_to = [ref]
        cmd = _run(spec)
        idx = cmd.index("--cache-to")
        assert cmd[idx + 1] == (
            f"type=registry,ref={ref},mode=max"
            ",image-manifest=true,oci-mediatypes=true"
        )

    def test_mode_min_opt_in(self, spec):
        ref = "111.dkr.ecr.us-east-1.amazonaws.com/p/buildcache:web-cache"
        spec.cache_to = [ref]
        spec.cache_mode = "min"
        cmd = _run(spec)
        idx = cmd.index("--cache-to")
        assert cmd[idx + 1] == (
            f"type=registry,ref={ref},mode=min"
            ",image-manifest=true,oci-mediatypes=true"
        )
        # image-manifest/oci-mediatypes still needed for ECR regardless of mode
        assert "image-manifest=true" in cmd[idx + 1]


class TestBuildxPush:
    def test_default_uses_load_not_push(self, spec):
        spec.cache_from = ["1.example.com/c:web-cache"]
        cmd = _run(spec)
        assert "--load" in cmd
        assert "--push" not in cmd

    def test_push_replaces_load(self, spec):
        spec.cache_from = ["1.example.com/c:web-cache"]
        spec.cache_to = ["1.example.com/c:web-cache"]
        spec.push = True
        cmd = _run(spec)
        assert "--push" in cmd
        assert "--load" not in cmd
        # still a buildx build
        assert cmd[:3] == ["docker", "buildx", "build"]

    def test_push_forces_buildx_even_without_cache(self, spec):
        # No cache args, but push=True must still route through buildx
        # (classic `docker build` can't build-and-push in one step).
        spec.push = True
        cmd = _run(spec)
        assert cmd[:3] == ["docker", "buildx", "build"]
        assert "--push" in cmd
        assert "--load" not in cmd
