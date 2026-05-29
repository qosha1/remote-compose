"""Service-to-service discovery tests for the ECS provider."""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict) -> DeployContext:
    return DeployContext(
        project="disco",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "c",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _svc(name: str, **kw) -> ServiceSpec:
    return ServiceSpec(name=name, cpu=256, memory=512, type="application", **kw)


class TestSingleService:
    def test_no_namespace_when_only_one_service(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, {"web": _svc("web")}), out)
        sd = (out / "service_discovery.tf").read_text()
        assert sd.strip() == "", "single-service compose doesn't need service discovery"

    def test_service_has_no_service_registries_block(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, {"web": _svc("web")}), out)
        services = (out / "services.tf").read_text()
        assert "service_registries" not in services


class TestMultiService:
    def test_namespace_created(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, {"api": _svc("api"), "db": _svc("db")}),
            out,
        )
        sd = (out / "service_discovery.tf").read_text()
        assert "aws_service_discovery_private_dns_namespace" in sd
        assert '"${var.project}.local"' in sd

    def test_discovery_service_per_compose_service(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                {
                    "api": _svc("api"),
                    "db": _svc("db"),
                    "cache": _svc("cache"),
                },
            ),
            out,
        )
        sd = (out / "service_discovery.tf").read_text()
        for name in ("api", "db", "cache"):
            assert f'aws_service_discovery_service" "{name}"' in sd
            assert f'name = "{name}"' in sd

    def test_ecs_services_register_with_cloud_map(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, {"api": _svc("api"), "db": _svc("db")}),
            out,
        )
        services = (out / "services.tf").read_text()
        assert "service_registries" in services
        assert "aws_service_discovery_service.api.arn" in services
        assert "aws_service_discovery_service.db.arn" in services

    def test_service_name_with_dash_sanitized_for_tf_reference(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                {
                    "api": _svc("api"),
                    "celery-worker": _svc("celery-worker"),
                },
            ),
            out,
        )
        services = (out / "services.tf").read_text()
        sd = (out / "service_discovery.tf").read_text()
        # TF identifier uses underscores; DNS name uses the original compose name
        assert 'aws_service_discovery_service" "celery_worker"' in sd
        assert 'name = "celery-worker"' in sd
        assert "aws_service_discovery_service.celery_worker.arn" in services
