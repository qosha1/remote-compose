"""Unit tests for EC2 launch type + ASG capacity provider (Phase 6b.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
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


def _svc(
    name: str, launch_type: str | None = None, cpu: int = 512, memory: int = 1024
) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        cpu=cpu,
        memory=memory,
        replicas=1,
        type="application",
        launch_type=launch_type,
    )


class TestFargateOnly:
    def test_no_capacity_tf_when_all_fargate(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, {"web": _svc("web")}), out)
        cap = (out / "capacity.tf").read_text()
        assert cap.strip() == "", "capacity.tf should be empty when no EC2 services"

    def test_cluster_capacity_providers_excludes_ec2(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, {"web": _svc("web")}), out)
        cluster = (out / "cluster.tf").read_text()
        assert "aws_ecs_capacity_provider.ec2.name" not in cluster

    def test_fargate_service_uses_launch_type(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, {"web": _svc("web")}), out)
        services = (out / "services.tf").read_text()
        assert 'launch_type     = "FARGATE"' in services
        assert "capacity_provider_strategy" not in services


class TestEc2Only:
    def test_capacity_tf_populated_for_ec2_service(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2", cpu=1024, memory=2048)},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "aws_launch_template" in cap
        assert "aws_autoscaling_group" in cap
        assert "aws_ecs_capacity_provider" in cap

    def test_ec2_service_uses_capacity_provider_strategy(self, tmp_path):
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert "capacity_provider_strategy" in services
        assert "aws_ecs_capacity_provider.ec2.name" in services
        assert 'launch_type     = "FARGATE"' not in services

    def test_task_definition_requires_ec2_compatibility(self, tmp_path):
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert 'requires_compatibilities = ["EC2"]' in services

    def test_cluster_includes_ec2_capacity_provider(self, tmp_path):
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cluster = (out / "cluster.tf").read_text()
        assert "aws_ecs_capacity_provider.ec2.name" in cluster


class TestMixedMode:
    def test_fargate_and_ec2_coexist_in_same_module(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "web": _svc("web", launch_type="FARGATE"),
                "worker": _svc("worker", launch_type="EC2"),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        # Both shapes present: FARGATE (launch_type) and EC2 (capacity_provider_strategy)
        assert 'launch_type     = "FARGATE"' in services
        assert "capacity_provider_strategy" in services
        # capacity.tf present because at least one EC2 service
        assert (out / "capacity.tf").read_text().strip() != ""

    def test_default_launch_type_ec2_applies_to_services_without_override(
        self, tmp_path
    ):
        ctx = _ctx(
            tmp_path,
            {
                "worker": _svc("worker"),  # no launch_type
            },
            default_launch_type="EC2",
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert 'requires_compatibilities = ["EC2"]' in services
        assert "capacity_provider_strategy" in services

    def test_service_launch_type_overrides_default(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {
                "web": _svc("web", launch_type="FARGATE"),  # override
            },
            default_launch_type="EC2",
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services = (out / "services.tf").read_text()
        assert 'launch_type     = "FARGATE"' in services
        # no capacity.tf content because no EC2 services
        assert (out / "capacity.tf").read_text().strip() == ""


class TestCapacityType:
    def test_on_demand_renders_simple_launch_template(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            ec2_capacity={"capacity_type": "ON_DEMAND"},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "launch_template {" in cap
        assert "mixed_instances_policy" not in cap

    def test_spot_renders_mixed_instances_policy(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            ec2_capacity={"capacity_type": "SPOT"},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "mixed_instances_policy" in cap
        assert "on_demand_percentage_above_base_capacity = 0" in cap

    def test_mixed_renders_mixed_instances_policy_with_percentage(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            ec2_capacity={"capacity_type": "MIXED", "spot_weight": 3},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "mixed_instances_policy" in cap
        assert "on_demand_base_capacity                  = 1" in cap

    def test_invalid_capacity_type_rejected(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            ec2_capacity={"capacity_type": "BOGUS"},
        )
        with pytest.raises(ProviderConfigError, match="capacity_type"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestAutoSizing:
    def test_explicit_instance_type_wins(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2", cpu=512, memory=1024)},
            ec2_capacity={
                "instance_type": "m5.2xlarge",
                "min": 2,
                "max": 10,
                "desired": 3,
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert 'instance_type = "m5.2xlarge"' in cap
        assert "desired_capacity    = 3" in cap

    def test_auto_size_when_instance_type_absent(self, tmp_path):
        """Summed demand 6144 CPU + 12 GiB should produce a working sizing."""
        ctx = _ctx(
            tmp_path,
            {
                "a": _svc("a", launch_type="EC2", cpu=2048, memory=4096),
                "b": _svc("b", launch_type="EC2", cpu=2048, memory=4096),
                "c": _svc("c", launch_type="EC2", cpu=2048, memory=4096),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        # largest single task = 2048/4096 → t3.medium
        assert 'instance_type = "t3.medium"' in cap
        # multiple instances needed to cover sum with headroom
        assert "desired_capacity    =" in cap


class TestDefaultLaunchTypeValidation:
    def test_invalid_default_launch_type(self, tmp_path):
        ctx = _ctx(tmp_path, {"web": _svc("web")}, default_launch_type="SPOT")
        with pytest.raises(ProviderConfigError, match="default_launch_type"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestImdsHardening:
    """IMDS lives on the launch template, not the task definition.

    ``aws_ecs_task_definition`` has no ``metadata_options`` argument, and a
    Fargate task takes its credentials from the task metadata endpoint
    (169.254.170.2) rather than IMDS. The instance role is the thing worth
    stealing, so the hardening belongs on ``aws_launch_template.ec2``.
    """

    def _cap(self, tmp_path, **ec2_capacity):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            **({"ec2_capacity": ec2_capacity} if ec2_capacity else {}),
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        return (out / "capacity.tf").read_text()

    def test_imdsv2_required_by_default(self, tmp_path):
        cap = self._cap(tmp_path)
        assert "metadata_options {" in cap
        assert 'http_tokens                 = "required"' in cap
        assert 'http_endpoint               = "enabled"' in cap

    def test_hop_limit_defaults_to_two_not_one(self, tmp_path):
        """1 cuts off every bridge-mode container on the instance."""
        assert "http_put_response_hop_limit = 2" in self._cap(tmp_path)

    def test_strict_hop_limit_is_opt_in(self, tmp_path):
        cap = self._cap(tmp_path, metadata_hop_limit=1)
        assert "http_put_response_hop_limit = 1" in cap

    def test_imdsv1_can_be_re_enabled_for_a_stack_that_needs_it(self, tmp_path):
        cap = self._cap(tmp_path, imdsv2="optional")
        assert 'http_tokens                 = "optional"' in cap

    def test_task_imds_block_is_off_by_default(self, tmp_path):
        assert "echo ECS_AWSVPC_BLOCK_IMDS" not in self._cap(tmp_path)

    def test_task_imds_block_writes_the_agent_config(self, tmp_path):
        """The knob that actually denies awsvpc tasks the instance role."""
        cap = self._cap(tmp_path, block_task_imds=True)
        assert "echo ECS_AWSVPC_BLOCK_IMDS=true >> /etc/ecs/ecs.config" in cap

    @pytest.mark.parametrize("value", ["enforced", "yes", True, 1])
    def test_invalid_imdsv2_mode_rejected(self, tmp_path, value):
        with pytest.raises(ProviderConfigError, match="imdsv2"):
            self._cap(tmp_path, imdsv2=value)

    @pytest.mark.parametrize("value", [0, 65, -1, "2", 2.5, True])
    def test_invalid_hop_limit_rejected(self, tmp_path, value):
        with pytest.raises(ProviderConfigError, match="metadata_hop_limit"):
            self._cap(tmp_path, metadata_hop_limit=value)

    def test_no_metadata_options_on_task_definitions(self, tmp_path):
        """Guard against the knob being re-added where AWS has no such field."""
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert "metadata_options" not in (out / "services.tf").read_text()
