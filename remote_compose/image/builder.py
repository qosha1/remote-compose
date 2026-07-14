"""Build container images via the Docker CLI.

The builder is provider-agnostic: it knows nothing about ECR, GCR, ACR, or any
other registry. Providers call :class:`ImageBuilder` to turn compose ``build:``
stanzas into tagged local images, then hand those tags to :class:`ImagePusher`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class ImageBuildError(RuntimeError):
    """Raised when ``docker build`` exits non-zero."""


@dataclass
class ImageBuildSpec:
    service: str
    context: Path
    dockerfile: Optional[Path] = None
    target: Optional[str] = None
    build_args: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    platform: Optional[str] = None
    # BuildKit registry cache (rc-e5u.45.2). When either is set we route
    # through `docker buildx build` so the args are honored. cache_from is
    # safe to set unconditionally — buildx silently misses if the ref is
    # absent. cache_to writes the layer cache back into the registry; we
    # always want mode=max so intermediate stages survive across machines.
    cache_from: list[str] = field(default_factory=list)
    cache_to: list[str] = field(default_factory=list)
    # rc-2kp: when True, build with --no-cache and skip cache_from. Set
    # by the deploy path when an `rc fix *` subcommand has touched files
    # since the last build (the registry cache may otherwise return
    # stale layers that don't reflect the user's edits).
    no_cache: bool = False
    # rc-8j7.4: registry cache EXPORT mode for --cache-to. "max" (default)
    # exports every intermediate layer so multi-stage pip/apt stages survive
    # across machines; "min" exports only the final stage (smaller, faster
    # round-trip, but cold pip layers). Only meaningful when cache_to is set.
    cache_mode: str = "max"
    # rc-8j7.4: when True, build via buildx `--push` — build AND push to the
    # tags in one buildkit step — instead of `--load` + a separate
    # `docker push`. Forces the buildx path even without cache args. Requires
    # a working buildx builder + prior registry auth. Default False preserves
    # the `--load` + ImagePusher path.
    push: bool = False


# rc-mtt: env-var knobs.
#   RC_BUILD_NO_PROGRESS_TIMEOUT_S — kill build if no buildkit output for N
#     seconds. Defaults to 300 (5 min). Catches the cache-to-mode=max hang
#     where buildkit goes silent during multi-GB layer cache uploads.
#   RC_DISABLE_BUILDCACHE — when set to truthy, drop cache_from/cache_to
#     before invoking buildx. Escape hatch when registry cache is broken.
_NO_PROGRESS_TIMEOUT_ENV = "RC_BUILD_NO_PROGRESS_TIMEOUT_S"
_DISABLE_BUILDCACHE_ENV = "RC_DISABLE_BUILDCACHE"


def _disable_buildcache_set() -> bool:
    return os.environ.get(_DISABLE_BUILDCACHE_ENV, "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# rc-8j7.5: the cache-to option suffix ECR requires. image-manifest=true +
# oci-mediatypes=true make buildkit export an OCI image manifest instead of
# its default manifest-list cache manifest (which ECR rejects). Kept as a
# single constant so the local builder and the remote (CodeBuild) buildspec
# format the cache export byte-identically.
_CACHE_TO_ECR_SUFFIX = "image-manifest=true,oci-mediatypes=true"


def resolve_dockerfile(spec: "ImageBuildSpec") -> Optional[Path]:
    """The ``-f`` path for a spec, or None when it has no explicit dockerfile.

    Docker resolves a relative ``-f`` against the caller's cwd, not the build
    context, so a relative dockerfile is joined to the context (callers can
    pass the compose-style ``./compose/.../Dockerfile`` relative path verbatim).
    None means buildx uses the default ``<context>/Dockerfile``.
    """
    if not spec.dockerfile:
        return None
    df = spec.dockerfile
    if not df.is_absolute():
        df = spec.context / df
    return df


def buildx_build_flags(
    spec: "ImageBuildSpec",
    *,
    cache_from: list[str],
    cache_to: list[str],
    dockerfile: Optional[str],
) -> list[str]:
    """The buildx-build flags shared by every backend (rc-8j7.5).

    Returns the argv slice AFTER ``docker buildx build [--builder X]
    [--push|--load]`` and BEFORE the trailing context path: ``-f``,
    ``--target``, ``--platform``, ``--no-cache``, ``--build-arg``,
    ``--cache-from``, ``--cache-to``, ``-t``. The local builder and the remote
    CodeBuild buildspec both call this so cache refs + tags are formatted
    identically no matter WHERE the build runs. The caller owns the
    docker/buildx/builder prefix, the ``--push`` vs ``--load`` choice, and the
    trailing context path — those differ by backend (local uses ``--load`` +
    its ``rc-cache`` builder + an absolute context; CodeBuild uses ``--push`` +
    a fresh builder + a context path relative to the extracted tar root).

    ``dockerfile`` is the already-resolved ``-f`` value the caller wants on
    this invocation (an absolute path for the local backend, a path relative to
    the extraction root for CodeBuild); None omits ``-f``.
    """
    args: list[str] = []
    if dockerfile:
        args += ["-f", dockerfile]
    if spec.target:
        args += ["--target", spec.target]
    if spec.platform:
        args += ["--platform", spec.platform]
    if spec.no_cache:
        args += ["--no-cache"]
    for key, value in spec.build_args.items():
        args += ["--build-arg", f"{key}={value}"]
    for ref in cache_from:
        args += ["--cache-from", f"type=registry,ref={ref}"]
    for ref in cache_to:
        # mode=max exports every intermediate layer (not just final-stage) —
        # without it cache hits only land on the last stage and the pip/apt
        # layers rebuild every time. mode is config-driven (default max).
        args += [
            "--cache-to",
            f"type=registry,ref={ref},mode={spec.cache_mode},{_CACHE_TO_ECR_SUFFIX}",
        ]
    for tag in spec.tags:
        args += ["-t", tag]
    return args


class ImageBuilder:
    """Invoke ``docker build`` once per :class:`ImageBuildSpec`.

    When the spec sets ``cache_from`` / ``cache_to`` we issue ``docker buildx
    build`` (BuildKit) so registry-backed layer cache works. Without those
    fields we keep the classic ``docker build`` path so older docker installs
    without the buildx plugin still work.

    rc-mtt: streams stdout+stderr line-by-line via the progress callback +
    runs a no-progress watchdog. If buildkit goes silent for more than
    ``RC_BUILD_NO_PROGRESS_TIMEOUT_S`` seconds (default 300), the build is
    killed and re-tried once without ``--cache-to`` (the most common cause
    of the silent hang is cache-to mode=max stalling on slow uplinks).
    """

    def __init__(
        self,
        docker_bin: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"
        self.progress = progress

    def build(self, spec: ImageBuildSpec) -> list[str]:
        """Build and tag the image; return the list of tags applied."""
        if not spec.tags:
            raise ImageBuildError(f"service {spec.service!r}: no tags supplied")

        cache_to = list(spec.cache_to)
        cache_from = list(spec.cache_from)
        if _disable_buildcache_set():
            self._emit(
                "  RC_DISABLE_BUILDCACHE set — skipping --cache-from/--cache-to."
            )
            cache_to = []
            cache_from = []
        if spec.no_cache:
            # rc-2kp: an `rc fix *` subcommand touched files since the last
            # build. Drop cache_from so we don't pull stale layers, and
            # set the --no-cache flag so docker rebuilds every step.
            self._emit(
                f"  rc-2kp: no_cache=True for {spec.service!r} — building "
                f"without --cache-from (an `rc fix *` change is in flight)."
            )
            cache_from = []

        try:
            return self._run_build(spec, cache_from, cache_to)
        except _NoProgressHang as exc:
            if not cache_to:
                # Already running without cache-to and STILL hung. Surface as
                # a regular build error rather than retry forever.
                raise ImageBuildError(
                    f"docker build for {spec.service!r} hung with no "
                    f"buildkit progress for {exc.timeout_s}s — see logs above. "
                    f"This is unusual without cache-to; check Docker daemon health."
                ) from None
            self._emit(
                f"  WARN: buildkit went silent for {exc.timeout_s}s during "
                f"cache-to push (likely a slow uplink uploading mode=max "
                f"layer cache). Killed; falling back to a no-cache rebuild "
                f"WITHOUT --cache-to. Set RC_DISABLE_BUILDCACHE=1 to skip "
                f"the cache-to attempt entirely on future deploys."
            )
            return self._run_build(spec, cache_from, [])

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    _BUILDX_BUILDER = "rc-cache"

    def _ensure_buildx_builder(self) -> Optional[str]:
        """Ensure a docker-container buildx builder exists for registry cache.

        Returns the builder name, or None if buildx / the container driver
        isn't available (caller then degrades to a plain cache-less build).
        Idempotent: reuses the builder across deploys once created.
        """
        name = self._BUILDX_BUILDER
        try:
            inspect = subprocess.run(
                [self.docker_bin, "buildx", "inspect", name],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            self._emit("  WARN: docker not found — building without cache.")
            return None
        if inspect.returncode == 0:
            return name
        create = subprocess.run(
            [
                self.docker_bin,
                "buildx",
                "create",
                "--name",
                name,
                "--driver",
                "docker-container",
                "--bootstrap",
            ],
            capture_output=True,
            text=True,
        )
        if create.returncode != 0:
            self._emit(
                "  WARN: could not create a docker-container buildx builder "
                f"({create.stderr.strip()[:200]}) — building WITHOUT registry "
                "cache. Install/enable buildx to get layer caching."
            )
            return None
        self._emit(f"  created buildx builder {name!r} (docker-container driver)")
        return name

    def _run_build(
        self,
        spec: ImageBuildSpec,
        cache_from: list[str],
        cache_to: list[str],
    ) -> list[str]:
        # rc-8j7.4: --push also requires buildx (classic `docker build` can't
        # build-and-push in one step), so force the buildx path when push is
        # requested even if no cache args are set.
        use_buildx = bool(cache_from or cache_to or spec.push)
        builder_name = None
        if use_buildx:
            # Registry cache export (--cache-to type=registry) needs the
            # docker-container driver — the default `docker` driver can't
            # export cache. rc owns a dedicated builder so callers (and CI)
            # don't have to set one up. If buildx/the driver isn't available,
            # degrade to a plain cache-less build rather than failing.
            builder_name = self._ensure_buildx_builder()
            if builder_name is None:
                use_buildx = False
                cache_from, cache_to = [], []
        if use_buildx:
            cmd = [self.docker_bin, "buildx", "build", "--builder", builder_name]
            if spec.push:
                # rc-8j7.4: build AND push to the registry in one buildkit
                # step — no separate `docker push`. Mutually exclusive with
                # --load (buildx rejects both).
                cmd.append("--push")
            else:
                # --load brings the built image into the local docker image
                # store so the subsequent `docker push` (against each tag) can
                # find it. Without --load buildx would only export to the
                # registry cache and the push would 404.
                cmd.append("--load")
        else:
            cmd = [self.docker_bin, "build"]
        # rc-8j7.5: the -f/--target/--platform/--no-cache/--build-arg/
        # --cache-from/--cache-to/-t flags are built by the shared
        # buildx_build_flags() so the local build and the remote CodeBuild
        # buildspec stay byte-identical (same cache refs, same tags). --no-cache
        # (rc-2kp) forces every layer to rebuild from on-disk source rather than
        # the registry's frozen layers; the empty cache_from above pairs with it.
        df = resolve_dockerfile(spec)
        cmd += buildx_build_flags(
            spec,
            cache_from=cache_from,
            cache_to=cache_to,
            dockerfile=str(df) if df else None,
        )
        cmd.append(str(spec.context))

        self._emit(f"$ {' '.join(cmd)}")

        # rc-mtt: use the no-progress watchdog ONLY when cache_to is set,
        # because that's the path that hangs on slow uplinks. Plain
        # `docker build` uses subprocess.run for backwards compat with
        # callers/tests that mock subprocess.run.
        if cache_to:
            timeout_s = int(os.environ.get(_NO_PROGRESS_TIMEOUT_ENV, "300"))
            rc, captured_stderr = self._popen_with_watchdog(cmd, timeout_s)
            if rc != 0:
                raise ImageBuildError(
                    f"docker build failed for {spec.service!r}: "
                    f"{captured_stderr.strip()[:500]}"
                )
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ImageBuildError(
                    f"docker build failed for {spec.service!r}: "
                    f"{result.stderr.strip()[:500]}"
                )
        return list(spec.tags)

    def _popen_with_watchdog(
        self,
        cmd: list[str],
        timeout_s: int,
    ) -> tuple[int, str]:
        """Run cmd with line streaming + a no-progress watchdog.

        rc-mtt: docker buildx with cache-to mode=max can hang during the
        cache-push phase with zero output for hours. Detect that by tracking
        the wall time since the last stdout/stderr line and killing the
        process if it exceeds ``timeout_s``. Caller catches _NoProgressHang
        and retries without cache-to.
        """
        last_output = [time.monotonic()]
        captured_stderr: list[str] = []
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def _drain(stream, is_stderr: bool):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    last_output[0] = time.monotonic()
                    self._emit(line.rstrip())
                    if is_stderr:
                        captured_stderr.append(line)
            finally:
                stream.close()

        t_out = threading.Thread(target=_drain, args=(proc.stdout, False), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, True), daemon=True)
        t_out.start()
        t_err.start()

        hang_detected = False
        while proc.poll() is None:
            elapsed = time.monotonic() - last_output[0]
            if elapsed > timeout_s:
                hang_detected = True
                proc.kill()
                break
            time.sleep(1)
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        rc = proc.wait()
        if hang_detected:
            raise _NoProgressHang(timeout_s)
        return rc, "".join(captured_stderr)

    def _emit(self, msg: str) -> None:
        if self.progress:
            self.progress(msg)


class _NoProgressHang(Exception):
    """Raised internally when the watchdog kills a stalled build."""

    def __init__(self, timeout_s: int) -> None:
        super().__init__(f"no buildkit progress for {timeout_s}s")
        self.timeout_s = timeout_s
