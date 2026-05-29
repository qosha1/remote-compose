"""ECR authenticator for ImagePusher.

Callable that logs docker into a given ECR registry hostname. Caches
per-registry auth so a push of N images to the same registry only logs in
once. Tokens are valid ~12 hours; we don't bother refreshing in-process.
"""

from __future__ import annotations

import base64
import subprocess
from typing import Any


class ECRAuthError(RuntimeError):
    pass


class ECRAuthenticator:
    def __init__(self, session: Any, docker_bin: str = "docker") -> None:
        self.session = session
        self.docker_bin = docker_bin
        self._authed: set[str] = set()

    def __call__(self, registry: str) -> dict:
        if registry in self._authed:
            return {"registry": registry, "cached": True}

        ecr = self.session.client("ecr")
        resp = ecr.get_authorization_token()
        data = (resp.get("authorizationData") or [None])[0]
        if not data:
            raise ECRAuthError(f"ECR returned no authorization data for {registry}")
        token_b64 = data["authorizationToken"]
        decoded = base64.b64decode(token_b64).decode()
        _user, password = decoded.split(":", 1)

        login = subprocess.run(
            [
                self.docker_bin,
                "login",
                "--username",
                "AWS",
                "--password-stdin",
                f"https://{registry}",
            ],
            input=password,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if login.returncode != 0:
            raise ECRAuthError(
                f"docker login failed for {registry}: " f"{login.stderr.strip()[:200]}"
            )
        self._authed.add(registry)
        return {"registry": registry, "cached": False}
