"""Existing Cloud Map namespace adopt support (rc-adopt D5).

rc normally creates a `<project>.local` private DNS namespace. When
``provider_config.ecs.existing_cloud_map_namespace_id`` is set, rc registers
its services into that live namespace instead of creating one — required so
peers that already resolve the existing names keep working (e.g. debuggai-api
calls django.production.browser-mgr.local:5000, so browser-mgr must register
into the existing `production.browser-mgr.local` namespace, not a new one).

GENERAL + opt-in + strictly ADDITIVE: with no override the emitted terraform is
byte-identical (guarded by test_golden.py).
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

_NS_ID = "ns-4agciigwqnaazfd4"


def _ctx(tmp_path: Path, ecs_overrides: dict | None = None) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "browser-mgr-prod",
        "vpc_cidr": "10.0.0.0/16",
    }
    ecs_cfg.update(ecs_overrides or {})
    return DeployContext(
        project="browser-mgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application"
            ),
            "postgres": ServiceSpec(
                name="postgres", cpu=512, memory=1024, type="infrastructure"
            ),
        },
        secrets=[],
    )


def _sd(tmp_path, ecs_overrides=None):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, ecs_overrides), out)
    return (out / "service_discovery.tf").read_text()


class TestExistingCloudMapNamespace:
    def test_no_namespace_created_services_use_existing_id(self, tmp_path):
        sd = _sd(tmp_path, {"existing_cloud_map_namespace_id": _NS_ID})
        # rc creates no namespace resource.
        assert 'resource "aws_service_discovery_private_dns_namespace"' not in sd
        # each service registers into the existing namespace id.
        assert f'namespace_id = "{_NS_ID}"' in sd
        # services are still registered.
        assert 'resource "aws_service_discovery_service" "django"' in sd
        assert 'resource "aws_service_discovery_service" "postgres"' in sd

    def test_create_mode_still_makes_namespace(self, tmp_path):
        """No override → rc creates the namespace + references the resource."""
        sd = _sd(tmp_path)
        assert 'resource "aws_service_discovery_private_dns_namespace" "main"' in sd
        assert (
            "namespace_id = aws_service_discovery_private_dns_namespace.main.id" in sd
        )

    def test_existing_namespace_skips_dhcp_when_adopting_vpc(self, tmp_path):
        """Adopt-in-place pairs existing namespace with an existing VPC — no
        rc-managed namespace and no DHCP options association."""
        sd = _sd(
            tmp_path,
            {
                "existing_cloud_map_namespace_id": _NS_ID,
                "vpc_id": "vpc-0b6967",
                "public_subnet_ids": ["subnet-a", "subnet-b"],
            },
        )
        assert 'resource "aws_service_discovery_private_dns_namespace"' not in sd
        assert 'resource "aws_vpc_dhcp_options"' not in sd
        assert f'namespace_id = "{_NS_ID}"' in sd
