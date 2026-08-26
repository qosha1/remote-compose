"""Emitted terraform for multi-container task groups (rc-mvse / rc-m2sn / rc-8xvk).

The byte-identical group-of-one guard lives in ``test_golden.py`` — it renders
through the SAME loop as a group of N, so these tests exercise the grouped path
rather than a preserved single-service branch.

Numbers here are the epic's own measurements against ``foundry-tenants``
(033937118837, us-west-2, 2026-08-26): 5 tenants x 6 containers = 30 tasks,
which the ENI dimension sizes at 4x m6i.large while memory alone wants 2.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config.v2_schema import TaskGroupV2
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.autosize import (
    KNOWN_INSTANCE_SHAPES,
    EC2TaskDemand,
    auto_size,
)

pytestmark = pytest.mark.unit


def _svc(name: str, **kw) -> ServiceSpec:
    kw.setdefault("cpu", 256)
    kw.setdefault("memory", 512)
    kw.setdefault("type", "application")
    return ServiceSpec(name=name, **kw)


def _tenant_services() -> dict[str, ServiceSpec]:
    return {
        "nginx": _svc("nginx", memory=512, public=True, port=80, image="nginx:1"),
        "django": _svc("django", memory=2048, port=8000, image="django:1"),
        "frontend": _svc("frontend", memory=1024, port=3000, image="frontend:1"),
        "reingest": _svc("reingest", memory=512, image="reingest:1"),
        "postgres": _svc("postgres", memory=1024, port=5432, image="postgres:16"),
        "redis": _svc("redis", memory=512, port=6379, image="redis:7"),
    }


def _groups(**overrides) -> dict[str, TaskGroupV2]:
    groups = {
        "nginx": TaskGroupV2(
            name="nginx", services=["nginx", "django", "frontend", "reingest"]
        ),
        "postgres": TaskGroupV2(name="postgres", services=["postgres", "redis"]),
    }
    groups.update(overrides)
    return groups


def _ctx(tmp_path: Path, services, task_groups=None, **ecs_cfg) -> DeployContext:
    cfg = {"region": "us-west-2", "cluster": "t-cluster", "vpc_cidr": "10.0.0.0/16"}
    cfg.update(ecs_cfg)
    return DeployContext(
        project="tenant",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        task_groups=task_groups or {},
        secrets=[],
    )


def _emit(tmp_path: Path, services, task_groups=None, **ecs_cfg) -> Path:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, task_groups, **ecs_cfg), out)
    return out


def _task_def(services_tf: str, name: str) -> str:
    """The body of one ``aws_ecs_task_definition`` resource."""
    block = re.search(
        r'resource "aws_ecs_task_definition" "' + re.escape(name) + r'" \{(.*?)\n\}\n',
        services_tf,
        re.S,
    )
    assert block, f"no task definition named {name!r}"
    return block.group(1)


def _container_names(services_tf: str, name: str) -> list[str]:
    """Container names, in emitted order, from one task definition.

    Deliberately text-based rather than parsed: ``jsonencode([...])`` carries
    HCL object syntax with comments, and the thing under test IS the emitted
    text.
    """
    body = _task_def(services_tf, name)
    # greedy to the LAST "])" — container_definitions is the only jsonencode
    # in a task definition, and its payload contains nested brackets.
    payload = re.search(r"container_definitions = jsonencode\((\[.*\])\)", body, re.S)
    assert payload, f"no container_definitions in {name!r}"
    return re.findall(r'^    name      = "([^"]+)"$', payload.group(1), re.M)


def _container_block(services_tf: str, task: str, container: str) -> str:
    """One container's slice of a task definition's container_definitions."""
    body = _task_def(services_tf, task)
    chunks = re.split(r"\n  \}, \{\n", body)
    for chunk in chunks:
        if re.search(r'^    name      = "%s"$' % re.escape(container), chunk, re.M):
            return chunk
    raise AssertionError(f"container {container!r} not found in task {task!r}")


class TestOneTaskPerGroup:
    def test_one_task_definition_and_service_per_group(self, tmp_path):
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        assert tf.count('resource "aws_ecs_task_definition"') == 2
        assert tf.count('resource "aws_ecs_service"') == 2
        assert 'resource "aws_ecs_service" "nginx"' in tf
        assert 'resource "aws_ecs_service" "postgres"' in tf
        for gone in ("django", "frontend", "reingest", "redis"):
            assert f'resource "aws_ecs_service" "{gone}"' not in tf

    def test_members_become_containers_in_declared_order(self, tmp_path):
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        assert _container_names(tf, "nginx") == [
            "nginx",
            "django",
            "frontend",
            "reingest",
        ]
        assert _container_names(tf, "postgres") == ["postgres", "redis"]

    def test_each_container_keeps_its_own_image_and_ports(self, tmp_path):
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        django = _container_block(tf, "nginx", "django")
        frontend = _container_block(tf, "nginx", "frontend")
        assert 'image     = "django:1"' in django
        assert 'image     = "frontend:1"' in frontend
        assert "containerPort = 8000" in django
        assert "containerPort = 3000" in frontend

    def test_log_stream_prefix_stays_per_container(self, tmp_path):
        """`rc logs django` filters CloudWatch by stream prefix, so a grouped
        member must keep its own — otherwise per-service logs disappear."""
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        for name in ("nginx", "django", "frontend", "reingest"):
            block = _container_block(tf, "nginx", name)
            assert f'"awslogs-stream-prefix" = "{name}"' in block

    def test_task_memory_is_the_sum_of_members(self, tmp_path):
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        # nginx 512 + django 2048 + frontend 1024 + reingest 512
        assert re.search(r'"nginx" \{.*?memory\s+= "4096"', tf, re.S)
        assert re.search(r'"postgres" \{.*?memory\s+= "1536"', tf, re.S)

    def test_declared_group_memory_overrides_the_sum(self, tmp_path):
        groups = _groups(
            postgres=TaskGroupV2(
                name="postgres", services=["postgres", "redis"], memory=1024
            )
        )
        tf = (_emit(tmp_path, _tenant_services(), groups) / "services.tf").read_text()
        assert re.search(r'"postgres" \{.*?memory\s+= "1024"', tf, re.S)

    def test_ecr_repos_stay_per_service_not_per_group(self, tmp_path):
        """Images are built per service; only the roll TARGET becomes the group."""
        services = _tenant_services()
        for name, spec in services.items():
            spec.image = None
            # distinct contexts: services that SHARE a build identity share one
            # ECR repo (rc-44i), which would mask the per-service assertion.
            spec.build_context = f"./{name}"
        out = _emit(tmp_path, services, _groups())
        tf = (out / "services.tf").read_text()
        for name in services:
            assert f'resource "aws_ecr_repository" "{name}"' in tf


class TestEssential:
    def test_essential_defaults_to_true_for_every_container(self, tmp_path):
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        for name in ("nginx", "django", "frontend", "reingest"):
            assert "essential = true" in _container_block(tf, "nginx", name)

    def test_per_container_essential_is_rendered(self, tmp_path):
        services = _tenant_services()
        services["frontend"].essential = False
        services["reingest"].essential = False
        tf = (_emit(tmp_path, services, _groups()) / "services.tf").read_text()
        for name in ("nginx", "django"):
            assert "essential = true" in _container_block(tf, "nginx", name)
        for name in ("frontend", "reingest"):
            block = _container_block(tf, "nginx", name)
            assert "essential = false" in block
            # the false branch carries the warning about silent degradation
            assert "never restarts an individual" in block

    def test_all_non_essential_is_rejected(self, tmp_path):
        """AWS: 'All tasks must have at least one essential container.'"""
        services = _tenant_services()
        for name in ("nginx", "django", "frontend", "reingest"):
            services[name].essential = False
        with pytest.raises(ProviderConfigError, match="at least one essential"):
            _emit(tmp_path, services, _groups())


class TestServiceDiscovery:
    def test_one_cloud_map_record_per_group(self, tmp_path):
        sd = (
            _emit(tmp_path, _tenant_services(), _groups()) / "service_discovery.tf"
        ).read_text()
        assert sd.count('resource "aws_service_discovery_service"') == 2
        for retired in ("django", "frontend", "reingest", "redis"):
            assert f'resource "aws_service_discovery_service" "{retired}"' not in sd

    def test_retired_hostnames_are_named_in_the_emitted_comment(self, tmp_path):
        sd = (
            _emit(tmp_path, _tenant_services(), _groups()) / "service_discovery.tf"
        ).read_text()
        assert "RETIRED by this grouping: django, frontend, reingest" in sd
        assert "RETIRED by this grouping: redis" in sd

    def test_ungrouped_stack_still_registers_every_service(self, tmp_path):
        sd = (_emit(tmp_path, _tenant_services()) / "service_discovery.tf").read_text()
        assert sd.count('resource "aws_service_discovery_service"') == 6
        assert "RETIRED" not in sd


class TestAlb:
    def test_target_group_names_the_ingress_container(self, tmp_path):
        tf = (
            _emit(tmp_path, _tenant_services(), _groups()) / "services.tf"
        ).read_text()
        lb = re.search(r"load_balancer \{(.*?)\}", tf, re.S)
        assert lb, "expected a load_balancer block on the public group"
        assert 'container_name   = "nginx"' in lb.group(1)
        assert "container_port   = 80" in lb.group(1)

    def test_ingress_selects_among_several_public_members(self, tmp_path):
        services = _tenant_services()
        services["django"].public = True
        groups = _groups(
            nginx=TaskGroupV2(
                name="nginx",
                services=["nginx", "django", "frontend", "reingest"],
                ingress="nginx",
            )
        )
        tf = (_emit(tmp_path, services, groups) / "services.tf").read_text()
        lb = re.search(r"load_balancer \{(.*?)\}", tf, re.S)
        assert 'container_name   = "nginx"' in lb.group(1)

    def test_ambiguous_ingress_is_rejected(self, tmp_path):
        services = _tenant_services()
        services["django"].public = True
        with pytest.raises(ProviderConfigError, match="ingress"):
            _emit(tmp_path, services, _groups())


class TestVolumes:
    def test_group_carries_its_members_volume_blocks(self, tmp_path):
        services = _tenant_services()
        services["postgres"].volumes = [
            {"name": "pgdata", "mount": "/var/lib/postgresql/data"}
        ]
        # the EFS mount makes postgres stateful, so its groupmate has to be
        # too — a group is one ECS service and carries one rollout policy
        services["redis"].stateful = True
        tf = (_emit(tmp_path, services, _groups()) / "services.tf").read_text()
        pg = re.search(
            r'resource "aws_ecs_task_definition" "postgres" \{(.*?)\n\}\n', tf, re.S
        ).group(1)
        assert 'volume {\n    name = "pgdata"' in pg

    def test_two_members_claiming_one_volume_name_is_rejected(self, tmp_path):
        services = _tenant_services()
        services["postgres"].volumes = [{"name": "data", "mount": "/pg"}]
        services["redis"].volumes = [{"name": "data", "mount": "/redis"}]
        # both mount EFS, so both are stateful — the group is uniform and the
        # rejection below is about the volume NAME, not the rollout policy
        with pytest.raises(ProviderConfigError, match="both mount volume"):
            _emit(tmp_path, services, _groups())


class TestValidationReachesEmit:
    def test_port_collision_within_a_group_is_rejected(self, tmp_path):
        services = _tenant_services()
        services["frontend"].port = 8000
        with pytest.raises(ProviderConfigError, match="port 8000"):
            _emit(tmp_path, services, _groups())

    def test_mixed_launch_type_within_a_group_is_rejected(self, tmp_path):
        services = _tenant_services()
        services["django"].launch_type = "EC2"
        with pytest.raises(ProviderConfigError, match="launch_type"):
            _emit(tmp_path, services, _groups())

    def test_mixed_iam_role_within_a_group_is_rejected(self, tmp_path):
        """task_role_arn is TASK-level, so a group collapses per-service IAM.

        The role is DECLARED here so this fails on the grouping conflict rather
        than on the unrelated undeclared-role check.
        """
        services = _tenant_services()
        services["django"].iam_role = "django-role"
        out = tmp_path / "tf"
        ctx = _ctx(tmp_path, services, _groups())
        ctx.rc_yml_v2 = {"iam_roles": {"django-role": {}}}
        with pytest.raises(ProviderConfigError, match="iam_role"):
            ECSProvider().emit_terraform(ctx, out)

    def test_unknown_member_is_rejected(self, tmp_path):
        groups = _groups(ghost=TaskGroupV2(name="ghost", services=["nope"]))
        with pytest.raises(ProviderConfigError, match="nope"):
            _emit(tmp_path, _tenant_services(), groups)


class TestAsgSizedFromGroups:
    """rc-8xvk. auto_size counts one branch ENI per EC2TaskDemand, so feeding
    it GROUPS instead of services is what converts grouping into fewer boxes —
    the template change alone changes no instance count."""

    @staticmethod
    def _m6i(name: str):
        shape = KNOWN_INSTANCE_SHAPES[name]
        return [shape.with_trunking()]

    def test_thirty_single_container_tasks_need_four_m6i_large(self):
        """Reproduces the live fleet exactly, as measured 2026-08-26:
        11520 MiB across 30 tasks. Memory alone wants 2 boxes
        (ceil(11520*1.2/7817)); the ENI dimension wants ceil(30*1.2/10) = 4,
        and the larger wins."""
        demands = [
            EC2TaskDemand(name=f"svc{i}", cpu_units=0, memory_mib=384)
            for i in range(30)
        ]
        assert sum(d.memory_mib for d in demands) == 11_520
        assert auto_size(demands, ladder=self._m6i("m6i.large")).desired_size == 4

    def test_ten_grouped_tasks_fit_one_m6i_xlarge(self):
        """Same 11520 MiB, regrouped into 5 tenants x 2 tasks. The workload did
        not shrink — only the ENI count did."""
        demands = [
            EC2TaskDemand(name=f"group{i}", cpu_units=0, memory_mib=1152)
            for i in range(10)
        ]
        assert sum(d.memory_mib for d in demands) == 11_520
        assert auto_size(demands, ladder=self._m6i("m6i.xlarge")).desired_size == 1

    def test_resizing_the_instance_alone_buys_nothing(self):
        """The same 30 ungrouped tasks on a double-size shape still needs 2
        boxes — the same $/mo as 4x m6i.large. Topology is the lever, not the
        instance type."""
        demands = [
            EC2TaskDemand(name=f"svc{i}", cpu_units=0, memory_mib=384)
            for i in range(30)
        ]
        assert auto_size(demands, ladder=self._m6i("m6i.xlarge")).desired_size == 2

    def test_emitted_asg_shrinks_when_services_are_grouped(self, tmp_path):
        services = _tenant_services()
        for spec in services.values():
            spec.launch_type = "EC2"

        ungrouped = (_emit(tmp_path / "a", services) / "capacity.tf").read_text()
        grouped = (
            _emit(tmp_path / "b", services, _groups()) / "capacity.tf"
        ).read_text()

        def desired(tf: str) -> int:
            return int(re.search(r"desired_capacity\s+= (\d+)", tf).group(1))

        assert desired(grouped) <= desired(ungrouped)


class TestStatefulIsComputedNotDeclared:
    """`stateful` is DERIVED (_is_stateful_service fires on EFS volumes and on
    singleton-scheduler names, not only on the rc.yml flag), so uniformity has
    to be checked on the computed value. Comparing the raw flag lets a group of
    [stateless, EFS-backed] pass validation and then render whichever rollout
    policy the FIRST member happens to imply — two postgres containers against
    one access point if the stateless member sorts first."""

    @staticmethod
    def _services():
        return {
            "django": _svc("django", memory=2048, port=8000, image="django:1"),
            "postgres": _svc(
                "postgres",
                memory=1024,
                port=5432,
                image="postgres:16",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
        }

    def test_grouping_a_stateless_service_with_an_efs_one_is_rejected(self, tmp_path):
        groups = {"django": TaskGroupV2(name="django", services=["django", "postgres"])}
        with pytest.raises(ProviderConfigError, match="stateful"):
            _emit(tmp_path, self._services(), groups)

    def test_rejected_regardless_of_declared_member_order(self, tmp_path):
        """The bug is order-dependent rendering; the reject must not be."""
        groups = {"django": TaskGroupV2(name="django", services=["postgres", "django"])}
        with pytest.raises(ProviderConfigError, match="stateful"):
            _emit(tmp_path, self._services(), groups)

    def test_a_singleton_scheduler_cannot_hide_in_a_stateless_group(self, tmp_path):
        services = {
            "django": _svc("django", memory=2048, port=8000, image="django:1"),
            "celery-beat": _svc("celery-beat", memory=512, image="celery:1"),
        }
        groups = {
            "django": TaskGroupV2(name="django", services=["django", "celery-beat"])
        }
        with pytest.raises(ProviderConfigError, match="stateful"):
            _emit(tmp_path, services, groups)

    def test_a_uniformly_stateful_group_is_accepted_and_renders_stop_then_start(
        self, tmp_path
    ):
        services = self._services()
        services["django"].stateful = True
        groups = {"django": TaskGroupV2(name="django", services=["django", "postgres"])}
        tf = (_emit(tmp_path, services, groups) / "services.tf").read_text()
        assert "deployment_minimum_healthy_percent = 0" in tf
        assert "deployment_maximum_percent         = 100" in tf
        assert 'availability_zone_rebalancing = "DISABLED"' in tf


class TestLaunchTypeUniformityUsesTheResolvedValue:
    def test_explicit_ec2_beside_the_ec2_default_is_not_a_conflict(self, tmp_path):
        """Both members resolve to EC2; only one says so out loud. Comparing the
        raw field would reject a config that renders identically."""
        services = {
            "web": _svc("web", memory=512, public=True, port=80, image="web:1"),
            "worker": _svc("worker", memory=512, image="worker:1", launch_type="EC2"),
        }
        groups = {"web": TaskGroupV2(name="web", services=["web", "worker"])}
        out = _emit(tmp_path, services, groups, default_launch_type="EC2")
        assert 'requires_compatibilities = ["EC2"]' in (out / "services.tf").read_text()


class TestGroupedStackFailsHonestlyOnUnportedPaths:
    """emit_terraform renders one service per group, but _ecs_service_name and
    _force_new_deployments still map a MEMBER name onto a live service. Until
    rc-ib01.2 / rc-ib01.1 land, those paths must say so rather than exhaust the
    name-probe chain and surface a bare AWS ServiceNotFound."""

    def _ctx(self, tmp_path):
        return _ctx(tmp_path, _tenant_services(), _groups())

    def test_force_roll_refuses_a_grouped_stack(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="does not support task_groups"):
            ECSProvider()._force_new_deployments(self._ctx(tmp_path), ["django"])

    def test_the_error_names_the_declared_groups(self, tmp_path):
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider()._force_new_deployments(self._ctx(tmp_path), ["django"])
        msg = str(exc.value)
        assert "nginx = [nginx, django, frontend, reingest]" in msg
        assert "rc-ib01" in msg

    def test_exec_refuses_a_grouped_stack(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="rc exec"):
            ECSProvider().exec(self._ctx(tmp_path), "django", ["true"])

    def test_run_refuses_a_grouped_stack(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="rc run"):
            ECSProvider().run_one_off(self._ctx(tmp_path), "django", ["true"])

    def test_an_ungrouped_stack_is_unaffected(self, tmp_path):
        """The guard must be inert for every existing rc user."""
        from remote_compose.provider.ecs.provider import (
            _reject_grouped_service_lookup,
        )

        _reject_grouped_service_lookup(_ctx(tmp_path, _tenant_services()), "rc deploy")
