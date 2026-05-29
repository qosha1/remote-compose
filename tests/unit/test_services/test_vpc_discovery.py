"""Tests for VPCService._discover_vpc_resources (remote-compose-tff).

Earlier behavior populated only ``vpc_id`` + ``vpc_cidr`` when
provision_vpc found an existing managed VPC by tag. If the DB record
was deleted but the AWS-side VPC still existed, every downstream step
(ALB / EFS / ECS) saw empty subnet lists and broke.

Fix: discover the full topology (subnets, IGW, NAT, route tables) via
boto3 when finding an existing VPC, populate the model with what
boto3 returns, backfill any pre-fix records that are missing fields.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from remote_compose.services.vpc_service import VPCService


@pytest.fixture
def svc():
    s = VPCService.__new__(VPCService)
    s._observers = []
    s.log_info = MagicMock()
    s.log_warning = MagicMock()
    s.log_error = MagicMock()
    s.notify_observers = MagicMock()
    return s


def _make_ec2(*, subnets=None, igws=None, natgws=None, route_tables=None, vpcs=None):
    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = {
        "Vpcs": vpcs
        or [
            {"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16"},
        ]
    }
    ec2.describe_subnets.return_value = {"Subnets": subnets or []}
    ec2.describe_internet_gateways.return_value = {
        "InternetGateways": igws or [],
    }
    ec2.describe_nat_gateways.return_value = {
        "NatGateways": natgws or [],
    }
    ec2.describe_route_tables.return_value = {
        "RouteTables": route_tables or [],
    }
    return ec2


class TestDiscoverHappyPath:
    def test_full_topology_round_trips(self, svc):
        ec2 = _make_ec2(
            subnets=[
                {
                    "SubnetId": "subnet-pub1",
                    "Tags": [{"Key": "Name", "Value": "c1-public-subnet-1"}],
                },
                {
                    "SubnetId": "subnet-pub2",
                    "Tags": [{"Key": "Name", "Value": "c1-public-subnet-2"}],
                },
                {
                    "SubnetId": "subnet-priv1",
                    "Tags": [{"Key": "Name", "Value": "c1-private-subnet-1"}],
                },
                {
                    "SubnetId": "subnet-priv2",
                    "Tags": [{"Key": "Name", "Value": "c1-private-subnet-2"}],
                },
            ],
            igws=[{"InternetGatewayId": "igw-1"}],
            natgws=[{"NatGatewayId": "nat-1"}],
            route_tables=[
                {
                    "RouteTableId": "rtb-pub",
                    "Tags": [{"Key": "Name", "Value": "c1-public-rt"}],
                },
                {
                    "RouteTableId": "rtb-priv",
                    "Tags": [{"Key": "Name", "Value": "c1-private-rt"}],
                },
            ],
        )
        out = svc._discover_vpc_resources(ec2, "vpc-1", "c1")

        assert out["vpc_cidr"] == "10.0.0.0/16"
        assert out["public_subnet_ids"] == ["subnet-pub1", "subnet-pub2"]
        assert out["private_subnet_ids"] == ["subnet-priv1", "subnet-priv2"]
        assert out["internet_gateway_id"] == "igw-1"
        assert out["nat_gateway_id"] == "nat-1"
        assert out["public_route_table_id"] == "rtb-pub"
        assert out["private_route_table_id"] == "rtb-priv"

    def test_subnet_classification_falls_back_to_map_public_ip(self, svc):
        # Untagged subnet — classification falls back to MapPublicIpOnLaunch.
        ec2 = _make_ec2(
            subnets=[
                {
                    "SubnetId": "subnet-pub-untagged",
                    "MapPublicIpOnLaunch": True,
                    "Tags": [],
                },
                {
                    "SubnetId": "subnet-priv-untagged",
                    "MapPublicIpOnLaunch": False,
                    "Tags": [],
                },
            ]
        )
        out = svc._discover_vpc_resources(ec2, "vpc-1", "c1")
        assert out["public_subnet_ids"] == ["subnet-pub-untagged"]
        assert out["private_subnet_ids"] == ["subnet-priv-untagged"]


class TestDiscoverPartialFailures:
    """Each step is wrapped in try/except — one missing/erroring AWS
    call shouldn't blow up the whole discovery."""

    def test_describe_subnets_failure_returns_none_for_subnets(self, svc):
        ec2 = _make_ec2()
        ec2.describe_subnets.side_effect = RuntimeError("AccessDenied")
        out = svc._discover_vpc_resources(ec2, "vpc-1", "c1")
        # Subnets None; vpc_cidr still discovered from describe_vpcs.
        assert out["public_subnet_ids"] is None
        assert out["private_subnet_ids"] is None
        assert out["vpc_cidr"] == "10.0.0.0/16"
        svc.log_warning.assert_called()

    def test_no_nat_gateway_returns_none_silently(self, svc):
        ec2 = _make_ec2(natgws=[])  # cost-optimized VPC — no NAT
        out = svc._discover_vpc_resources(ec2, "vpc-1", "c1")
        assert out["nat_gateway_id"] is None
        # NOT a warning — empty result is normal for some VPC layouts.
        # (We only log_warning on actual exceptions.)
