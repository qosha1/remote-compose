"""Terraform emission for the declared ``network:`` / ``repositories:`` blocks.

Covers the planner (CIDR allocation, reference resolution, derived endpoint
ingress) and the rendered HCL, including the guarantee that a config WITHOUT a
network block emits exactly what it did before the feature existed.
"""

from __future__ import annotations

import pytest

from remote_compose.provider import DeployContext, SecretRef, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider

pytestmark = pytest.mark.unit


NETWORK = {
    "security_groups": {
        "runners": {
            "description": "Ephemeral runners.",
            "egress": [
                {"to": "endpoint:ecr"},
                {"to": "endpoint:logs"},
                {"to": "endpoint:s3"},
                {"to": "sg:api", "ports": [5000]},
                {"to": "cidr:0.0.0.0/0", "ports": [53], "protocol": "udp"},
            ],
        },
        "api": {
            "ingress": [
                {"from": "alb", "ports": [5000]},
                {"from": "sg:runners", "ports": [5000]},
            ],
        },
    },
    "subnets": {"runners-private": {"public": False, "egress": "endpoints"}},
    "endpoints": {
        "ecr": {"services": ["ecr.api", "ecr.dkr"], "subnets": ["runners-private"]},
        "logs": {"services": ["logs"], "subnets": ["runners-private"]},
        "s3": {"services": ["s3"], "subnets": ["runners-private"]},
    },
}
REPOSITORIES = {"db-sidecar": {"mirror": "postgres:16-alpine"}}


def _ctx(tmp_path, *, network=None, repositories=None, services=None, secrets=None):
    rc = {
        "version": 2,
        "project": "bmgr",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
    }
    if network is not None:
        rc["network"] = network
    if repositories is not None:
        rc["repositories"] = repositories
    return DeployContext(
        project="bmgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc,
        provider_config={
            "ecs": {"region": "us-west-2", "cluster": "bmgr", "vpc_cidr": "10.0.0.0/16"}
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=(
            services
            if services is not None
            else {
                "worker": ServiceSpec(
                    name="worker",
                    cpu=512,
                    memory=1024,
                    image="busybox",
                    security_groups=["runners"],
                    subnet_group="runners-private",
                ),
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    image="busybox",
                    public=True,
                    port=5000,
                    health_check_path="/health",
                ),
            }
        ),
        secrets=secrets or [],
    )


def _plain():
    """Services with no declared-network references, for blocks that are
    orthogonal to placement (repositories, subnet-only cases)."""
    return {"w": ServiceSpec(name="w", cpu=256, memory=512, image="x")}


def _emit(tmp_path, **kwargs):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, **kwargs), out)
    return {p.name: p.read_text() for p in out.iterdir() if p.is_file()}


class TestNoNetworkBlockIsInert:
    def test_declared_templates_render_empty(self, tmp_path):
        files = _emit(tmp_path, services=_plain())
        assert files["network_declared.tf"].strip() == ""
        assert files["repositories.tf"].strip() == ""

    def test_services_keep_the_shared_group_and_public_subnets(self, tmp_path):
        files = _emit(tmp_path, services=_plain())
        assert "subnets          = aws_subnet.public[*].id" in files["services.tf"]
        assert (
            "security_groups  = [aws_security_group.tasks.id]" in files["services.tf"]
        )
        assert "assign_public_ip = true" in files["services.tf"]

    def test_no_declared_outputs_are_emitted(self, tmp_path):
        files = _emit(tmp_path, services=_plain())
        for name in ("security_groups", "subnets", "vpc_endpoints", "repositories"):
            assert f'output "{name}"' not in files["outputs.tf"]


class TestSecurityGroupEmission:
    def test_declared_group_has_no_inline_rules(self, tmp_path):
        """Inline blocks would re-add AWS's allow-all egress and cannot be
        mixed with rule resources. Default-deny depends on their absence."""
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        block = tf.split('resource "aws_security_group" "rc_runners"')[1].split(
            "\nresource "
        )[0]
        assert "ingress {" not in block
        assert "egress {" not in block

    def test_rules_become_separate_resources(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert 'resource "aws_vpc_security_group_egress_rule"' in tf
        assert 'resource "aws_vpc_security_group_ingress_rule"' in tf

    def test_cidr_reference_resolves_to_cidr_ipv4(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert 'cidr_ipv4         = "0.0.0.0/0"' in tf
        assert 'ip_protocol       = "udp"' in tf

    def test_sg_reference_resolves_to_the_declared_group(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert "referenced_security_group_id = aws_security_group.rc_runners.id" in tf

    def test_alb_reference_resolves_to_the_built_in_alb_group(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert "referenced_security_group_id = aws_security_group.alb.id" in tf

    def test_no_implicit_alb_or_wide_egress_is_injected(self, tmp_path):
        """The whole point: a declared group gets what it asked for, nothing more."""
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        runner_rules = [
            line
            for line in tf.splitlines()
            if "rc_runners_eg_" in line or "rc_runners_in_" in line
        ]
        # 5 declared egress rules; zero ingress rules were declared, so zero
        # exist — no ALB rule appeared out of nowhere.
        assert sum(1 for r in runner_rules if "_eg_" in r) == 5
        assert sum(1 for r in runner_rules if "_in_" in r) == 0

    def test_rule_names_are_content_addressed_not_positional(self, tmp_path):
        """Inserting a rule must not rename every rule after it."""
        import copy

        before = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        mutated = copy.deepcopy(NETWORK)
        mutated["security_groups"]["runners"]["egress"].insert(
            0, {"to": "cidr:10.1.0.0/16", "ports": [443]}
        )
        after = _emit(tmp_path / "b", network=mutated)["network_declared.tf"]
        original_names = {
            line.split('"')[3]
            for line in before.splitlines()
            if "rc_runners_eg_" in line
        }
        new_names = {
            line.split('"')[3]
            for line in after.splitlines()
            if "rc_runners_eg_" in line
        }
        assert original_names < new_names  # strict subset: all survived, one added


class TestDerivedEndpointIngress:
    def test_egress_to_an_endpoint_grants_the_reverse_ingress(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert 'resource "aws_security_group" "rc_ecr_vpce"' in tf
        assert "derived from 'rc_runners' egress to endpoint:ecr" in tf

    def test_derived_ingress_defaults_to_443(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        derived = tf.split('"rc_ecr_vpce_in_')[1].split("}")[0]
        assert "from_port" in derived and "= 443" in derived
        assert derived.count("= 443") == 2  # from_port and to_port

    def test_gateway_endpoint_egress_uses_a_prefix_list(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert "prefix_list_id    = aws_vpc_endpoint.rc_s3_s3.prefix_list_id" in tf

    def test_gateway_endpoint_gets_no_security_group(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert 'resource "aws_security_group" "rc_s3_vpce"' not in tf


class TestSubnetEmission:
    def test_allocates_cidrs_above_the_builtin_range(self, tmp_path):
        import re

        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        indices = [
            int(m) for m in re.findall(r"cidrsubnet\(var\.vpc_cidr, 8, (\d+)\)", tf)
        ]
        assert indices, "no auto-allocated CIDRs emitted"
        # 0-1 (built-in public) and 10-11 (built-in private) are rc's.
        assert all(i >= 20 for i in indices)
        assert not ({0, 1, 10, 11} & set(indices))
        # Contiguous within the group.
        assert indices == list(range(min(indices), min(indices) + len(indices)))

    def test_endpoints_mode_emits_no_default_route(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert 'resource "aws_route" "rc_runners_private_default"' not in tf
        assert 'resource "aws_nat_gateway"' not in tf

    def test_nat_mode_emits_one_shared_gateway(self, tmp_path):
        net = {
            "security_groups": {"a": {"egress": [{"to": "cidr:0.0.0.0/0"}]}},
            "subnets": {"p": {"egress": "nat"}},
        }
        tf = _emit(
            tmp_path,
            network=net,
            services=_plain(),
        )["network_declared.tf"]
        assert 'resource "aws_nat_gateway" "rc_nat"' in tf
        assert "nat_gateway_id         = aws_nat_gateway.rc_nat.id" in tf
        assert "subnet_id     = aws_subnet.public[0].id" in tf

    def test_colliding_explicit_offsets_are_rejected(self, tmp_path):
        net = {
            "security_groups": {"a": {"egress": [{"to": "cidr:0.0.0.0/0"}]}},
            "subnets": {
                "x": {"cidr_offset": 30, "count": 2},
                "y": {"cidr_offset": 31, "count": 2},
            },
        }
        with pytest.raises(ProviderConfigError, match="overlaps"):
            _emit(
                tmp_path,
                network=net,
                services=_plain(),
            )


class TestServicePlacement:
    def test_declared_groups_replace_the_shared_group(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["services.tf"]
        worker = tf.split('resource "aws_ecs_service" "worker"')[1].split(
            "\nresource "
        )[0]
        assert "security_groups  = [aws_security_group.rc_runners.id]" in worker
        assert "aws_security_group.tasks.id" not in worker

    def test_private_placement_disables_the_public_ip(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["services.tf"]
        worker = tf.split('resource "aws_ecs_service" "worker"')[1].split(
            "\nresource "
        )[0]
        assert "subnets          = aws_subnet.rc_runners_private[*].id" in worker
        assert "assign_public_ip = false" in worker

    def test_unplaced_services_are_unaffected(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["services.tf"]
        api = tf.split('resource "aws_ecs_service" "api"')[1].split("\nresource ")[0]
        assert "subnets          = aws_subnet.public[*].id" in api
        assert "security_groups  = [aws_security_group.tasks.id]" in api
        assert "assign_public_ip = true" in api


class TestReachabilityGuard:
    """A task in a NAT-free subnet that cannot reach ECR fails minutes into a
    rollout with an opaque CannotPullContainerError. Refuse to emit it."""

    def _net(self, interface_services, *, with_s3=False):
        """Gateway and interface services cannot share one endpoint entry, so
        s3 is always declared separately."""
        endpoints = {"e": {"services": interface_services, "subnets": ["p"]}}
        egress = [{"to": "endpoint:e"}]
        if with_s3:
            endpoints["gw"] = {"services": ["s3"], "subnets": ["p"]}
            egress.append({"to": "endpoint:gw"})
        return {
            "security_groups": {"runners": {"egress": egress}},
            "subnets": {"p": {"egress": "endpoints"}},
            "endpoints": endpoints,
        }

    def _svc(self, **kw):
        return {
            "w": ServiceSpec(
                name="w",
                cpu=256,
                memory=512,
                image="x",
                subnet_group="p",
                **kw,
            )
        }

    def test_missing_endpoints_are_named(self, tmp_path):
        with pytest.raises(ProviderConfigError) as exc:
            _emit(
                tmp_path,
                network=self._net(["ecr.api", "ecr.dkr"]),
                services=self._svc(security_groups=["runners"]),
            )
        assert "logs" in str(exc.value) and "s3" in str(exc.value)
        assert "ecr.api" in str(exc.value)  # reported as already reachable

    def test_shared_group_in_a_natless_subnet_is_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="no VPC endpoint admits"):
            _emit(
                tmp_path,
                network=self._net(["ecr.api", "ecr.dkr", "logs"], with_s3=True),
                services=self._svc(),
            )

    def test_endpoint_in_another_subnet_group_does_not_count(self, tmp_path):
        net = self._net(["ecr.api", "ecr.dkr", "logs"], with_s3=True)
        net["subnets"]["other"] = {"egress": "none"}
        net["endpoints"]["e"]["subnets"] = ["other"]
        net["endpoints"]["gw"]["subnets"] = ["other"]
        with pytest.raises(ProviderConfigError, match="Reachable today: nothing"):
            _emit(
                tmp_path,
                network=net,
                services=self._svc(security_groups=["runners"]),
            )

    def test_secrets_require_a_secretsmanager_endpoint(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="secretsmanager"):
            _emit(
                tmp_path,
                network=self._net(["ecr.api", "ecr.dkr", "logs"], with_s3=True),
                services=self._svc(security_groups=["runners"]),
                secrets=[
                    SecretRef(
                        name="db",
                        source="aws_sm",
                        arn="arn:aws:secretsmanager:us-west-2:1:secret:db-x",
                    )
                ],
            )

    def test_a_complete_endpoint_set_passes(self, tmp_path):
        files = _emit(
            tmp_path,
            network=self._net(["ecr.api", "ecr.dkr", "logs"], with_s3=True),
            services=self._svc(security_groups=["runners"]),
        )
        assert 'resource "aws_subnet" "rc_p"' in files["network_declared.tf"]


class TestOutputs:
    def test_declared_ids_are_exported(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK, repositories=REPOSITORIES)["outputs.tf"]
        assert '"runners" = aws_security_group.rc_runners.id' in tf
        assert '"runners-private" = aws_subnet.rc_runners_private[*].id' in tf
        assert '"ecr.ecr.api" = aws_vpc_endpoint.rc_ecr_ecr_api.id' in tf
        assert (
            '"db-sidecar" = aws_ecr_repository.rc_repo_db_sidecar.repository_url' in tf
        )
        assert 'output "vpc_id"' in tf

    def test_egress_mode_is_reported_per_group(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["outputs.tf"]
        assert '"runners-private" = "endpoints"' in tf

    def test_endpoint_groups_are_reported_separately(self, tmp_path):
        """`security_groups` stays exactly what rc.yml declared; rc-synthesized
        endpoint groups get their own output."""
        tf = _emit(tmp_path, network=NETWORK)["outputs.tf"]
        declared = tf.split('output "security_groups"')[1].split("output ")[0]
        assert "rc_ecr_vpce" not in declared
        assert 'output "vpc_endpoint_security_groups"' in tf
        assert "aws_security_group.rc_ecr_vpce.id" in tf
        endpoint_map = tf.split('output "vpc_endpoint_security_groups"')[1]
        assert '"ecr-vpce"' in endpoint_map


class TestRepositoryEmission:
    def test_repo_is_created_with_the_project_prefix(self, tmp_path):
        tf = _emit(tmp_path, repositories=REPOSITORIES, services=_plain())[
            "repositories.tf"
        ]
        assert 'name                 = "${var.project}/db-sidecar"' in tf
        assert "# mirror of: postgres:16-alpine" in tf

    def test_lifecycle_policy_only_when_requested(self, tmp_path):
        tf = _emit(tmp_path, repositories=REPOSITORIES, services=_plain())[
            "repositories.tf"
        ]
        assert "aws_ecr_lifecycle_policy" not in tf
        tf2 = _emit(
            tmp_path / "b",
            repositories={"r": {"expire_untagged_days": 14}},
            services=_plain(),
        )["repositories.tf"]
        assert 'resource "aws_ecr_lifecycle_policy" "rc_repo_r"' in tf2

    def test_immutable_tags_are_honoured(self, tmp_path):
        tf = _emit(tmp_path, repositories={"r": {"mutable": False}}, services=_plain())[
            "repositories.tf"
        ]
        assert 'image_tag_mutability = "IMMUTABLE"' in tf


class TestDeterminism:
    def test_emission_is_byte_stable_across_runs(self, tmp_path):
        a = _emit(tmp_path / "a", network=NETWORK, repositories=REPOSITORIES)
        b = _emit(tmp_path / "b", network=NETWORK, repositories=REPOSITORIES)
        assert a == b


class TestExistingVpcAdoptMode:
    """Declared subnets inside a VPC rc does not own.

    rc knows neither the real CIDR range nor the internet gateway, so the two
    things that depend on them must be supplied explicitly rather than guessed.
    """

    def _ctx_adopt(self, tmp_path, network, pc_extra=None, services=None):
        pc = {
            "ecs": {
                "region": "us-west-2",
                "vpc_id": "vpc-123",
                "public_subnet_ids": ["subnet-a", "subnet-b"],
            }
        }
        pc["ecs"].update(pc_extra or {})
        ctx = _ctx(tmp_path, network=network, services=services or _plain())
        ctx.provider_config = pc
        return ctx

    def _emit_adopt(self, tmp_path, network, pc_extra=None, services=None):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            self._ctx_adopt(tmp_path, network, pc_extra, services), out
        )
        return {p.name: p.read_text() for p in out.iterdir() if p.is_file()}

    OPEN_SG = {"security_groups": {"a": {"egress": [{"to": "cidr:0.0.0.0/0"}]}}}

    def test_auto_cidr_allocation_is_refused(self, tmp_path):
        net = dict(self.OPEN_SG, subnets={"p": {}})
        with pytest.raises(ProviderConfigError, match="cannot carve a CIDR block"):
            self._emit_adopt(tmp_path, net)

    def test_explicit_cidrs_are_used_verbatim(self, tmp_path):
        net = dict(
            self.OPEN_SG,
            subnets={"p": {"cidrs": ["10.9.0.0/24", "10.9.1.0/24"]}},
        )
        tf = self._emit_adopt(tmp_path, net)["network_declared.tf"]
        assert 'element(["10.9.0.0/24", "10.9.1.0/24"], count.index)' in tf
        assert "vpc_id            = data.aws_vpc.main.id" in tf

    def test_public_group_without_a_known_igw_is_refused(self, tmp_path):
        net = dict(
            self.OPEN_SG,
            subnets={"p": {"public": True, "cidrs": ["10.9.0.0/24", "10.9.1.0/24"]}},
        )
        with pytest.raises(ProviderConfigError, match="internet_gateway_id"):
            self._emit_adopt(tmp_path, net)

    def test_declared_igw_is_used_for_the_default_route(self, tmp_path):
        net = dict(
            self.OPEN_SG,
            subnets={"p": {"public": True, "cidrs": ["10.9.0.0/24", "10.9.1.0/24"]}},
        )
        tf = self._emit_adopt(tmp_path, net, {"internet_gateway_id": "igw-0abc"})[
            "network_declared.tf"
        ]
        assert 'gateway_id             = "igw-0abc"' in tf

    def test_nat_lands_in_the_adopted_public_subnet(self, tmp_path):
        net = dict(
            self.OPEN_SG,
            subnets={"n": {"egress": "nat", "cidrs": ["10.9.2.0/24", "10.9.3.0/24"]}},
        )
        tf = self._emit_adopt(tmp_path, net)["network_declared.tf"]
        assert "subnet_id     = local.rc_public_subnet_ids[0]" in tf
        # aws_internet_gateway.main does not exist in adopt mode.
        assert "depends_on = [aws_internet_gateway.main]" not in tf


class TestBlockIndependence:
    """The two blocks render into separate files and must not leak into
    each other's."""

    def test_repositories_only_emits_no_network_file_content(self, tmp_path):
        files = _emit(tmp_path, repositories=REPOSITORIES, services=_plain())
        assert files["network_declared.tf"].strip() == ""
        assert "aws_ecr_repository" in files["repositories.tf"]

    def test_network_only_emits_no_repository_file_content(self, tmp_path):
        files = _emit(tmp_path, network=NETWORK)
        assert files["repositories.tf"].strip() == ""
        assert "aws_security_group" in files["network_declared.tf"]


class TestReviewRegressions:
    """One test per defect found reviewing this change before it merged.

    Each of these emitted terraform that failed at `terraform validate`, at
    `terraform apply`, or silently did the wrong thing.
    """

    def test_alb_ref_resolves_to_the_data_source_when_adopting_an_alb(self, tmp_path):
        """`aws_security_group.alb` does not exist in adopt-ALB mode, so a bare
        `alb` ref emitted a dangling reference -- while the public-service
        validator simultaneously insisted the rule be written."""
        out = tmp_path / "tf"
        ctx = _ctx(
            tmp_path,
            network={
                "security_groups": {"g": {"ingress": [{"from": "alb", "ports": [80]}]}}
            },
            services={
                "api": ServiceSpec(
                    name="api",
                    cpu=256,
                    memory=512,
                    image="x",
                    public=True,
                    port=80,
                    health_check_path="/",
                    domain="api.example.com",
                    security_groups=["g"],
                )
            },
        )
        ctx.provider_config = {
            "ecs": {
                "region": "us-west-2",
                "vpc_id": "vpc-1",
                "public_subnet_ids": ["subnet-a", "subnet-b"],
                "existing_alb": {
                    "arn": "arn:aws:elasticloadbalancing:us-west-2:111122223333"
                    ":loadbalancer/app/x/1",
                    "https_listener_arn": "arn:aws:elasticloadbalancing:us-west-2"
                    ":111122223333:listener/app/x/1/2",
                },
            }
        }
        ECSProvider().emit_terraform(ctx, out)
        tf = (out / "network_declared.tf").read_text()
        assert "aws_security_group.alb.id" not in tf
        # A set, so for_each -- not count with an index.
        assert "for_each                     = data.aws_lb.main.security_groups" in tf
        assert "referenced_security_group_id = each.value" in tf

    def test_alb_ref_still_uses_the_managed_group_normally(self, tmp_path):
        tf = _emit(tmp_path, network=NETWORK)["network_declared.tf"]
        assert "referenced_security_group_id = aws_security_group.alb.id" in tf
        assert "for_each" not in tf

    def test_explicit_cidrs_overlapping_an_auto_block_are_rejected(self, tmp_path):
        """The old check compared HCL expression STRINGS, so
        `cidrsubnet(var.vpc_cidr, 8, N)` never matched the literal it produces."""
        import re

        from remote_compose.provider.ecs.network_plan import (
            _cidrsubnet,
            build_network_plan,
        )

        from remote_compose.config._schema_parser import _parse_network

        net = _parse_network({"subnets": {"aaa": {"egress": "none"}}})
        net.validate()
        expr = (
            build_network_plan(net, {}, vpc_cidr="10.0.0.0/16")
            .subnet_groups[0]
            .cidr_exprs[0]
        )
        idx = int(re.search(r", (\d+)\)", expr).group(1))
        collide = [str(_cidrsubnet("10.0.0.0/16", 8, idx + i)) for i in range(2)]

        clash = _parse_network(
            {
                "subnets": {
                    "aaa": {"egress": "none"},
                    "bbb": {"egress": "none", "cidrs": collide},
                }
            }
        )
        clash.validate()
        with pytest.raises(ProviderConfigError, match="overlaps"):
            build_network_plan(clash, {}, vpc_cidr="10.0.0.0/16")

    def test_explicit_cidr_outside_the_vpc_is_rejected(self, tmp_path):
        net = {
            "security_groups": {"g": {}},
            "subnets": {
                "x": {"egress": "none", "count": 1, "cidrs": ["192.168.5.0/24"]}
            },
        }
        with pytest.raises(ProviderConfigError, match="outside the VPC range"):
            _emit(tmp_path, network=net, services=_plain())

    def test_explicit_cidr_colliding_with_a_builtin_subnet_is_rejected(self, tmp_path):
        # 10.0.10.0/24 is rc's built-in private subnet 0.
        net = {
            "security_groups": {"g": {}},
            "subnets": {"x": {"egress": "none", "count": 1, "cidrs": ["10.0.10.0/24"]}},
        }
        with pytest.raises(ProviderConfigError, match="rc built-in"):
            _emit(tmp_path, network=net, services=_plain())

    def test_subnet_cidrs_do_not_move_when_an_earlier_name_is_added(self):
        """cidr_block is ForceNew: renumbering a live group destroys and
        recreates subnets that have running task ENIs attached."""
        from remote_compose.config._schema_parser import _parse_network
        from remote_compose.provider.ecs.network_plan import build_network_plan

        def allocate(names):
            net = _parse_network({"subnets": {n: {"egress": "none"} for n in names}})
            net.validate()
            plan = build_network_plan(net, {}, vpc_cidr="10.0.0.0/16")
            return {v.name: v.cidr_exprs for v in plan.subnet_groups}

        before = allocate(["workers"])
        after = allocate(["analytics", "workers"])
        assert before["workers"] == after["workers"]

    def test_gateway_endpoint_grants_every_service_prefix_list(self, tmp_path):
        """A [s3, dynamodb] endpoint only granted services[0]'s prefix list,
        while the reachability check reported both as reachable."""
        net = {
            "security_groups": {
                "g": {"egress": [{"to": "endpoint:gw"}, {"to": "endpoint:i"}]}
            },
            "subnets": {"p": {"egress": "endpoints"}},
            "endpoints": {
                "gw": {"services": ["s3", "dynamodb"], "subnets": ["p"]},
                "i": {"services": ["ecr.api", "ecr.dkr", "logs"], "subnets": ["p"]},
            },
        }
        tf = _emit(
            tmp_path,
            network=net,
            services={
                "w": ServiceSpec(
                    name="w",
                    cpu=256,
                    memory=512,
                    image="x",
                    security_groups=["g"],
                    subnet_group="p",
                )
            },
        )["network_declared.tf"]
        assert "prefix_list_id    = aws_vpc_endpoint.rc_gw_s3.prefix_list_id" in tf
        assert (
            "prefix_list_id    = aws_vpc_endpoint.rc_gw_dynamodb.prefix_list_id" in tf
        )

    def test_egress_none_is_guarded_like_egress_endpoints(self, tmp_path):
        """Both emit an identical route table -- no default route -- so both
        have the same CannotPullContainerError failure."""
        net = {
            "security_groups": {"g": {}},
            "subnets": {"iso": {"egress": "none"}},
        }
        with pytest.raises(ProviderConfigError, match="egress: none"):
            _emit(
                tmp_path,
                network=net,
                services={
                    "w": ServiceSpec(
                        name="w",
                        cpu=256,
                        memory=512,
                        image="x",
                        security_groups=["g"],
                        subnet_group="iso",
                    )
                },
            )

    def test_endpoint_sg_colliding_with_a_declared_group_is_rejected(self, tmp_path):
        """A group named `<X>-vpce` and an interface endpoint named `<X>` both
        become the terraform address rc_X_vpce."""
        net = {
            "security_groups": {
                "g": {"egress": [{"to": "endpoint:ecr"}]},
                "ecr-vpce": {},
            },
            "subnets": {"p": {"egress": "none"}},
            "endpoints": {"ecr": {"services": ["ecr.api"], "subnets": ["p"]}},
        }
        with pytest.raises(ProviderConfigError, match="terraform address"):
            _emit(tmp_path, network=net, services=_plain())

    def test_endpoint_service_name_flattening_collision_is_rejected(self, tmp_path):
        """endpoint `a` serving `b.c` and endpoint `a-b` serving `c` both
        become aws_vpc_endpoint.rc_a_b_c."""
        net = {
            "security_groups": {
                "g": {"egress": [{"to": "endpoint:a"}, {"to": "endpoint:a-b"}]}
            },
            "subnets": {"p": {"egress": "none"}},
            "endpoints": {
                "a": {"services": ["b.c"], "subnets": ["p"]},
                "a-b": {"services": ["c"], "subnets": ["p"]},
            },
        }
        with pytest.raises(ProviderConfigError, match="terraform address"):
            _emit(tmp_path, network=net, services=_plain())

    @pytest.mark.parametrize("reserved", ["tasks", "alb", "efs"])
    def test_security_group_reusing_an_rc_name_is_rejected(self, tmp_path, reserved):
        """Collides on the AWS Name, not the terraform address, so validate
        passes and apply fails with InvalidGroup.Duplicate."""
        with pytest.raises(ProviderConfigError, match="rc already creates"):
            _emit(
                tmp_path,
                network={"security_groups": {reserved: {}}},
                services=_plain(),
            )

    def test_repository_reusing_the_buildcache_name_is_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="rc already creates"):
            _emit(tmp_path, repositories={"buildcache": {}}, services=_plain())

    def test_repository_colliding_with_a_service_repo_is_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="already gets the ECR"):
            _emit(tmp_path, repositories={"w": {}}, services=_plain())

    def test_icmp_emits_type_and_code_not_a_port_range(self, tmp_path):
        """AWS reads from_port/to_port as ICMP type/code, valid -1..255. The
        0/65535 that means 'all ports' for tcp is out of range, and terraform
        does not catch it at plan time."""
        net = {
            "security_groups": {
                "g": {"ingress": [{"from": "cidr:10.0.0.0/16", "protocol": "icmp"}]}
            }
        }
        tf = _emit(tmp_path, network=net, services=_plain())["network_declared.tf"]
        assert 'ip_protocol       = "icmp"' in tf
        assert "from_port         = -1" in tf
        assert "to_port           = -1" in tf
        assert "65535" not in tf
