"""Unit tests for remote_compose.image.pusher."""

from __future__ import annotations

from unittest import mock

import pytest

from remote_compose.image.pusher import ImagePushError, ImagePusher


class TestImagePusher:
    def test_push_invokes_docker_per_tag(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            pushed = ImagePusher(docker_bin="docker").push(
                [
                    "1.dkr.ecr.us-west-2.amazonaws.com/myapp/web:abc",
                    "1.dkr.ecr.us-west-2.amazonaws.com/myapp/web:latest",
                ]
            )
        assert len(pushed) == 2
        assert run.call_count == 2

    def test_authenticator_called_once_per_registry(self):
        auth_calls: list[str] = []

        def auth(registry: str):
            auth_calls.append(registry)
            return object()

        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ImagePusher(authenticator=auth, docker_bin="docker").push(
                [
                    "registry-a.example.com/a:1",
                    "registry-a.example.com/a:2",
                    "registry-b.example.com/b:1",
                ]
            )
        assert sorted(auth_calls) == [
            "registry-a.example.com",
            "registry-b.example.com",
        ]

    def test_push_error_raises(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="permission denied"
            )
            with pytest.raises(ImagePushError, match="permission denied"):
                ImagePusher(docker_bin="docker").push(["registry.example.com/x:1"])

    def test_registry_for_infers_docker_hub(self):
        assert ImagePusher._registry_for("myapp:latest") == "docker.io"
        assert ImagePusher._registry_for("library/nginx:alpine") == "docker.io"

    def test_registry_for_uses_hostname(self):
        assert ImagePusher._registry_for("myreg.example.com/x:1") == "myreg.example.com"
        assert ImagePusher._registry_for("ghcr.io/org/app:v1") == "ghcr.io"

    def test_registry_for_uses_host_with_port(self):
        assert ImagePusher._registry_for("localhost:5000/x:1") == "localhost:5000"
