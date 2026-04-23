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


class ImageBuilder:
    """Invoke ``docker build`` once per :class:`ImageBuildSpec`."""

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
