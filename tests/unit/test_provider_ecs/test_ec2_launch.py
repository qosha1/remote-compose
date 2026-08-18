"""Unit tests for EC2 launch type + ASG capacity provider (Phase 6b.2)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import RecordingTerraformRunner


def _ctx(
    tmp_path: Path, services: dict, rc_yml_v2: dict | None = None, **ecs_cfg_overrides
) -> DeployContext:
    ecs_cfg = {
        "region": "us-west-2",
        "cluster": "test-cluster",
        "vpc_cidr": "10.0.0.0/16",
    }
    ecs_cfg.update(ecs_cfg_overrides)
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc_yml_v2 or {},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _svc(
    name: str,
    launch_type: str | None = None,
    cpu: int = 512,
    memory: int = 1024,
    replicas: int = 1,
) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        cpu=cpu,
        memory=memory,
        replicas=replicas,
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
        # largest single task = 2048 CPU / 4096 MiB. 4096 MiB is t3.medium's
        # full nominal memory, and autosize reserves headroom for the ECS
        # agent + OS (see RESERVED_MEMORY_MIB in autosize.py), so t3.medium
        # no longer has enough allocatable memory and this bumps to t3.large.
        assert 'instance_type = "t3.large"' in cap
        # multiple instances needed to cover sum with headroom
        assert "desired_capacity    =" in cap

    def test_high_replica_tiny_task_hits_eni_ceiling_not_cpu_mem(self, tmp_path):
        """rc-e5u.25.1: a single tiny-footprint service with many replicas
        needs more instances than cpu/memory demand alone would suggest,
        because awsvpc caps t3.small at 2 usable task ENIs (max_enis=3, one
        reserved for the instance's own primary ENI). By cpu/memory alone
        this would size to a single t3.small instance; the ENI ceiling
        forces desired_capacity up instead."""
        ctx = _ctx(
            tmp_path,
            {
                "worker": _svc(
                    "worker", launch_type="EC2", cpu=128, memory=128, replicas=5
                ),
            },
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert 'instance_type = "t3.small"' in cap
        # ceil(5 replicas * 1.2 headroom / 2 usable ENIs) = 3 instances,
        # not the 1 that cpu/memory math alone (128 cpu / 128 MiB) implies.
        assert "desired_capacity    = 3" in cap

    def test_ec2_capacity_max_too_small_for_eni_demand_raises(self, tmp_path):
        """`ec2_capacity.max` is threaded into auto_size() as its max_cap
        ceiling, not just applied to the rendered ASG after the fact — so
        when declared demand needs more instances than that ceiling allows,
        emit_terraform must fail loudly instead of rendering an invalid
        aws_autoscaling_group (desired_capacity > max_size)."""
        ctx = _ctx(
            tmp_path,
            {
                "worker": _svc(
                    "worker", launch_type="EC2", cpu=128, memory=128, replicas=30
                ),
            },
            ec2_capacity={"max": 5},
        )
        with pytest.raises(ProviderConfigError, match="ec2_capacity"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_raising_ec2_capacity_max_resolves_the_eni_ceiling(self, tmp_path):
        """The ValueError's advice ("raise ec2_capacity.max") has to
        actually work: bumping `max` high enough for the same 30-replica
        demand that failed above must let auto_size() succeed."""
        ctx = _ctx(
            tmp_path,
            {
                "worker": _svc(
                    "worker", launch_type="EC2", cpu=128, memory=128, replicas=30
                ),
            },
            ec2_capacity={"max": 20},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert 'instance_type = "t3.small"' in cap
        # ceil(30 * 1.2 / 2) = 18, within the raised max_cap=20.
        assert "desired_capacity    = 18" in cap
        assert "max_size            = 20" in cap


class TestExplicitInstanceTypeDensityValidation:
    """rc-e5u.25.10: auto_size() never runs when ec2_capacity.instance_type
    is set explicitly, so nothing else checked whether the declared EC2 task
    demand actually fits -- infeasible config emitted clean terraform and
    only failed later as tasks stuck PENDING in real ECS."""

    def test_eni_demand_exceeds_explicit_shape_raises(self, tmp_path):
        # t3a.small: max_enis=2 -> 1 usable ENI slot per instance.
        ctx = _ctx(
            tmp_path,
            {
                "worker": _svc(
                    "worker", launch_type="EC2", cpu=128, memory=128, replicas=3
                ),
            },
            ec2_capacity={"instance_type": "t3a.small", "desired": 1},
        )
        with pytest.raises(ProviderConfigError, match="awsvpc task ENI slots"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_memory_demand_exceeds_explicit_shape_raises(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2", cpu=256, memory=6144)},
            ec2_capacity={"instance_type": "t3.small", "desired": 1},
        )
        with pytest.raises(ProviderConfigError, match="cannot host a task"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_fitting_demand_on_explicit_shape_succeeds(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2", cpu=512, memory=1024)},
            ec2_capacity={"instance_type": "m5.large", "desired": 1},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert 'instance_type = "m5.large"' in cap

    def test_unverified_instance_type_skips_validation(self, tmp_path):
        """An instance type rc has no verified ENI/cpu/mem data for is not
        modeled -- same "not modeled" precedent as a custom auto_size()
        ladder -- so it must NOT raise, even for demand that would clearly
        never fit any real instance."""
        ctx = _ctx(
            tmp_path,
            {
                "worker": _svc(
                    "worker",
                    launch_type="EC2",
                    cpu=999999,
                    memory=999999,
                    replicas=999,
                ),
            },
            ec2_capacity={"instance_type": "not-a-real-instance-type", "desired": 1},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert 'instance_type = "not-a-real-instance-type"' in cap


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


class TestEc2CapacitySubnetGroup:
    """rc-e5u.25.6: ec2_capacity.subnet_group opts the ASG's instances into a
    DECLARED network.subnets group, through the same resolver
    (_resolve_subnet_group_placement) a service's own subnet_group: uses.

    Absent the knob, ec2_capacity keeps rc-e5u.25.5's
    default_subnet_placement-derived behavior exactly -- covered by
    test_golden.py (byte-identical fixture) and
    test_default_subnet_placement.py, not repeated here.
    """

    def _ctx(self, tmp_path, network, ec2_capacity=None, **ecs_cfg_overrides):
        return _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            rc_yml_v2={"network": network},
            ec2_capacity=ec2_capacity or {},
            **ecs_cfg_overrides,
        )

    def test_declared_public_group(self, tmp_path):
        ctx = self._ctx(
            tmp_path,
            {"subnets": {"asg-public": {"public": True}}},
            {"subnet_group": "asg-public"},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.rc_asg_public[*].id" in cap
        assert "associate_public_ip_address = true" in cap

    def test_declared_private_nat_group(self, tmp_path):
        ctx = self._ctx(
            tmp_path,
            {"subnets": {"asg-nat": {"egress": "nat"}}},
            {"subnet_group": "asg-nat"},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        network_declared = (out / "network_declared.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.rc_asg_nat[*].id" in cap
        assert "associate_public_ip_address = false" in cap
        assert "aws_nat_gateway" in network_declared

    def test_declared_endpoints_group_is_rejected(self, tmp_path):
        """A container instance's ${project}-ec2-instances security group is
        never a member of network.security_groups, so it can never be
        admitted by a VPC endpoint's own security group -- unlike a service,
        ec2_capacity has no `security_groups:` override to fix this with.
        Refuse unconditionally rather than let this look like a fixable
        "missing endpoint" case (see network_plan.check_endpoint_reachability
        for the AWS-doc citations on what a container instance additionally
        needs -- ecs / ecs-agent / ecs-telemetry -- just to register)."""
        ctx = self._ctx(
            tmp_path,
            {
                # A declared group's egress rule is what keeps
                # validate_network_refs' orphan-endpoint check (a separate,
                # earlier guard) from firing -- it does NOT make the endpoint
                # reachable from the ec2_instances SG the ASG actually uses,
                # which is the gap under test here.
                "security_groups": {"unused": {"egress": [{"to": "endpoint:ecs"}]}},
                "subnets": {"asg-endpoints": {"egress": "endpoints"}},
                "endpoints": {
                    "ecs": {
                        "services": ["ecs", "ecs-agent", "ecs-telemetry"],
                        "subnets": ["asg-endpoints"],
                    }
                },
            },
            {"subnet_group": "asg-endpoints"},
        )
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        assert "ec2_capacity.subnet_group" in str(exc.value)
        assert "ec2-instances" in str(exc.value)
        assert "ecs-agent" in str(exc.value)

    def test_undeclared_group_name_rejected(self, tmp_path):
        ctx = self._ctx(
            tmp_path,
            {"subnets": {"asg-public": {"public": True}}},
            {"subnet_group": "typo-dname"},
        )
        with pytest.raises(
            ProviderConfigError, match="ec2_capacity.subnet_group"
        ) as exc:
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        assert "typo-dname" in str(exc.value)
        assert "asg-public" in str(exc.value)  # names what IS known

    def test_undeclared_group_name_rejected_even_with_no_network_block(self, tmp_path):
        """No `network:` block at all -- known groups is 'none'."""
        ctx = _ctx(
            tmp_path,
            {"worker": _svc("worker", launch_type="EC2")},
            ec2_capacity={"subnet_group": "nope"},
        )
        with pytest.raises(ProviderConfigError, match="known: none"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_absent_subnet_group_is_unaffected(self, tmp_path):
        """No ec2_capacity.subnet_group -- falls straight through to
        rc-e5u.25.5's default (public subnets, byte-identical)."""
        ctx = self._ctx(
            tmp_path,
            {"subnets": {"asg-public": {"public": True}}},
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.public[*].id" in cap

    def test_absent_subnet_group_still_honors_default_subnet_placement_private(
        self, tmp_path
    ):
        """No ec2_capacity.subnet_group, but default_subnet_placement:
        private -- the fallback branch of _resolve_subnet_group_placement
        must still route through rc-0cv's private default, not silently get
        overwritten by the new resolver. Neither test_golden.py (public
        default) nor test_default_subnet_placement.py (no EC2 service) would
        catch a wiring mistake here."""
        ctx = self._ctx(
            tmp_path,
            {"subnets": {"asg-public": {"public": True}}},
            default_subnet_placement="private",
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        cap = (out / "capacity.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.private[*].id" in cap
        assert "associate_public_ip_address = false" in cap


# ---------------------------------------------------------------------
# rc-e5u.25.9: destroy() pre-drains EC2-launch-type services + their
# capacity ASG via the AWS SDK before terraform destroy ever runs.
# See ECSProvider._predrain_ec2_capacity's docstring for the full
# mechanism / citations. These tests mock terraform (RecordingTerraformRunner)
# and boto3 (per-service-name MagicMocks) — no real terraform or AWS calls.
# ---------------------------------------------------------------------


def _multi_client_session():
    """A mock boto3 Session whose .client(name) returns a DISTINCT
    MagicMock per service name (unlike a single shared mock), so
    assertions on the "ecs" client can't accidentally pass because of
    calls actually made against the "autoscaling" client or vice versa."""
    clients: dict[str, mock.MagicMock] = {}

    def _client(name, *args, **kwargs):
        return clients.setdefault(name, mock.MagicMock())

    sess = mock.MagicMock()
    sess.client.side_effect = _client
    sess.clients = clients  # test-only convenience accessor
    return sess


@pytest.fixture
def recorder():
    holder: dict = {"runner": None}

    def factory(out_dir: Path) -> RecordingTerraformRunner:
        if holder["runner"] is None:
            holder["runner"] = RecordingTerraformRunner(out_dir)
        return holder["runner"]

    factory.holder = holder  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def mock_session():
    return _multi_client_session()


@pytest.fixture
def provider(recorder, mock_session):
    return ECSProvider(
        runner_factory=recorder,
        session_factory=lambda ctx: mock_session,
    )


def _already_drained(ecs_client, autoscaling_client) -> None:
    """Script both clients so the pre-drain wait loops exit on their
    FIRST poll (no sleeping, no reliance on RC_DESTROY_DRAIN_TIMEOUT_S)."""
    ecs_client.describe_services.return_value = {
        "services": [{"serviceName": "worker", "runningCount": 0}],
    }
    autoscaling_client.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"Instances": []}],
    }
    ecs_client.list_container_instances.return_value = {
        "containerInstanceArns": [],
    }


class TestDestroyPreDrainFargateUnaffected:
    def test_fargate_only_destroy_makes_zero_aws_calls(
        self, provider, recorder, mock_session, tmp_path
    ):
        """Regression guard for the proven Fargate destroy path
        (test_ecs_full_lifecycle.py): a Fargate-only stack's destroy()
        must not touch AWS at all."""
        ctx = _ctx(tmp_path, {"web": _svc("web", launch_type="FARGATE")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        runner = recorder(ctx.working_dir / "terraform")
        runner.calls.clear()

        provider.destroy(ctx)

        assert mock_session.client.call_count == 0
        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "destroy"]


class TestDestroyPreDrainEc2:
    def test_predrain_sets_desired_count_zero_before_terraform_destroy(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(
            tmp_path, {"worker": _svc("worker", launch_type="EC2")}, cluster="myclust"
        )
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        runner = recorder(ctx.working_dir / "terraform")
        runner.calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        _already_drained(ecs, autoscaling)

        provider.destroy(ctx)

        ecs.update_service.assert_called_once_with(
            cluster="myclust", service="worker", desiredCount=0
        )
        autoscaling.update_auto_scaling_group.assert_called_once_with(
            AutoScalingGroupName="myapp-ec2-asg",
            MinSize=0,
            MaxSize=0,
            DesiredCapacity=0,
        )
        # terraform destroy still runs — the pre-drain is additive, not
        # a replacement for terraform's own teardown.
        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "destroy"]

    def test_predrain_deregisters_leftover_container_instances(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        _already_drained(ecs, autoscaling)
        ecs.list_container_instances.return_value = {
            "containerInstanceArns": ["arn:aws:ecs:...:container-instance/abc"]
        }

        provider.destroy(ctx)

        ecs.deregister_container_instance.assert_called_once_with(
            cluster="test-cluster",
            containerInstance="arn:aws:ecs:...:container-instance/abc",
            force=True,
        )

    def test_predrain_only_touches_ec2_services_in_mixed_stack(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(
            tmp_path,
            {
                "web": _svc("web", launch_type="FARGATE"),
                "worker": _svc("worker", launch_type="EC2"),
            },
        )
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.describe_services.return_value = {
            "services": [{"serviceName": "worker", "runningCount": 0}],
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        assert ecs.update_service.call_count == 1
        assert ecs.update_service.call_args.kwargs["service"] == "worker"

    def test_predrain_timeout_warns_but_still_runs_terraform_destroy(
        self, provider, recorder, mock_session, tmp_path, monkeypatch
    ):
        """A task that never stops must not hang destroy() forever —
        terraform's own destroy timeout is the real backstop."""
        monkeypatch.setenv("RC_DESTROY_DRAIN_TIMEOUT_S", "0")
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        runner = recorder(ctx.working_dir / "terraform")
        runner.calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        # Task never drains, ASG instance never terminates.
        ecs.describe_services.return_value = {
            "services": [{"serviceName": "worker", "runningCount": 1}],
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": [{"InstanceId": "i-stuck"}]}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}
        warnings = []

        provider2 = ECSProvider(
            runner_factory=recorder,
            session_factory=lambda c: mock_session,
            progress=warnings.append,
        )
        provider2.destroy(ctx)

        assert any("WARN" in w for w in warnings)
        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "destroy"]

    def test_predrain_never_raises_on_client_error(
        self, provider, recorder, mock_session, tmp_path, monkeypatch
    ):
        """AWS errors during pre-drain (expired creds, service already
        gone, ...) must be swallowed — runner.destroy() is always the
        backstop; a raise here would make the stack undestroyable."""
        # Bounds the autoscaling wait loop to exactly one poll regardless
        # of the (unconfigured, therefore truthy-MagicMock) response
        # shape below — this test is about the ecs client raising, not
        # about timing out the ASG wait.
        monkeypatch.setenv("RC_DESTROY_DRAIN_TIMEOUT_S", "0")
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        runner = recorder(ctx.working_dir / "terraform")
        runner.calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        ecs.update_service.side_effect = RuntimeError("ExpiredTokenException")
        ecs.describe_services.side_effect = RuntimeError("ExpiredTokenException")

        provider.destroy(ctx)  # must not raise

        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "destroy"]

    def test_predrain_treats_service_not_reported_as_already_drained(
        self, provider, recorder, mock_session, tmp_path
    ):
        """describe_services omitting a service (deleted out of band)
        must be treated as drained, not as 'still running forever'."""
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.describe_services.return_value = {"services": []}
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)  # must complete without looping/hanging

        assert ecs.update_service.call_count == 1


# ---------------------------------------------------------------------
# rc-915l: the pre-drain's service list comes from ctx.services, which on
# the destroy path is built with require_compose_file=False — so when the
# compose file is gone (ephemeral stacks delete theirs after deploying),
# every compose-only service silently drops out of it and never gets
# drained. See _predrain_ec2_capacity / _live_ec2_service_names.
# ---------------------------------------------------------------------


class TestDestroyPreDrainEnumeratesLiveServices:
    """A compose-only service can only be EC2 via default_launch_type: EC2
    (launch_type is an rc.yml-only field), and rc.yml services never drop
    out of ctx.services. So that one setting is an exact test for "the
    local list may be incomplete" — not a heuristic.
    """

    def _live(self, ecs, names, *, cp="myapp-ec2-cp"):
        ecs.list_services.return_value = {
            "serviceArns": [f"arn:aws:ecs:us-west-2:1:service/c/{n}" for n in names]
        }
        ecs.describe_services.return_value = {
            "services": [
                {
                    "serviceName": n,
                    "runningCount": 0,
                    "capacityProviderStrategy": [{"capacityProvider": cp}],
                }
                for n in names
            ],
        }

    def test_drains_a_deployed_service_missing_from_the_context(
        self, provider, recorder, mock_session, tmp_path
    ):
        """The bug: compose is gone, so 'worker' isn't in ctx.services at
        all — but it is still running on EC2 capacity in the cluster."""
        ctx = _ctx(tmp_path, {}, default_launch_type="EC2", cluster="myclust")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        self._live(ecs, ["worker"])
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        ecs.update_service.assert_called_once_with(
            cluster="myclust", service="worker", desiredCount=0
        )

    def test_live_services_are_unioned_with_the_local_ones(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path, {"beat": _svc("beat")}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        self._live(ecs, ["worker", "beat"])
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        drained = {c.kwargs["service"] for c in ecs.update_service.call_args_list}
        assert drained == {"beat", "worker"}

    def test_a_foreign_service_on_fargate_capacity_is_left_alone(
        self, provider, recorder, mock_session, tmp_path
    ):
        """rc always creates its own cluster (cluster.tf.j2), and adopt_owned
        covers the ALB only — so a foreign stack can't be in here. The
        capacity-provider filter is the belt to that suspenders: only
        services running on THIS project's EC2 capacity provider are
        drained, never a Fargate service that happens to share the cluster.
        """
        ctx = _ctx(tmp_path, {}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.list_services.return_value = {
            "serviceArns": ["arn:aws:ecs:us-west-2:1:service/c/other"]
        }
        ecs.describe_services.return_value = {
            "services": [
                {"serviceName": "other", "runningCount": 0, "launchType": "FARGATE"},
            ],
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        assert ecs.update_service.call_count == 0

    def test_another_projects_ec2_capacity_provider_is_left_alone(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path, {}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        self._live(ecs, ["theirs"], cp="someone-else-ec2-cp")
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        assert ecs.update_service.call_count == 0

    def test_pagination_is_followed(self, provider, recorder, mock_session, tmp_path):
        """list_services caps at 100 per page; a stack big enough to page
        must not have its tail silently dropped."""
        ctx = _ctx(tmp_path, {}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.list_services.side_effect = [
            {"serviceArns": ["arn:.../svc-a"], "nextToken": "t1"},
            {"serviceArns": ["arn:.../svc-b"]},
        ]
        ecs.describe_services.side_effect = lambda **kw: {
            "services": [
                {
                    "serviceName": s.rsplit("/", 1)[-1],
                    "runningCount": 0,
                    "capacityProviderStrategy": [{"capacityProvider": "myapp-ec2-cp"}],
                }
                for s in kw["services"]
            ],
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        drained = {c.kwargs["service"] for c in ecs.update_service.call_args_list}
        assert drained == {"svc-a", "svc-b"}

    def test_describe_is_chunked_to_the_api_limit(
        self, provider, recorder, mock_session, tmp_path
    ):
        """describe_services accepts at most 10 service names per call."""
        names = [f"s{i}" for i in range(23)]
        ctx = _ctx(tmp_path, {}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.list_services.return_value = {
            "serviceArns": [f"arn:.../{n}" for n in names]
        }
        seen_batches = []

        def _describe(**kw):
            seen_batches.append(len(kw["services"]))
            return {
                "services": [
                    {
                        "serviceName": s.rsplit("/", 1)[-1],
                        "runningCount": 0,
                        "capacityProviderStrategy": [
                            {"capacityProvider": "myapp-ec2-cp"}
                        ],
                    }
                    for s in kw["services"]
                ],
            }

        ecs.describe_services.side_effect = _describe
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)

        assert max(seen_batches) <= 10
        assert ecs.update_service.call_count == 23

    def test_a_vanished_service_in_failures_is_ignored(
        self, provider, recorder, mock_session, tmp_path
    ):
        """A service deleted between list_services and describe_services
        comes back under failures[], not services[] — it's already gone,
        which is the desired end state, so it must not blow up the drain."""
        ctx = _ctx(tmp_path, {}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.list_services.return_value = {"serviceArns": ["arn:.../ghost"]}
        ecs.describe_services.return_value = {
            "services": [],
            "failures": [{"arn": "arn:.../ghost", "reason": "MISSING"}],
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)  # must not raise

        assert ecs.update_service.call_count == 0

    def test_enumeration_failure_falls_back_to_the_local_list(
        self, provider, recorder, mock_session, tmp_path
    ):
        """Best-effort: if the cluster can't be listed, drain what we know
        about rather than draining nothing."""
        ctx = _ctx(tmp_path, {"beat": _svc("beat")}, default_launch_type="EC2")
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        ecs.list_services.side_effect = RuntimeError("ClusterNotFoundException")
        ecs.describe_services.return_value = {
            "services": [{"serviceName": "beat", "runningCount": 0}],
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": []}],
        }
        ecs.list_container_instances.return_value = {"containerInstanceArns": []}

        provider.destroy(ctx)  # must not raise

        ecs.update_service.assert_called_once_with(
            cluster="test-cluster", service="beat", desiredCount=0
        )


class TestDestroyPreDrainStillMakesZeroCallsWhenItCan:
    """The gate has to stay exact in the other direction too: with
    default_launch_type left at FARGATE, an EC2 service must have said so
    in rc.yml, and rc.yml services never drop out of ctx.services — so the
    local list is provably complete and enumerating live would be a
    pointless AWS call on every Fargate destroy.
    """

    def test_fargate_default_with_no_ec2_services_lists_nothing(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path, {"web": _svc("web", launch_type="FARGATE")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()

        provider.destroy(ctx)

        assert mock_session.client.call_count == 0

    def test_fargate_default_with_an_explicit_ec2_service_does_not_enumerate(
        self, provider, recorder, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path, {"worker": _svc("worker", launch_type="EC2")})
        provider.emit_terraform(ctx, ctx.working_dir / "terraform")
        recorder(ctx.working_dir / "terraform").calls.clear()
        ecs = mock_session.clients.setdefault("ecs", mock.MagicMock())
        autoscaling = mock_session.clients.setdefault("autoscaling", mock.MagicMock())
        _already_drained(ecs, autoscaling)

        provider.destroy(ctx)

        ecs.list_services.assert_not_called()
        ecs.update_service.assert_called_once_with(
            cluster="test-cluster", service="worker", desiredCount=0
        )
