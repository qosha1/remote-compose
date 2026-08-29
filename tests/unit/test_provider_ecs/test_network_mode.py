"""rc-u122: ``provider_config.ecs.network_mode`` — awsvpc is not a law of physics.

services.tf.j2 hardcoded ``network_mode = "awsvpc"`` with no conditional and no
rc.yml key, so every task burned a branch ENI and the ENI dimension — not
memory, not CPU — sized the fleet. ``rc dev up`` never paid that, because it
runs docker compose on the box where every container shares the host ENI.

Modelled against a live 30-service estate (5 tenants), with real
deploymentConfiguration (55 tasks at the rolling-deploy peak, not 30) and real
docker-stats usage:

    awsvpc + inflated reservations   -> 4 x m6i.large   $280.32/mo
    honest memory only, awsvpc kept  -> 4 x m6i.large   $280.32/mo   NO CHANGE
    bridge, reservations untouched   -> 2 x m6i.large   $140.16/mo
    bridge + honest memory           -> 1 x m6i.large   $ 70.08/mo

The middle row is the point: while the mode is awsvpc, right-sizing memory
moves ZERO boxes. Actual memory in use across all 30 containers was 2,904 MiB —
four boxes for a 3 GB workload.

The trade is real and is the operator's to make, never rc's: bridge puts every
task on the host ENI under the host security group, so per-task security groups
are gone. That is why this is an explicit opt-in key with an awsvpc default and
a hard error (not a warning, not a silent drop) when a stack asks for bridge
while also declaring per-task network placement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider

# Bridge implies EC2. Fargate supports awsvpc and nothing else, so a
# {"network_mode": "bridge"} stack left on the default FARGATE launch type is
# one AWS rejects — these tests would then be asserting the render of a
# configuration that could never deploy, which is exactly the gap this feature
# is under scrutiny for. Pin the launch type here so every bridge case below
# describes a stack that could actually run.
BRIDGE = {"network_mode": "bridge", "default_launch_type": "EC2"}


def _ctx(tmp_path: Path, services, ecs_over=None, network=None) -> DeployContext:
    ecs: dict = {
        "region": "us-west-2",
        "cluster": "app-cluster",
        "vpc_cidr": "10.0.0.0/16",
    }
    ecs.update(ecs_over or {})
    rc: dict = {}
    if network is not None:
        rc["network"] = network
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc,
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _emit(tmp_path, services, ecs_over=None, name="tf", network=None):
    out = tmp_path / name
    ECSProvider().emit_terraform(_ctx(tmp_path, services, ecs_over, network), out)
    return out


def _services_tf(tmp_path, services, ecs_over=None, name="tf", network=None) -> str:
    return (
        _emit(tmp_path, services, ecs_over, name, network) / "services.tf"
    ).read_text()


def _web():
    return ServiceSpec(name="web", cpu=256, memory=512, port=80, public=True)


def _pair():
    # >1 service so service discovery is emitted at all
    return {
        "web": _web(),
        "api": ServiceSpec(name="api", cpu=256, memory=512, port=8000),
    }


class TestAwsvpcRemainsTheDefault:
    """Nobody's stack moves without an edit — the whole no-regression guard."""

    def test_no_key_still_renders_awsvpc(self, tmp_path):
        tf = _services_tf(tmp_path, {"web": _web()})
        assert 'network_mode             = "awsvpc"' in tf

    def test_no_key_still_renders_network_configuration(self, tmp_path):
        tf = _services_tf(tmp_path, {"web": _web()})
        assert "network_configuration {" in tf
        assert "subnets          =" in tf
        assert "security_groups  =" in tf

    def test_explicit_awsvpc_is_byte_identical_to_omitting_it(self, tmp_path):
        a = _services_tf(tmp_path, {"web": _web()}, None, "a")
        b = _services_tf(tmp_path, {"web": _web()}, {"network_mode": "awsvpc"}, "b")
        assert a == b


class TestBridge:
    def test_renders_bridge(self, tmp_path):
        tf = _services_tf(tmp_path, {"web": _web()}, BRIDGE)
        assert 'network_mode             = "bridge"' in tf
        assert '"awsvpc"' not in tf

    def test_omits_network_configuration(self, tmp_path):
        """ECS rejects networkConfiguration outright for non-awsvpc modes:
        'Network Configuration is not valid for the given networkMode'. Leaving
        the block in would make every bridge stack fail at apply."""
        tf = _services_tf(tmp_path, {"web": _web()}, BRIDGE)
        assert "network_configuration {" not in tf
        assert "assign_public_ip" not in tf

    def test_port_mappings_omit_host_port_for_dynamic_mapping(self, tmp_path):
        """A static hostPort would collide the moment two tenants both want
        :80 on one box — which is precisely the density this feature exists to
        get. Omitting it makes ECS assign an ephemeral port per task."""
        tf = _services_tf(tmp_path, {"web": _web()}, BRIDGE)
        assert "containerPort = 80" in tf
        assert "hostPort" not in tf

    def test_alb_target_group_targets_instances_not_ips(self, tmp_path):
        """target_type=ip needs a task ENI to point at. In bridge the task has
        no ENI of its own, so the target is the instance + its dynamic port."""
        alb = (_emit(tmp_path, {"web": _web()}, BRIDGE) / "alb.tf").read_text()
        assert 'target_type = "instance"' in alb
        assert 'target_type = "ip"' not in alb

    def test_service_discovery_uses_srv_not_a_records(self, tmp_path):
        """An A record carries no port, and in bridge the port is dynamic, so
        A records would resolve to a host with no way to reach the container."""
        sd = (_emit(tmp_path, _pair(), BRIDGE) / "service_discovery.tf").read_text()
        assert 'type = "SRV"' in sd
        assert 'type = "A"' not in sd


class TestValidation:
    def test_unknown_mode_is_rejected(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="network_mode"):
            _services_tf(tmp_path, {"web": _web()}, {"network_mode": "host"})

    def test_bridge_with_declared_security_groups_is_rejected(self, tmp_path):
        """Per-task security groups do not exist in bridge mode. Silently
        dropping them would remove an isolation boundary the author explicitly
        asked for, which is the one failure mode this feature must not have."""
        svc = ServiceSpec(
            name="web",
            cpu=256,
            memory=512,
            port=80,
            public=True,
            security_groups=["runners"],
        )
        net = {"security_groups": {"runners": {"egress": [{"to": "cidr:0.0.0.0/0"}]}}}
        with pytest.raises(ProviderConfigError, match="security_groups"):
            _services_tf(tmp_path, {"web": svc}, BRIDGE, network=net)


class TestSizing:
    """The half that actually buys the boxes. Rendering bridge but still
    sizing the ASG from a per-task ENI ceiling would emit a correct stack onto
    exactly the fleet the mode exists to shrink."""

    def test_bridge_drops_the_eni_dimension_from_the_shape(self):
        from remote_compose.provider.ecs.autosize import KNOWN_INSTANCE_SHAPES
        from remote_compose.provider.ecs.provider import ECSProvider

        m6i = KNOWN_INSTANCE_SHAPES["m6i.large"]
        assert m6i.with_trunking().task_eni_slots == 10  # the ceiling today

        awsvpc = ECSProvider._effective_shape(m6i, True, "awsvpc")
        bridge = ECSProvider._effective_shape(m6i, True, "bridge")
        assert awsvpc.task_eni_slots == 10
        assert bridge.task_eni_slots is None  # "not modeled" -> dimension skipped
        # memory/cpu are untouched: bridge changes the network, not the box
        assert bridge.memory_gib == m6i.memory_gib
        assert bridge.vcpu == m6i.vcpu

    def test_trunking_cannot_resurrect_a_ceiling_that_does_not_apply(self):
        from remote_compose.provider.ecs.autosize import KNOWN_INSTANCE_SHAPES

        m6i = KNOWN_INSTANCE_SHAPES["m6i.large"]
        assert m6i.without_task_enis().with_trunking().task_eni_slots is None

    def test_many_small_services_size_fewer_instances_under_bridge(self):
        """The measured shape of the estate that motivated rc-u122: 30 small
        EC2 services whose memory fits twice over, sized by ENI at 4 boxes."""
        from remote_compose.provider.ecs.autosize import (
            EC2TaskDemand,
            KNOWN_INSTANCE_SHAPES,
            auto_size,
        )
        from remote_compose.provider.ecs.provider import ECSProvider

        m6i = KNOWN_INSTANCE_SHAPES["m6i.large"]
        demands = [
            EC2TaskDemand(name=f"s{i}", cpu_units=0, memory_mib=384) for i in range(30)
        ]
        awsvpc = auto_size(
            demands, ladder=[ECSProvider._effective_shape(m6i, True, "awsvpc")]
        )
        bridge = auto_size(
            demands, ladder=[ECSProvider._effective_shape(m6i, True, "bridge")]
        )
        assert awsvpc.desired_size == 4  # 30 tasks / 10 ENI slots, +20% headroom
        assert bridge.desired_size < awsvpc.desired_size
        assert bridge.desired_size == 2  # 11,520 MiB of memory, nothing else


class TestWarningsStayHonestUnderBridge:
    def test_shared_root_volume_warns_harder_not_less(self, tmp_path):
        """The density bound used to come only from the ENI ceiling, so under
        bridge it went silent — exactly backwards. Removing the per-task ENI is
        what lets one box hold 30 tasks instead of 10, so the shared-disk
        hazard is at its WORST right where the old bound stopped existing."""
        from remote_compose.provider.ecs.provider import ECSProvider
        from remote_compose.provider.ecs.autosize import EC2TaskDemand

        p = ECSProvider()
        p._warnings = []
        resolved = {
            "instance_type": "m6i.large",
            "desired_size": 1,
            "root_volume_size": None,
        }
        demands = [
            EC2TaskDemand(name=f"s{i}", cpu_units=0, memory_mib=256) for i in range(30)
        ]
        p._warn_on_shared_root_volume(resolved, demands, True, "bridge")
        joined = " ".join(getattr(p, "_warnings", []) or [])
        assert "root_volume_size" in joined
        assert "30 bridge tasks" in joined


class TestComposesWithTaskGroups:
    """rc-ib01 (task_groups) and rc-u122 (network_mode) landed independently and
    both cut the ENI bill, by different means: grouping reduces task COUNT,
    bridge removes the ENI dimension outright. They are not alternatives and
    nothing stops a stack asking for both, so the combination has to emit a
    coherent task definition rather than one feature's half of one."""

    def _services(self):
        return {
            "nginx": ServiceSpec(
                name="nginx", cpu=256, memory=512, port=80, public=True, image="nginx:1"
            ),
            "django": ServiceSpec(
                name="django", cpu=256, memory=2048, port=8000, image="django:1"
            ),
            "postgres": ServiceSpec(
                name="postgres", cpu=256, memory=1024, port=5432, image="postgres:16"
            ),
            "redis": ServiceSpec(
                name="redis", cpu=256, memory=512, port=6379, image="redis:7"
            ),
        }

    def _groups(self):
        from remote_compose.config._task_group_types import TaskGroupV2

        return {
            "nginx": TaskGroupV2(name="nginx", services=["nginx", "django"]),
            "postgres": TaskGroupV2(name="postgres", services=["postgres", "redis"]),
        }

    def _emit_both(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            DeployContext(
                project="tenant",
                compose_path=tmp_path / "docker-compose.yml",
                rc_yml_v2={},
                provider_config={
                    "ecs": {
                        "region": "us-west-2",
                        "cluster": "t-cluster",
                        "vpc_cidr": "10.0.0.0/16",
                        "network_mode": "bridge",
                        # Bridge implies EC2 — see the BRIDGE constant above.
                        "default_launch_type": "EC2",
                    }
                },
                tf_backend_config={"type": "local"},
                working_dir=tmp_path,
                services=self._services(),
                task_groups=self._groups(),
                secrets=[],
            ),
            out,
        )
        return out

    def test_grouped_bridge_emits_one_bridge_task_def_per_group(self, tmp_path):
        tf = (self._emit_both(tmp_path) / "services.tf").read_text()
        assert tf.count('resource "aws_ecs_task_definition"') == 2
        assert tf.count('network_mode             = "bridge"') == 2
        assert '"awsvpc"' not in tf

    def test_grouped_bridge_still_omits_network_configuration(self, tmp_path):
        """The guard is on the stack mode, not the group, so a multi-container
        task must drop the block exactly like a single-container one."""
        tf = (self._emit_both(tmp_path) / "services.tf").read_text()
        assert "network_configuration {" not in tf

    def test_grouped_bridge_keeps_both_containers_in_each_task(self, tmp_path):
        """Neither feature may quietly drop the other's work."""
        tf = (self._emit_both(tmp_path) / "services.tf").read_text()
        for c in ("nginx", "django", "postgres", "redis"):
            assert f'name      = "{c}"' in tf


class TestBridgeRejectsFargate:
    """rc-u122 follow-up: Fargate supports awsvpc and nothing else.

    This is the easiest invalid stack to reach by accident, because the launch
    type defaults to FARGATE — adding a single `network_mode: bridge` line to
    an otherwise untouched rc.yml produces terraform AWS refuses. Caught at
    plan time rather than at RegisterTaskDefinition.
    """

    def _ctx(self, tmp_path, monkeypatch, ecs_extra):
        import yaml
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml

        (tmp_path / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: nginx:alpine\n"
        )
        rc = tmp_path / "rc.yml"
        rc.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "project": "bfg",
                    "compose_file": "docker-compose.yml",
                    "provider": "ecs",
                    "provider_config": {
                        "ecs": {
                            "region": "us-west-2",
                            "vpc_cidr": "10.0.0.0/16",
                            **ecs_extra,
                        }
                    },
                    "services": {
                        "web": {"cpu": 256, "memory": 512, "port": 80, "public": True}
                    },
                    "terraform": {"backend": {"type": "local"}},
                }
            )
        )
        monkeypatch.chdir(tmp_path)
        _v, raw, v2 = load_rc_yml(rc)
        return build_deploy_context(v2, raw, rc)

    def test_bridge_with_default_fargate_is_rejected(self, tmp_path, monkeypatch):
        import pytest

        from remote_compose.provider.base import ProviderConfigError
        from remote_compose.provider.ecs.provider import ECSProvider

        ctx = self._ctx(tmp_path, monkeypatch, {"network_mode": "bridge"})
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        assert "bridge" in str(exc.value) and "EC2 launch type" in str(exc.value)

    def test_bridge_with_ec2_is_accepted(self, tmp_path, monkeypatch):
        from remote_compose.provider.ecs.provider import ECSProvider

        ctx = self._ctx(
            tmp_path,
            monkeypatch,
            {"network_mode": "bridge", "default_launch_type": "EC2"},
        )
        ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        rendered = (tmp_path / "tf" / "services.tf").read_text()
        assert 'network_mode             = "bridge"' in rendered

    def test_awsvpc_with_fargate_is_untouched(self, tmp_path, monkeypatch):
        # The guard must not fire on the default configuration.
        from remote_compose.provider.ecs.provider import ECSProvider

        ctx = self._ctx(tmp_path, monkeypatch, {})
        ECSProvider().emit_terraform(ctx, tmp_path / "tf")
