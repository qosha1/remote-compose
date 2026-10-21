"""provider_config.ecs.placement_azs pins default task + EC2 placement to AZs.

startsimpli-prod runs one postgres task in us-west-2b. rc renders the ASG and
every default-placed service across ALL rc-managed private subnets, so a
celery-worker landed in us-west-2a and read ~500 GB/day from postgres across
the AZ boundary (InterZone transfer, ~$149/mo). private_subnet_ids cannot pin
it in create mode (the refs are aws_subnet.private[*].id), and lowering the
subnet count would destroy the wrong subnet. placement_azs filters the
placement refs only: subnets, route tables, NAT and the ALB are untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, **ecs_extra) -> DeployContext:
    ecs = {
        "region": "us-west-2",
        "cluster": "c",
        "vpc_cidr": "10.0.0.0/16",
        "default_launch_type": "EC2",
        "default_subnet_placement": "private",
    }
    ecs.update(ecs_extra)
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "worker": ServiceSpec(name="worker", cpu=512, memory=1024, replicas=1)
        },
    )


def _emit(tmp_path: Path, **ecs_extra) -> tuple[str, str, str]:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, **ecs_extra), out)
    return (
        (out / "capacity.tf").read_text(),
        (out / "services.tf").read_text(),
        (out / "network.tf").read_text(),
    )


FILTER = (
    '[for s in aws_subnet.private : s.id if contains(["us-west-2b"], '
    "s.availability_zone)]"
)


def test_unset_is_unchanged(tmp_path):
    cap, svc, _ = _emit(tmp_path)
    assert "vpc_zone_identifier = aws_subnet.private[*].id" in cap
    assert "subnets          = aws_subnet.private[*].id" in svc


def test_pins_the_asg_and_default_service_placement(tmp_path):
    cap, svc, _ = _emit(tmp_path, placement_azs=["us-west-2b"])
    assert f"vpc_zone_identifier = {FILTER}" in cap
    assert f"subnets          = {FILTER}" in svc


def test_does_not_touch_the_subnets_themselves(tmp_path):
    _, _, before = _emit(tmp_path)
    _, _, after = _emit(tmp_path / "x", placement_azs=["us-west-2b"])
    assert before == after


@pytest.mark.parametrize("bad", ["us-west-2b", [], [""], [2]])
def test_rejects_malformed(tmp_path, bad):
    with pytest.raises(ProviderConfigError, match="placement_azs"):
        _emit(tmp_path, placement_azs=bad)


def test_rejected_with_an_adopted_vpc(tmp_path):
    with pytest.raises(ProviderConfigError, match="placement_azs"):
        _emit(
            tmp_path,
            placement_azs=["us-west-2b"],
            vpc_id="vpc-1",
            public_subnet_ids=["subnet-a", "subnet-b"],
        )
