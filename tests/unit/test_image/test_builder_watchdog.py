"""rc-mtt: docker buildx with cache-to mode=max can hang indefinitely on
slow uplinks during the cache-push phase. ImageBuilder kills the build
after RC_BUILD_NO_PROGRESS_TIMEOUT_S (default 300s) of buildkit silence
and retries once without --cache-to.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from remote_compose.image.builder import (
    ImageBuildError,
    ImageBuildSpec,
    ImageBuilder,
    _disable_buildcache_set,
)


@pytest.fixture
def spec(tmp_path):
    ctx = tmp_path / "app"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM alpine\n")
    return ImageBuildSpec(
        service="web",
        context=ctx,
        tags=["myapp/web:latest"],
        cache_to=["111.dkr.ecr.us-east-1.amazonaws.com/p/cache:web"],
    )


def _make_fake_popen(*, exit_code=0, hang=False, lines=None):
    """Build a fake subprocess.Popen factory.

    hang=True: readline blocks forever (simulates buildkit silence).
    lines: list of stdout lines to emit before EOF.
    """
    lines = lines or []

    def _factory(cmd, *args, **kwargs):
        proc = mock.MagicMock()
        proc.cmd = cmd
        # poll returns None until we say it's done
        poll_results = [None] * 1000 + [exit_code]
        proc.poll = mock.Mock(side_effect=poll_results)
        proc.wait = mock.Mock(return_value=exit_code)

        if hang:
            # readline blocks until process is killed
            done = {"flag": False}
            def _readline():
                if done["flag"]:
                    return ""
                # Simulate blocking; we yield empty string only after kill
                import time as _t
                _t.sleep(0.05)
                return ""
            proc.stdout = mock.Mock()
            proc.stdout.readline = _readline
            proc.stderr = mock.Mock()
            proc.stderr.readline = _readline

            def _kill():
                done["flag"] = True
                proc.poll.side_effect = [exit_code or -9]
            proc.kill = _kill
        else:
            it_out = iter(lines + [""])
            it_err = iter([""])
            proc.stdout = mock.Mock()
            proc.stdout.readline.side_effect = lambda: next(it_out, "")
            proc.stderr = mock.Mock()
            proc.stderr.readline.side_effect = lambda: next(it_err, "")

        proc.stdout.close = mock.Mock()
        proc.stderr.close = mock.Mock()
        return proc

    return _factory


class TestWatchdogKillsHangAndRetriesWithoutCacheTo:
    def test_hang_triggers_retry_without_cache_to(self, spec, monkeypatch):
        monkeypatch.setenv("RC_BUILD_NO_PROGRESS_TIMEOUT_S", "1")
        events: list[str] = []
        # First call: hang. Second call (retry without cache-to): success.
        call_count = {"n": 0}

        def factory(cmd, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_fake_popen(hang=True)(cmd, *args, **kwargs)
            return _make_fake_popen(exit_code=0)(cmd, *args, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=factory), \
             mock.patch("subprocess.run") as srun:
            srun.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            tags = ImageBuilder(
                docker_bin="docker", progress=events.append,
            ).build(spec)

        assert tags == spec.tags
        # Must have hit the watchdog warning + retried.
        assert any(
            "buildkit went silent" in e and "cache-to" in e
            for e in events
        ), f"missing watchdog warning. events={events}"
        # Retry uses subprocess.run (no cache_to → simple path).
        assert srun.called, "retry should use subprocess.run after dropping cache_to"


class TestDisableBuildcacheEnvVar:
    def test_set_drops_cache_args_before_invocation(self, spec, monkeypatch):
        monkeypatch.setenv("RC_DISABLE_BUILDCACHE", "1")
        assert _disable_buildcache_set() is True
        events: list[str] = []
        with mock.patch("subprocess.run") as srun:
            srun.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImageBuilder(
                docker_bin="docker", progress=events.append,
            ).build(spec)
        # Should have used subprocess.run (no cache → simple path) and
        # NOT included --cache-to in the command.
        assert srun.called
        cmd = srun.call_args.args[0]
        assert not any(c.startswith("--cache-") for c in cmd), (
            f"cache args should be stripped when RC_DISABLE_BUILDCACHE=1. "
            f"cmd={cmd}"
        )

    def test_unset_passes_cache_args_through(self, spec, monkeypatch):
        monkeypatch.delenv("RC_DISABLE_BUILDCACHE", raising=False)
        assert _disable_buildcache_set() is False
