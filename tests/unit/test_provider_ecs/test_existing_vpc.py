"""Existing-VPC support (rc-a57).

rc normally creates a VPC. When ``provider_config.ecs.vpc_id`` is set, it
deploys INTO an existing VPC instead — required for stacks that must share a
VPC + security group with peer systems (same-VPC SG-referencing + Cloud Map
DNS that cross-VPC peering cannot replicate).

This is a GENERAL, opt-in capability (no stack-specific logic) and is strictly
ADDITIVE: with no ``vpc_id`` the emitted terraform is byte-identical to today
(guarded by tests/unit/test_provider_ecs/test_golden.py). These tests pin both
the config-shape validation and the adopt-mode emission. AWS pre-flight
(describe-vpcs/subnets) is covered separately (rc-h0b).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import preflight_existing_vpc
from remote_compose.provider.base import ProviderConfigError


class _FakeEc2:
    """Minimal ec2 client double for pre-flight (rc-h0b)."""

    def __init__(self, vpc_ids, subnets):
        self._vpc_ids = set(vpc_ids)
        self._subnets = subnets  # list of {SubnetId, VpcId, AvailabilityZone}

    def describe_vpcs(self, VpcIds):
        return {"Vpcs": [{"VpcId": v} for v in VpcIds if v in self._vpc_ids]}

    def describe_subnets(self, Filters):
        wanted = Filters[0]["Values"]
        return {"Subnets": [s for s in self._subnets if s["VpcId"] in wanted]}


def _subnet(sid, vpc="vpc-0b6967", az="us-east-2a"):
    return {"SubnetId": sid, "VpcId": vpc, "AvailabilityZone": az}


# ── pre-flight AWS validation (rc-h0b) ──────────────────────────────────────
class TestPreflight:
    def _cfg(self, **over):
        cfg = {
            "vpc_id": "vpc-0b6967",
            "public_subnet_ids": ["subnet-a", "subnet-b"],
            "private_subnet_ids": ["subnet-c", "subnet-d"],
        }
        cfg.update(over)
        return cfg

    def test_noop_when_no_vpc_id(self):
        preflight_existing_vpc({"region": "us-east-2"}, _FakeEc2([], []))  # no raise

    def test_happy_path(self):
        ec2 = _FakeEc2(
            ["vpc-0b6967"],
            [
                _subnet("subnet-a", az="us-east-2a"),
                _subnet("subnet-b", az="us-east-2b"),
                _subnet("subnet-c", az="us-east-2a"),
                _subnet("subnet-d", az="us-east-2b"),
            ],
        )
        preflight_existing_vpc(self._cfg(), ec2)  # no raise

    def test_vpc_not_found(self):
        ec2 = _FakeEc2([], [])
        with pytest.raises(ProviderConfigError, match="not found"):
            preflight_existing_vpc(self._cfg(), ec2)

    def test_subnet_missing_or_wrong_vpc(self):
        # subnet-b is in a different VPC -> not returned by the vpc-id filter
        ec2 = _FakeEc2(
            ["vpc-0b6967"],
            [_subnet("subnet-a"), _subnet("subnet-b", vpc="vpc-other")],
        )
        with pytest.raises(ProviderConfigError, match="subnet"):
            preflight_existing_vpc(
                self._cfg(
                    public_subnet_ids=["subnet-a", "subnet-b"], private_subnet_ids=[]
                ),
                ec2,
            )

    def test_public_subnets_must_span_two_azs(self):
        ec2 = _FakeEc2(
            ["vpc-0b6967"],
            [
                _subnet("subnet-a", az="us-east-2a"),
                _subnet("subnet-b", az="us-east-2a"),
            ],
        )
        with pytest.raises(ProviderConfigError, match="availability zones"):
            preflight_existing_vpc(
                self._cfg(
                    public_subnet_ids=["subnet-a", "subnet-b"], private_subnet_ids=[]
                ),
                ec2,
            )


def _ctx(tmp_path: Path, ecs_overrides: dict | None = None) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "mesh-prod",
        "aws_profile": "default",
        "vpc_cidr": "10.0.0.0/16",
    }
    ecs_cfg.update(ecs_overrides or {})
    return DeployContext(
        project="mesh",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                replicas=1,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
            ),
            "api": ServiceSpec(
                name="api", cpu=512, memory=1024, replicas=1, type="application"
            ),
        },
        secrets=[],
    )


ADOPT = {
    "vpc_id": "vpc-0b6967",
    "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
    "private_subnet_ids": ["subnet-priv-a", "subnet-priv-b"],
    "security_group_ids": ["sg-mesh"],
}


# ── config-shape validation (rc-40e) ───────────────────────────────────────
class TestConfigShapeValidation:
    def test_vpc_id_without_subnets_errors(self, tmp_path):
        ctx = _ctx(tmp_path, {"vpc_id": "vpc-0b6967"})
        with pytest.raises(ProviderConfigError, match="public_subnet_ids"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_subnets_without_vpc_id_errors(self, tmp_path):
        ctx = _ctx(tmp_path, {"public_subnet_ids": ["subnet-a", "subnet-b"]})
        with pytest.raises(ProviderConfigError, match="vpc_id"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_private_subnets_optional(self, tmp_path):
        cfg = dict(ADOPT)
        cfg.pop("private_subnet_ids")
        ECSProvider().emit_terraform(_ctx(tmp_path, cfg), tmp_path / "tf")  # no raise


# ── adopt-mode emission (rc-23j) ────────────────────────────────────────────
class TestAdoptModeEmission:
    def _net(self, tmp_path) -> str:
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, ADOPT), out)
        return (out / "network.tf").read_text()

    def test_no_vpc_or_subnet_resources_created(self, tmp_path):
        net = self._net(tmp_path)
        assert 'resource "aws_vpc"' not in net
        assert 'resource "aws_subnet"' not in net
        assert 'resource "aws_internet_gateway"' not in net
        assert 'resource "aws_route_table"' not in net

    def test_vpc_is_data_sourced(self, tmp_path):
        net = self._net(tmp_path)
        assert 'data "aws_vpc" "main"' in net
        assert "vpc-0b6967" in net

    def test_provided_subnets_exposed_as_locals(self, tmp_path):
        net = self._net(tmp_path)
        assert "subnet-pub-a" in net and "subnet-pub-b" in net
        assert "subnet-priv-a" in net and "subnet-priv-b" in net

    def test_alb_uses_existing_subnets(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, ADOPT), out)
        alb = (out / "alb.tf").read_text()
        # ALB references the network locals, not created subnets.
        assert "aws_subnet.public" not in alb
        assert "local." in alb

    def test_tasks_carry_extra_security_group(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, ADOPT), out)
        services = (out / "services.tf").read_text()
        assert "sg-mesh" in services
        assert "aws_security_group.tasks.id" in services  # rc's own SG still attached

    def test_cloudmap_namespace_in_existing_vpc(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, ADOPT), out)
        sd = (out / "service_discovery.tf").read_text()
        assert 'resource "aws_vpc"' not in sd  # no created VPC
        # namespace registers in the adopted VPC (data source, not a created one)
        assert "vpc  = data.aws_vpc.main.id" in sd

    def test_no_dhcp_options_override_on_adopted_vpc(self, tmp_path):
        # A VPC has exactly one DHCP options set; associating a new one would
        # REPLACE the existing VPC's options and break the shared mesh's DNS.
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, ADOPT), out)
        sd = (out / "service_discovery.tf").read_text()
        assert "aws_vpc_dhcp_options" not in sd


# ── default mode stays create-VPC (additive guarantee) ──────────────────────
class TestDefaultModeUnchanged:
    def test_default_still_creates_vpc(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        net = (out / "network.tf").read_text()
        assert 'resource "aws_vpc" "main"' in net
        assert 'resource "aws_subnet" "public"' in net
        assert 'data "aws_vpc"' not in net

    def test_default_tasks_have_only_rc_sg(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        services = (out / "services.tf").read_text()
        assert "security_groups  = [aws_security_group.tasks.id]" in services
