"""rc-8j7.1: BuildBackend seam + LocalBuildBackend + backend registry.

The seam makes WHERE a build happens pluggable. The default `local`
backend wraps today's ImageBuilder + ImagePusher flow; a registry keyed by
name lets a configured deploy swap in a remote backend (rc-8j7.5) without
the provider caring. LocalBuildBackend is a pure refactor of the inline
build+push loop, so the existing provider tests are the real end-to-end
proof; this file locks the seam contract in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import threading
import time

from remote_compose.image import backend as backend_mod
from remote_compose.image.backend import (
    DEFAULT_BUILD_BACKEND,
    DEFAULT_BUILD_MAX_WORKERS,
    AwsCodeBuildBackend,
    BuildBackend,
    BuildConfig,
    LocalBuildBackend,
    UnknownBuildBackendError,
    available_backends,
    create_build_backend,
    resolve_build_config,
)
from remote_compose.image.builder import ImageBuildSpec


def _spec(name: str) -> ImageBuildSpec:
    return ImageBuildSpec(
        service=name,
        context=Path("/tmp") / name,
        tags=[f"1.dkr.ecr.us-east-1.amazonaws.com/p/{name}:latest"],
    )


class TestRegistry:
    def test_default_is_local(self):
        assert DEFAULT_BUILD_BACKEND == "local"

    def test_local_registered_and_available(self):
        assert "local" in available_backends()

    def test_create_local_returns_backend(self):
        backend = create_build_backend("local")
        assert isinstance(backend, LocalBuildBackend)
        assert isinstance(backend, BuildBackend)

    def test_unknown_backend_raises_with_available_list(self):
        with pytest.raises(UnknownBuildBackendError, match="local"):
            create_build_backend("does-not-exist")


class TestAwsCodeBuildRegistration:
    """rc-8j7.5: the aws-codebuild backend resolves + constructs through the
    same seam (behavior is covered in test_codebuild_backend.py)."""

    def test_registered_and_constructible(self):
        assert "aws-codebuild" in available_backends()
        backend = create_build_backend("aws-codebuild")
        assert isinstance(backend, AwsCodeBuildBackend)
        assert isinstance(backend, BuildBackend)

    def test_config_resolves_to_it_with_role(self):
        cfg = resolve_build_config(
            {
                "ecs": {
                    "build": {
                        "backend": "aws-codebuild",
                        "codebuild": {"service_role_arn": "arn:aws:iam::1:role/r"},
                    }
                }
            },
            {},
            env={},
        )
        assert cfg.backend == "aws-codebuild"
        assert cfg.codebuild.service_role_arn == "arn:aws:iam::1:role/r"

    def test_missing_role_errors_clearly(self):
        with pytest.raises(ValueError, match="service_role_arn"):
            resolve_build_config(
                {"ecs": {"build": {"backend": "aws-codebuild"}}}, {}, env={}
            )

    def test_empty_specs_is_noop(self):
        backend = create_build_backend("aws-codebuild")
        assert backend.build_and_push([]) == []


class TestLocalBuildBackendDelegates:
    """LocalBuildBackend builds each spec, pushes its tags, and returns the
    service names — exactly what the inline provider loop used to do."""

    def _patched(self):
        builder = mock.MagicMock()
        builder.build.side_effect = lambda spec: list(spec.tags)
        pusher = mock.MagicMock()
        return builder, pusher

    def test_builds_then_pushes_each_spec(self):
        builder, pusher = self._patched()
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder", return_value=builder
            ),
            mock.patch(
                "remote_compose.image.backend.ImagePusher", return_value=pusher
            ),
        ):
            backend = create_build_backend("local")
            pushed = backend.build_and_push([_spec("api"), _spec("worker")])

        assert pushed == ["api", "worker"]
        assert builder.build.call_count == 2
        assert pusher.push.call_count == 2

    def test_returned_names_track_input_order(self):
        builder, pusher = self._patched()
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder", return_value=builder
            ),
            mock.patch(
                "remote_compose.image.backend.ImagePusher", return_value=pusher
            ),
        ):
            backend = create_build_backend("local")
            pushed = backend.build_and_push(
                [_spec("z"), _spec("a"), _spec("m")]
            )
        assert pushed == ["z", "a", "m"]

    def test_empty_specs_is_noop(self):
        builder, pusher = self._patched()
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder", return_value=builder
            ),
            mock.patch(
                "remote_compose.image.backend.ImagePusher", return_value=pusher
            ),
        ):
            backend = create_build_backend("local")
            assert backend.build_and_push([]) == []
        builder.build.assert_not_called()
        pusher.push.assert_not_called()

    def test_authenticator_and_docker_bin_threaded_through(self):
        auth = object()
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder"
            ) as builder_cls,
            mock.patch(
                "remote_compose.image.backend.ImagePusher"
            ) as pusher_cls,
        ):
            create_build_backend(
                "local", authenticator=auth, docker_bin="/usr/bin/docker"
            )
        builder_cls.assert_called_once()
        pusher_cls.assert_called_once()
        assert pusher_cls.call_args.kwargs["authenticator"] is auth
        assert pusher_cls.call_args.kwargs["docker_bin"] == "/usr/bin/docker"
        assert builder_cls.call_args.kwargs["docker_bin"] == "/usr/bin/docker"

    def test_push_spec_skips_separate_docker_push(self):
        # rc-8j7.4: a spec with push=True is pushed by buildx --push, so the
        # backend must NOT also call ImagePusher.push for it.
        builder, pusher = self._patched()
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder", return_value=builder
            ),
            mock.patch(
                "remote_compose.image.backend.ImagePusher", return_value=pusher
            ),
        ):
            backend = create_build_backend("local")
            spec = _spec("api")
            spec.push = True
            pushed = backend.build_and_push([spec])
        assert pushed == ["api"]
        builder.build.assert_called_once()
        pusher.push.assert_not_called()


@pytest.fixture
def _extra_backends():
    """Register throwaway backends so precedence tests have >1 valid name."""
    marker = mock.MagicMock(spec=BuildBackend)
    backend_mod.register_backend("dummy", lambda **kw: marker)
    backend_mod.register_backend("other", lambda **kw: marker)
    yield
    backend_mod._BACKENDS.pop("dummy", None)
    backend_mod._BACKENDS.pop("other", None)


class TestResolveBuildConfig:
    """rc-8j7.2: env > provider_config.ecs.build > rc.yml build > default."""

    def test_default_is_local_backend(self):
        cfg = resolve_build_config({}, {}, env={})
        assert isinstance(cfg, BuildConfig)
        assert cfg.backend == DEFAULT_BUILD_BACKEND == "local"
        assert cfg.cache_mode == "max"
        assert cfg.push is False
        assert cfg.max_workers == DEFAULT_BUILD_MAX_WORKERS

    def test_env_var_wins_over_all(self, _extra_backends):
        cfg = resolve_build_config(
            {"ecs": {"build": {"backend": "other"}}},
            {"build": {"backend": "local"}},
            env={"RC_BUILD_BACKEND": "dummy"},
        )
        assert cfg.backend == "dummy"

    def test_provider_config_beats_rc_yml(self, _extra_backends):
        cfg = resolve_build_config(
            {"ecs": {"build": {"backend": "other"}}},
            {"build": {"backend": "dummy"}},
            env={},
        )
        assert cfg.backend == "other"

    def test_rc_yml_used_when_no_provider_config(self, _extra_backends):
        cfg = resolve_build_config(
            {"ecs": {}},
            {"build": {"backend": "dummy"}},
            env={},
        )
        assert cfg.backend == "dummy"

    def test_unknown_backend_name_raises(self):
        with pytest.raises(UnknownBuildBackendError):
            resolve_build_config(
                {"ecs": {"build": {"backend": "wat"}}}, {}, env={}
            )

    def test_cache_mode_and_push_and_workers_resolved(self):
        cfg = resolve_build_config(
            {
                "ecs": {
                    "build": {
                        "cache_mode": "min",
                        "push": True,
                        "max_workers": 8,
                    }
                }
            },
            {},
            env={},
        )
        assert cfg.cache_mode == "min"
        assert cfg.push is True
        assert cfg.max_workers == 8

    def test_invalid_cache_mode_raises(self):
        with pytest.raises(ValueError, match="cache_mode"):
            resolve_build_config(
                {"ecs": {"build": {"cache_mode": "medium"}}}, {}, env={}
            )

    def test_env_max_workers_overrides(self):
        cfg = resolve_build_config(
            {"ecs": {"build": {"max_workers": 2}}},
            {},
            env={"RC_BUILD_MAX_WORKERS": "6"},
        )
        assert cfg.max_workers == 6

    def test_none_dicts_tolerated(self):
        cfg = resolve_build_config(None, None, env={})
        assert cfg.backend == "local"


class TestLocalBuildBackendParallel:
    """rc-8j7.3: independent image groups build concurrently, bounded, with
    a deterministic returned order and fail-fast on any group error."""

    def _backend_with_builds(self, build_fn, *, max_workers):
        builder = mock.MagicMock()
        builder.build.side_effect = build_fn
        pusher = mock.MagicMock()
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder", return_value=builder
            ),
            mock.patch(
                "remote_compose.image.backend.ImagePusher", return_value=pusher
            ),
        ):
            return create_build_backend("local", max_workers=max_workers)

    def test_runs_groups_concurrently(self):
        # Three specs; each build blocks on a barrier that only trips once all
        # three threads arrive. If builds ran serially the barrier would
        # deadlock/timeout — reaching past it proves real concurrency.
        barrier = threading.Barrier(3, timeout=5)

        def _build(spec):
            barrier.wait()
            return list(spec.tags)

        backend = self._backend_with_builds(_build, max_workers=3)
        pushed = backend.build_and_push([_spec("a"), _spec("b"), _spec("c")])
        assert sorted(pushed) == ["a", "b", "c"]

    def test_bounded_by_max_workers(self):
        # max_workers=2 must never run 3 builds at once.
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def _build(spec):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return list(spec.tags)

        backend = self._backend_with_builds(_build, max_workers=2)
        backend.build_and_push([_spec("a"), _spec("b"), _spec("c"), _spec("d")])
        assert active["peak"] <= 2

    def test_returned_order_is_input_order_despite_finish_order(self):
        # Later specs finish first (inverse sleep); returned list must still
        # follow input order.
        order = ["first", "second", "third"]

        def _build(spec):
            # Earlier specs sleep LONGER, so they finish LAST — the returned
            # list must still track input order, not completion order.
            time.sleep(0.02 * (len(order) - order.index(spec.service)))
            return list(spec.tags)

        backend = self._backend_with_builds(_build, max_workers=3)
        pushed = backend.build_and_push([_spec(n) for n in order])
        assert pushed == order

    def test_one_failing_group_fails_the_batch(self):
        def _build(spec):
            if spec.service == "b":
                raise RuntimeError("boom in b")
            return list(spec.tags)

        backend = self._backend_with_builds(_build, max_workers=3)
        with pytest.raises(RuntimeError, match="boom in b"):
            backend.build_and_push([_spec("a"), _spec("b"), _spec("c")])


class TestInstrumentation:
    """rc-8j7.6: per-image + total build timing is emitted to progress."""

    def test_emits_per_image_and_total_timing(self):
        builder = mock.MagicMock()
        builder.build.side_effect = lambda spec: list(spec.tags)
        pusher = mock.MagicMock()
        events: list[str] = []
        with (
            mock.patch(
                "remote_compose.image.backend.ImageBuilder", return_value=builder
            ),
            mock.patch(
                "remote_compose.image.backend.ImagePusher", return_value=pusher
            ),
        ):
            backend = create_build_backend("local", progress=events.append)
            backend.build_and_push([_spec("api"), _spec("worker")])

        per_image = [e for e in events if "built+pushed in" in e]
        totals = [e for e in events if "build+push total" in e]
        assert any("api" in e for e in per_image)
        assert any("worker" in e for e in per_image)
        assert len(totals) == 1
        assert "2 image" in totals[0]
