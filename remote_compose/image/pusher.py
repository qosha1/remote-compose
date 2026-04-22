"""Push container images via the Docker CLI.

Registry auth is the caller's concern: providers pass an ``authenticator``
callable that logs in to the target registry before push. The pusher
itself does not know about ECR, GCR, ACR, or private-registry credentials.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional


class ImagePushError(RuntimeError):
    """Raised when ``docker push`` exits non-zero."""


@dataclass
class _AuthSession:
    """Transient record of a registry login, returned by the authenticator."""
    registry: str


class ImagePusher:
    """Invoke ``docker push`` against pre-built local tags."""

    def __init__(
        self,
        authenticator: Optional[Callable[[str], _AuthSession]] = None,
        docker_bin: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.authenticator = authenticator
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"
        self.progress = progress

    def push(self, tags: list[str]) -> list[str]:
        """Push every tag; return the list of successfully pushed tags."""
        if self.authenticator is not None:
            registries = {self._registry_for(t) for t in tags}
            for reg in registries:
                self.authenticator(reg)

        pushed: list[str] = []
        for tag in tags:
            cmd = [self.docker_bin, "push", tag]
            self._emit(f"$ {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ImagePushError(
                    f"docker push failed for {tag!r}: "
                    f"{result.stderr.strip()[:500]}"
                )
            pushed.append(tag)
        return pushed

    @staticmethod
    def _registry_for(tag: str) -> str:
        """Extract the registry hostname from an image tag.

        Tags without a ``/`` are pure image names (Docker Hub). When a ``/``
        is present, the first path component is treated as a registry only
        if it looks like a hostname (contains ``.``) or a host:port pair.
        Otherwise it is treated as a Docker Hub org/user (e.g. ``library``).
        """
        if "/" not in tag:
            return "docker.io"
        head = tag.split("/", 1)[0]
        return head if ("." in head or ":" in head) else "docker.io"

    def _emit(self, msg: str) -> None:
        if self.progress:
            self.progress(msg)
