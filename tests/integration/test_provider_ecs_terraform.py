"""Truth test for ECSProvider.emit_terraform.

Runs ``terraform init -backend=false && terraform validate`` against the
emitted module. If this passes, the HCL is syntactically and
semantically valid according to the AWS provider.

Skipped automatically when terraform is not usable in this environment
(see sentinel in test_terraform_runner).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import TerraformRunner

pytestmark = pytest.mark.integration


def _terraform_usable() -> bool:
    if not shutil.which("terraform"):
        return False
    try:
        result = subprocess.run(
            ["terraform", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_terraform = pytest.mark.skipif(
    not _terraform_usable(),
    reason="terraform binary not usable in this environment (binary missing or sandboxed)",
)


@pytest.fixture
def ecs_ctx(tmp_path):
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
            ),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=[],
    )


@requires_terraform
class TestEmittedHclValidates:
    def test_terraform_init_and_validate(self, ecs_ctx, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ecs_ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()


def _multi_domain_ctx(tmp_path):
    """Two public services on distinct subdomains of one zone — exercises
    the rc-e5u.39 multi-domain ALB routing path (per-service TG, host-header
    listener rules, multi-SAN ACM cert)."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
                domain="web.example.com",
            ),
            "api": ServiceSpec(
                name="api",
                cpu=512,
                memory=1024,
                type="application",
                public=True,
                port=8000,
                health_check_path="/health",
                domain="api.example.com",
            ),
        },
        secrets=[],
    )


def _alias_ctx(tmp_path):
    """One public service with a primary domain + 2 aliases. Exercises the
    rc-e5u.40 nginx-as-front + aliases path: SANs grow, R53 records grow,
    listener rules do NOT (default action handles all)."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "nginx": ServiceSpec(
                name="nginx",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/",
                domain="primary.example.com",
                aliases=["a.example.com", "b.example.com"],
            ),
        },
        secrets=[],
    )


@requires_terraform
class TestMultiDomainEmissionValidates:
    """rc-e5u.39 backfill: the multi-domain ALB output validates against
    real terraform. Unit tests in test_domain.py prove the HCL shape; this
    test proves the AWS provider accepts it."""

    def test_multi_domain_module_passes_terraform_validate(self, tmp_path):
        ctx = _multi_domain_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_multi_domain_emits_per_service_target_group(self, tmp_path):
        """Sanity assertion in addition to validation: each domained service
        gets its own aws_lb_target_group resource."""
        ctx = _multi_domain_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        alb_tf = (out / "alb.tf").read_text()
        # One TG per service.
        assert 'resource "aws_lb_target_group" "web"' in alb_tf
        assert 'resource "aws_lb_target_group" "api"' in alb_tf


@requires_terraform
class TestAliasEmissionValidates:
    """rc-e5u.40 backfill: nginx-as-front + aliases output validates against
    real terraform. Confirms the design point: aliases extend SANs and R53
    records but do NOT add listener rules."""

    def test_alias_module_passes_terraform_validate(self, tmp_path):
        ctx = _alias_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_aliases_extend_acm_san_list(self, tmp_path):
        """All 3 hostnames (primary + 2 aliases) appear in the cert config."""
        ctx = _alias_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain_tf = (out / "domain.tf").read_text()
        assert "primary.example.com" in domain_tf
        assert "a.example.com" in domain_tf
        assert "b.example.com" in domain_tf

    def test_aliases_get_r53_records_but_no_listener_rules(self, tmp_path):
        """3 R53 app A records (one per host), but listener rules count == 0
        because the default action handles all 3 via SNI."""
        ctx = _alias_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        domain_tf = (out / "domain.tf").read_text()
        alb_tf = (out / "alb.tf").read_text()
        # 3 app-A-record resources expected (primary + 2 aliases). The
        # cert_validation record (for ACM DNS validation) is separate and
        # not counted here.
        app_records = domain_tf.count('resource "aws_route53_record" "app_')
        assert app_records == 3, (
            f"expected 3 R53 app A records (primary + 2 aliases), got "
            f"{app_records}\n{domain_tf}"
        )
        # Listener rules: aliases must NOT appear in any rule's host_header.
        # (Existing emission keeps a rule for the primary domain even when
        # it's redundant with the default_target action — see the matching
        # unit test test_aliases_do_not_emit_listener_rules.)
        for alias in ("a.example.com", "b.example.com"):
            for rule_block in alb_tf.split('aws_lb_listener_rule"')[1:]:
                rule_block_short = rule_block.split("resource ")[0]
                assert alias not in rule_block_short, (
                    f"alias {alias!r} must not appear in any aws_lb_listener_rule "
                    f"host_header; got block:\n{rule_block_short[:400]}"
                )


def _adopt_ctx(tmp_path):
    """Existing-VPC (adopt) mode (rc-a57): vpc_id + explicit subnets + an extra
    mesh SG. Includes an infrastructure service so service_discovery emits
    (exercises the adopt-mode DHCP-options skip). terraform validate is offline,
    so the fake vpc/subnet ids don't need to exist."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_id": "vpc-0adopt123",
                "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
                "private_subnet_ids": ["subnet-priv-a", "subnet-priv-b"],
                "security_group_ids": ["sg-mesh"],
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
            ),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
            "cache": ServiceSpec(
                name="cache", cpu=256, memory=512, type="infrastructure"
            ),
        },
        secrets=[],
    )


@requires_terraform
class TestExistingVpcHclValidates:
    """rc-a57 truth test: adopt-mode HCL is valid terraform."""

    def test_adopt_mode_init_and_validate(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_adopt_ctx(tmp_path), out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_adopt_mode_creates_no_vpc_or_subnets(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_adopt_ctx(tmp_path), out)
        net = (out / "network.tf").read_text()
        assert 'resource "aws_vpc"' not in net
        assert 'resource "aws_subnet"' not in net
        assert 'data "aws_vpc" "main"' in net
        # adopt mode must not touch the existing VPC's DHCP options
        assert "aws_vpc_dhcp_options" not in (out / "service_discovery.tf").read_text()


def _shared_image_ctx(tmp_path):
    """rc-44i: 3 services share one build (django pattern) -> ONE ECR repo +
    siblings reference it. Truth-tests that the deduped emission (gated
    aws_ecr_repository + outputs) is valid terraform (catches dangling refs)."""
    shared = dict(context=str(tmp_path / "app"), dockerfile="Dockerfile")
    (tmp_path / "app").mkdir(exist_ok=True)

    def svc(name, **extra):
        return ServiceSpec(
            name=name,
            cpu=256,
            memory=512,
            build_context=shared["context"],
            dockerfile=shared["dockerfile"],
            **extra,
        )

    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "django": svc("django", type="application"),
            "celery-worker": svc("celery-worker", type="worker"),
            "celery-beat": svc("celery-beat", type="worker"),
        },
        secrets=[],
    )


@requires_terraform
class TestSharedImageHclValidates:
    """rc-44i truth test: deduped shared-image emission is valid terraform."""

    def test_shared_image_init_and_validate(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_shared_image_ctx(tmp_path), out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_one_repo_three_task_defs(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_shared_image_ctx(tmp_path), out)
        services_tf = (out / "services.tf").read_text()
        assert services_tf.count('resource "aws_ecr_repository" "django"') == 1
        assert 'resource "aws_ecr_repository" "celery_beat"' not in services_tf
        assert services_tf.count('resource "aws_ecs_task_definition"') == 3


def _ec2_default_launch_type_ctx(tmp_path):
    """rc-e5u.25.3: env-wide `default_launch_type: EC2`, no per-service
    override. All 42 tests in test_ec2_launch.py only assert on rendered
    .tf string content -- nothing runs `terraform validate` against this
    shape. Mirrors the top-level `ecs_ctx` fixture (same two services) so
    the only variable being exercised is the launch type."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
                "default_launch_type": "EC2",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
            ),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=[],
    )


def _ec2_mixed_ctx(tmp_path):
    """rc-e5u.25.3: one FARGATE service + one EC2 service in the same
    module, mirroring TestMixedMode.test_fargate_and_ec2_coexist_in_same_module
    in tests/unit/test_provider_ecs/test_ec2_launch.py but run through real
    terraform instead of string assertions."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
                "ec2_capacity": {
                    "capacity_type": "SPOT",
                    "instance_type": "m5.large",
                    "min": 1,
                    "max": 3,
                    "desired": 2,
                },
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/health",
                launch_type="FARGATE",
            ),
            "worker": ServiceSpec(
                name="worker",
                cpu=512,
                memory=1024,
                type="worker",
                launch_type="EC2",
            ),
        },
        secrets=[],
    )


@requires_terraform
class TestEc2LaunchTypeHclValidates:
    """rc-e5u.25.3: terraform-validate coverage for the EC2 launch_type
    path (parent bug: rc-e5u.25 -- capacity.tf.j2 used to hardcode the EC2
    ASG into private subnets with no NAT/public-IP path, unlike the Fargate
    placement logic in services.tf.j2).

    IMPORTANT: `terraform validate` is a static, offline schema/reference
    check -- it never contacts AWS and has no notion of subnet routing. It
    CANNOT catch "ASG placed in a private subnet with no NAT gateway, so
    instances never reach the internet to register with ECS" -- that is a
    runtime reachability failure, only observable via `apply` or by reading
    the HCL. A green run here means the HCL is syntactically and
    referentially valid, not that the stack is reachable -- the structural
    assertion below (`test_default_launch_type_ec2_asg_reaches_internet_via_public_ip`)
    is what pins the actual placement shape.

    rc-e5u.25.5 fixed this: the EC2 ASG now gets the same
    default_subnet_placement-aware placement (public subnets +
    associate_public_ip_address, locked-down SG) that Fargate task ENIs
    already got from rc-0cv. See capacity.tf.j2's network_interfaces block
    and provider.py's reuse of default_placement_subnets_ref /
    default_placement_assign_public_ip for the EC2 capacity_cfg.

    rc-e5u.25.6 adds an opt-in on top: ec2_capacity.subnet_group points the
    ASG at a DECLARED network.subnets group instead of the environment-wide
    default -- see TestEc2CapacitySubnetGroupHclValidates below for that
    path's terraform-validate coverage (public and private+nat groups; a
    declared `egress: endpoints` group is refused before any .tf exists at
    all, so it has no terraform-validate case -- see
    tests/unit/test_provider_ecs/test_ec2_launch.py::
    TestEc2CapacitySubnetGroup::test_declared_endpoints_group_is_rejected).
    """

    def test_default_launch_type_ec2_module_passes_terraform_validate(self, tmp_path):
        ctx = _ec2_default_launch_type_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_default_launch_type_ec2_asg_reaches_internet_via_public_ip(self, tmp_path):
        """rc-e5u.25.5: with no explicit default_subnet_placement, the ASG
        lands in the public subnets (rc-0cv's default) with
        associate_public_ip_address = true, mirroring how Fargate tasks with
        no subnet_group get assign_public_ip = true in services.tf.j2. No
        NAT gateway is needed for this path -- the instances reach the ECS
        control plane / ECR / CloudWatch directly, the same "public subnets
        + locked-down SG" tradeoff Fargate placement already made (SG only
        allows inbound from the ALB SG + self, so the public IP is an
        egress path, not an exposure)."""
        ctx = _ec2_default_launch_type_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        capacity_tf = (out / "capacity.tf").read_text()
        network_tf = (out / "network.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.public[*].id" in capacity_tf
        assert "aws_nat_gateway" not in network_tf
        # vpc_security_group_ids moved inside network_interfaces -- the AWS
        # provider rejects a launch template that sets both.
        assert "network_interfaces {" in capacity_tf
        assert "associate_public_ip_address = true" in capacity_tf
        # device_index / delete_on_termination are schema-optional but set
        # explicitly here to remove reliance on RunInstances-time ENI
        # defaulting -- whether that defaulting would behave the same is a
        # runtime AWS API question `terraform validate` cannot confirm
        # either way.
        assert "device_index                = 0" in capacity_tf
        assert "delete_on_termination       = true" in capacity_tf
        assert (
            "security_groups             = [aws_security_group.ec2_instances.id]"
            in (capacity_tf)
        )
        assert "vpc_security_group_ids = [aws_security_group.ec2_instances.id]" not in (
            capacity_tf
        )

    def test_mixed_fargate_and_ec2_module_passes_terraform_validate(self, tmp_path):
        ctx = _ec2_mixed_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()

    def test_mixed_module_both_launch_shapes_present(self, tmp_path):
        """Sanity check alongside validate(): the FARGATE service keeps
        launch_type, the EC2 service switches to capacity_provider_strategy,
        and capacity.tf is populated -- same assertions as
        TestMixedMode.test_fargate_and_ec2_coexist_in_same_module in the
        unit suite, now proven against real terraform."""
        ctx = _ec2_mixed_ctx(tmp_path)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        assert 'launch_type     = "FARGATE"' in services_tf
        assert "capacity_provider_strategy" in services_tf
        cap = (out / "capacity.tf").read_text()
        assert cap.strip() != ""
        assert "mixed_instances_policy" in cap  # SPOT capacity_type


def _ec2_capacity_declared_subnet_ctx(tmp_path, *, network, subnet_group):
    """rc-e5u.25.6: one EC2 service, ec2_capacity.subnet_group pointed at a
    DECLARED network.subnets group."""
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={"network": network},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "itest-cluster",
                "vpc_cidr": "10.0.0.0/16",
                "ec2_capacity": {"subnet_group": subnet_group},
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "worker": ServiceSpec(
                name="worker",
                cpu=512,
                memory=1024,
                type="worker",
                launch_type="EC2",
            ),
        },
        secrets=[],
    )


@requires_terraform
class TestEc2CapacitySubnetGroupHclValidates:
    """rc-e5u.25.6: ec2_capacity.subnet_group -- terraform-validate coverage
    for the two placements that actually work today (a declared public
    group, and a declared private+nat group). A declared `egress: endpoints`
    group is refused by check_endpoint_reachability before any .tf file
    exists (see network_plan.check_endpoint_reachability's is_ec2_capacity
    branch, and the unit-test coverage in test_ec2_launch.py), so there is
    no terraform-validate case for it -- confirmed by the unit test, not
    repeated here."""

    def test_declared_public_group_passes_terraform_validate(self, tmp_path):
        ctx = _ec2_capacity_declared_subnet_ctx(
            tmp_path,
            network={"subnets": {"asg-public": {"public": True}}},
            subnet_group="asg-public",
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()
        cap = (out / "capacity.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.rc_asg_public[*].id" in cap
        assert "associate_public_ip_address = true" in cap

    def test_declared_private_nat_group_passes_terraform_validate(self, tmp_path):
        ctx = _ec2_capacity_declared_subnet_ctx(
            tmp_path,
            network={"subnets": {"asg-nat": {"egress": "nat"}}},
            subnet_group="asg-nat",
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()
        cap = (out / "capacity.tf").read_text()
        network_declared = (out / "network_declared.tf").read_text()
        assert "vpc_zone_identifier = aws_subnet.rc_asg_nat[*].id" in cap
        assert "associate_public_ip_address = false" in cap
        assert "aws_nat_gateway" in network_declared
