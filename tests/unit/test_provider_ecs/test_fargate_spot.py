"""Per-service ``capacity_type: SPOT`` -> FARGATE_SPOT (rc-2ja3)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config._errors import ConfigError
from remote_compose.config._schema_parser import _parse_service
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict, **ecs_cfg_overrides) -> DeployContext:
    ecs_cfg = {
        "region": "us-west-2",
        "cluster": "test-cluster",
        "vpc_cidr": "10.0.0.0/16",
    }
    ecs_cfg.update(ecs_cfg_overrides)
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _svc(name, launch_type=None, capacity_type="ON_DEMAND", cpu=1024, memory=4096):
    return ServiceSpec(
        name=name,
        cpu=cpu,
        memory=memory,
        type="worker",
        launch_type=launch_type,
        capacity_type=capacity_type,
    )


def _render(tmp_path, services, **cfg):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, **cfg), out)
    return (out / "services.tf").read_text(), (out / "cluster.tf").read_text()


def _service_block(services_tf: str, name: str) -> str:
    m = re.search(
        r'resource "aws_ecs_service" "%s" \{.*?\n\}\n' % re.escape(name),
        services_tf,
        re.S,
    )
    assert m, f"no aws_ecs_service for {name}"
    return m.group(0)


class TestFargateSpotRender:
    def test_spot_renders_strategy_and_no_launch_type(self, tmp_path):
        tf, _ = _render(
            tmp_path,
            {"lathe": _svc("lathe", launch_type="FARGATE", capacity_type="SPOT")},
        )
        block = _service_block(tf, "lathe")
        assert 'capacity_provider = "FARGATE_SPOT"' in block
        assert "weight            = 1" in block
        assert "launch_type" not in block
        assert "force_new_deployment = true" in block
        assert "assign_public_ip" in block  # awsvpc network config intact

    def test_on_demand_fargate_unchanged(self, tmp_path):
        tf, _ = _render(tmp_path, {"web": _svc("web", launch_type="FARGATE")})
        block = _service_block(tf, "web")
        assert 'launch_type     = "FARGATE"' in block
        assert "capacity_provider_strategy" not in block
        assert "FARGATE_SPOT" not in block

    def test_spot_in_ec2_default_stack_with_explicit_fargate(self, tmp_path):
        tf, cluster = _render(
            tmp_path,
            {
                "api": _svc("api"),  # inherits EC2
                "lathe": _svc("lathe", launch_type="FARGATE", capacity_type="SPOT"),
            },
            default_launch_type="EC2",
        )
        lathe = _service_block(tf, "lathe")
        assert 'capacity_provider = "FARGATE_SPOT"' in lathe
        assert "launch_type" not in lathe
        api = _service_block(tf, "api")
        assert "aws_ecs_capacity_provider.ec2.name" in api
        assert "FARGATE_SPOT" not in api
        # cluster keeps FARGATE + FARGATE_SPOT + the ec2 provider
        assert '"FARGATE",' in cluster
        assert '"FARGATE_SPOT",' in cluster
        assert "aws_ecs_capacity_provider.ec2.name" in cluster

    def test_cluster_lists_fargate_spot_with_no_spot_service(self, tmp_path):
        _, cluster = _render(tmp_path, {"web": _svc("web")})
        assert '"FARGATE_SPOT",' in cluster


class TestFargateTaskDefInEc2DefaultStack:
    """A launch_type: FARGATE service in an EC2-default stack must still get a
    valid Fargate task definition (cpu 1024 / memory 4096 is a valid pair)."""

    def test_task_definition_shape(self, tmp_path):
        tf, _ = _render(
            tmp_path,
            {
                "api": _svc("api", cpu=512, memory=1024),
                "lathe": _svc("lathe", launch_type="FARGATE", capacity_type="SPOT"),
            },
            default_launch_type="EC2",
        )
        m = re.search(
            r'resource "aws_ecs_task_definition" "lathe" \{.*?\n\}\n', tf, re.S
        )
        assert m
        td = m.group(0)
        assert 'network_mode             = "awsvpc"' in td
        assert 'requires_compatibilities = ["FARGATE"]' in td
        assert 'cpu                      = "1024"' in td
        assert 'memory                   = "4096"' in td
        assert "execution_role_arn       = aws_iam_role.task_execution.arn" in td


class TestValidation:
    def test_spot_inherited_ec2_is_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match=r"'lathe'.*launch_type: FARGATE"):
            _render(
                tmp_path,
                {"lathe": _svc("lathe", capacity_type="SPOT")},
                default_launch_type="EC2",
            )

    def test_spot_explicit_ec2_is_config_error_in_provider(self, tmp_path):
        with pytest.raises(ConfigError, match=r"'lathe'.*launch_type: FARGATE"):
            _render(
                tmp_path,
                {"lathe": _svc("lathe", launch_type="EC2", capacity_type="SPOT")},
            )

    def test_schema_rejects_spot_with_explicit_ec2(self):
        sv = _parse_service(
            "lathe", {"cpu": 1024, "memory": 4096, "launch_type": "EC2", "capacity_type": "SPOT"}
        )
        with pytest.raises(ConfigError, match=r"'lathe'.*launch_type: FARGATE"):
            sv.validate()

    def test_schema_rejects_mixed_per_service(self):
        sv = _parse_service("x", {"capacity_type": "MIXED"})
        with pytest.raises(ConfigError, match="capacity_type"):
            sv.validate()

    def test_schema_default_is_on_demand(self):
        assert _parse_service("x", {}).capacity_type == "ON_DEMAND"

    def test_schema_accepts_spot_fargate_and_unset_launch(self):
        _parse_service("x", {"launch_type": "FARGATE", "capacity_type": "SPOT"}).validate()
        _parse_service("x", {"capacity_type": "SPOT"}).validate()  # resolved by provider
