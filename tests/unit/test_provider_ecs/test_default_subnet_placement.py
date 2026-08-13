"""provider_config.ecs.default_subnet_placement (rc-0cv).

A service with no explicit ``subnet_group`` always landed on
``public_subnet_ids`` with ``assignPublicIp=ENABLED`` — even in an
existing-VPC deployment where the caller already threaded real, adopted
``private_subnet_ids`` through ``provider_config.ecs``. The only opt-out was
the heavier declarative ``network:`` block, and in existing-VPC mode that
feature always carves a BRAND NEW subnet — it can't adopt one a parent stack
already provisioned, so it can't be used to place a service on subnets that
already exist.

``default_subnet_placement: private`` makes "no explicit subnet_group" mean
"the already-resolved private subnets" instead, for every service in the
stack — no per-service opt-in, no risk of a duplicate subnet.

GENERAL + opt-in + strictly ADDITIVE: the default is "public", byte-identical
to before this existed (guarded by test_golden.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider


def _ctx(
    tmp_path: Path,
    ecs_overrides: dict | None = None,
    rc_yml_v2: dict | None = None,
    **over,
) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "foundry-tenant",
        "vpc_id": "vpc-0b6967",
        "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
        "private_subnet_ids": ["subnet-priv-a", "subnet-priv-b"],
    }
    ecs_cfg.update(ecs_overrides or {})
    services = over.pop("services", None) or {
        "django": ServiceSpec(name="django", cpu=512, memory=1024, type="application"),
    }
    return DeployContext(
        project="foundry-tenant",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc_yml_v2 or {},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _emit(tmp_path, ecs_overrides: dict | None = None, **over):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, ecs_overrides, **over), out)
    return out


class TestDefaultSubnetPlacement:
    def test_default_is_public_byte_identical(self, tmp_path):
        services = (_emit(tmp_path) / "services.tf").read_text()
        assert "subnets          = local.rc_public_subnet_ids" in services
        assert "assign_public_ip = true" in services

    def test_private_places_service_with_no_subnet_group_on_private(self, tmp_path):
        services = (
            _emit(tmp_path, {"default_subnet_placement": "private"}) / "services.tf"
        ).read_text()
        assert "subnets          = local.rc_private_subnet_ids" in services
        assert "assign_public_ip = false" in services

    def test_private_default_does_not_override_explicit_subnet_group(self, tmp_path):
        # An explicit subnet_group still wins — this knob only changes the
        # behavior for services that name NO subnet_group at all.
        network_cfg = {
            "network": {
                "subnets": {
                    "public-explicit": {
                        "cidrs": ["10.0.10.0/24"],
                        "count": 1,
                        "public": True,
                    }
                }
            }
        }
        services_map = {
            "django": ServiceSpec(
                name="django",
                cpu=512,
                memory=1024,
                type="application",
                subnet_group="public-explicit",
            ),
        }
        out = _emit(
            tmp_path,
            {
                "default_subnet_placement": "private",
                "internet_gateway_id": "igw-0abc123",
            },
            rc_yml_v2=network_cfg,
            services=services_map,
        )
        services = (out / "services.tf").read_text()
        assert "local.rc_private_subnet_ids" not in services
        assert "local.rc_public_subnet_ids" not in services
        assert "aws_subnet.rc_" in services  # its own declared subnet group

    def test_invalid_value_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="default_subnet_placement"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, {"default_subnet_placement": "both"}), tmp_path / "tf"
            )
