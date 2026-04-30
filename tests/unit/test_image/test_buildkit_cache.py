"""BuildKit registry cache wiring (rc-e5u.45.2).

The builder routes through ``docker buildx build`` whenever ``cache_from``
or ``cache_to`` is set, and emits the matching ``--cache-from`` /
``--cache-to`` args with ``mode=max`` (so intermediate stages survive
across machines + CI runs).

Provider-side wiring lives in ``test_provider_ecs/test_image_deploy.py``;
this file is the focused unit test for ImageBuilder behavior.
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

    rc-mtt: when cache_to is set, the builder routes through Popen + a
    no-progress watchdog instead of subprocess.run. Mock both so tests
    don't care which path the builder picks.
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
    with mock.patch("subprocess.run") as run, \
         mock.patch("subprocess.Popen", side_effect=_fake_popen):
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ImageBuilder(docker_bin="docker").build(spec)
    if "cmd" in captured:
        return captured["cmd"]
    return run.call_args.args[0]


class TestNoCacheUsesClassicDockerBuild:
    def test_default_path_is_docker_build(self, spec):
        cmd = _run(spec)
        # No buildx subcommand when cache args are absent — preserves
        # compatibility with docker installs lacking the buildx plugin.
        assert cmd[:2] == ["docker", "build"]
        assert "buildx" not in cmd
        assert not any(c.startswith("--cache-") for c in cmd)


class TestCacheFromSwitchesToBuildx:
    def test_cache_from_alone_routes_through_buildx(self, spec):
        spec.cache_from = ["111.dkr.ecr.us-east-1.amazonaws.com/p/buildcache:web-cache"]
        cmd = _run(spec)
        assert cmd[:3] == ["docker", "buildx", "build"]
        assert "--load" in cmd, (
            "buildx must --load so the local image store has the tag for "
            "the subsequent docker push to find."
        )

    def test_cache_from_arg_format(self, spec):
        ref = "111.dkr.ecr.us-east-1.amazonaws.com/p/buildcache:web-cache"
        spec.cache_from = [ref]
        cmd = _run(spec)
        idx = cmd.index("--cache-from")
        assert cmd[idx + 1] == f"type=registry,ref={ref}"

    def test_multiple_cache_from_refs_each_get_a_flag(self, spec):
        spec.cache_from = [
            "1.example.com/c:a",
            "2.example.com/c:b",
        ]
        cmd = _run(spec)
        cf_args = [
            cmd[i + 1] for i, c in enumerate(cmd) if c == "--cache-from"
        ]
        assert cf_args == [
            "type=registry,ref=1.example.com/c:a",
            "type=registry,ref=2.example.com/c:b",
        ]


class TestCacheToWritesModeMax:
    def test_cache_to_includes_mode_max(self, spec):
        # mode=max is the load-bearing detail: without it cache only
        # exports the final stage, so multi-stage Dockerfiles see no hit
        # on intermediate (e.g. pip install) layers.
        ref = "111.dkr.ecr.us-east-1.amazonaws.com/p/buildcache:web-cache"
        spec.cache_to = [ref]
        cmd = _run(spec)
        idx = cmd.index("--cache-to")
        assert cmd[idx + 1] == f"type=registry,ref={ref},mode=max"

    def test_cache_to_alone_also_routes_through_buildx(self, spec):
        spec.cache_to = ["1.example.com/c:a"]
        cmd = _run(spec)
        assert cmd[:3] == ["docker", "buildx", "build"]


class TestCacheCombinedWithOtherFlags:
    def test_cache_alongside_target_platform_buildargs(self, spec):
        spec.cache_from = ["1.example.com/c:web-cache"]
        spec.cache_to = ["1.example.com/c:web-cache"]
        spec.target = "production"
        spec.platform = "linux/amd64"
        spec.build_args = {"VERSION": "1.0"}
        cmd = _run(spec)
        # All flags coexist on the buildx command line.
        assert "--target" in cmd and "production" in cmd
        assert "--platform" in cmd and "linux/amd64" in cmd
        assert "VERSION=1.0" in cmd
        assert any(c == "--cache-from" for c in cmd)
        assert any(c == "--cache-to" for c in cmd)
        # Tag still applied.
        assert "-t" in cmd
