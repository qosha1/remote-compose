"""Translate Copilot service.image → compose service entry.

Two shapes:
  image.build: { context, dockerfile, args, target }   → compose build
  image.location: <ecr-url>                             → compose image:

Both can carry image.port, image.depends_on, image.healthcheck — those
are handled by other translators (service-type, healthcheck) — this one
just shapes the build/image.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.copilot.discover import CopilotService
from remote_compose.copilot.translate import translate_image


def _svc(raw: dict) -> CopilotService:
    return CopilotService(
        name=raw.get("name", "x"),
        type=raw.get("type", "Backend Service"),
        manifest_path=Path("/dev/null"),
        raw=raw,
    )


class TestImageBuild:
    def test_build_dict_carries_context_dockerfile(self):
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {
                        "build": {
                            "context": "./",
                            "dockerfile": "compose/production/django/Dockerfile",
                        }
                    },
                }
            )
        )
        assert out["build"]["context"] == "./"
        assert out["build"]["dockerfile"] == "compose/production/django/Dockerfile"

    def test_build_args_passed_through(self):
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {
                        "build": {
                            "context": ".",
                            "args": {"VERSION": "1.2", "ENV": "prod"},
                        }
                    },
                }
            )
        )
        assert out["build"]["args"] == {"VERSION": "1.2", "ENV": "prod"}

    def test_build_target_passed_through(self):
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {"build": {"context": ".", "target": "production"}},
                }
            )
        )
        assert out["build"]["target"] == "production"

    def test_build_string_form(self):
        # Copilot allows `build: ./path/to/Dockerfile.dir` shorthand.
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {"build": "./api"},
                }
            )
        )
        # Compose accepts the string form directly.
        assert out["build"] == "./api"


class TestImageLocation:
    def test_image_location_becomes_image(self):
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {
                        "location": "123456789012.dkr.ecr.us-east-2.amazonaws.com/myapp:v1",
                    },
                }
            )
        )
        assert out["image"] == "123456789012.dkr.ecr.us-east-2.amazonaws.com/myapp:v1"
        assert "build" not in out

    def test_location_with_template_var_left_intact(self):
        # Copilot supports ${TAG} interpolation that compose doesn't.
        # Carry it as-is + downstream translators can warn.
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {
                        "location": "123456789012.dkr.ecr.us-east-2.amazonaws.com/myapp:${TAG}",
                    },
                }
            )
        )
        assert "${TAG}" in out["image"]


class TestImageNeitherBuildNorLocation:
    def test_no_image_block_returns_empty(self):
        out, warnings = translate_image(_svc({"name": "api"}))
        assert out == {}
        assert warnings == []

    def test_image_block_without_build_or_location_returns_empty(self):
        # E.g. just image.port set; nothing for compose to build/pull.
        out, warnings = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {"port": 8080},
                }
            )
        )
        assert out == {}


class TestPortNotEmittedHere:
    """image.port is for ALB routing — handled by translate_service_type,
    not the image translator. Verify we don't accidentally emit it."""

    def test_port_not_in_compose_output(self):
        out, _ = translate_image(
            _svc(
                {
                    "name": "api",
                    "image": {"build": ".", "port": 8001},
                }
            )
        )
        # port is service-type concern; compose ports[] is host-mapping
        # which isn't relevant on Fargate (each task has its own ENI).
        assert "ports" not in out
        assert "port" not in out
