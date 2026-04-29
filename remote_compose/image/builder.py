"""Build container images via the Docker CLI.

The builder is provider-agnostic: it knows nothing about ECR, GCR, ACR, or any
other registry. Providers call :class:`ImageBuilder` to turn compose ``build:``
stanzas into tagged local images, then hand those tags to :class:`ImagePusher`.
"""

from __future__ import annotations

import shutil
import subprocess
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


class ImageBuilder:
    """Invoke ``docker build`` once per :class:`ImageBuildSpec`.

    When the spec sets ``cache_from`` / ``cache_to`` we issue ``docker buildx
    build`` (BuildKit) so registry-backed layer cache works. Without those
    fields we keep the classic ``docker build`` path so older docker installs
    without the buildx plugin still work.
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

        use_buildx = bool(spec.cache_from or spec.cache_to)
        if use_buildx:
            cmd = [self.docker_bin, "buildx", "build"]
            # --load brings the built image into the local docker image
            # store so the subsequent `docker push` (against each tag) can
            # find it. Without --load buildx would only export to the
            # registry cache and the push would 404.
            cmd.append("--load")
        else:
            cmd = [self.docker_bin, "build"]
        if spec.dockerfile:
            # Docker resolves a relative -f against the caller's cwd, not the
            # build context. Join to the context so callers can pass the
            # compose-style "./compose/.../Dockerfile" relative path verbatim.
            df = spec.dockerfile
            if not df.is_absolute():
                df = spec.context / df
            cmd += ["-f", str(df)]
        if spec.target:
            cmd += ["--target", spec.target]
        if spec.platform:
            cmd += ["--platform", spec.platform]
        for key, value in spec.build_args.items():
            cmd += ["--build-arg", f"{key}={value}"]
        for ref in spec.cache_from:
            cmd += ["--cache-from", f"type=registry,ref={ref}"]
        for ref in spec.cache_to:
            # mode=max exports every intermediate layer (not just final-stage)
            # — without it cache hits only land on the last stage and the
            # `pip install` layer rebuilds every time.
            cmd += ["--cache-to", f"type=registry,ref={ref},mode=max"]
        for tag in spec.tags:
            cmd += ["-t", tag]
        cmd.append(str(spec.context))

        self._emit(f"$ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ImageBuildError(
                f"docker build failed for {spec.service!r}: "
                f"{result.stderr.strip()[:500]}"
            )
        return list(spec.tags)

    def _emit(self, msg: str) -> None:
        if self.progress:
            self.progress(msg)
