"""Per-service ``placement`` -> ECS ordered_placement_strategy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config._errors import ConfigError
from remote_compose.config._schema_parser import _parse_service
from remote_compose.config._task_group_types import TaskGroupV2, validate_task_groups
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

BINPACK = 'type  = "binpack"\n    field = "memory"'
SPREAD = 'type  = "spread"\n    field = "instanceId"'


def _ctx(tmp_path: Path, services: dict, groups=None) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {"region": "us-west-2", "cluster": "t", "vpc_cidr": "10.0.0.0/16"}
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        task_groups=groups or {},
        secrets=[],
    )


def _svc(name, placement=None, launch_type="EC2", **kw):
    return ServiceSpec(
        name=name,
        cpu=1024,
        memory=4096,
        type="worker",
        launch_type=launch_type,
        placement=placement,
        **kw,
    )


def _render(tmp_path, services, groups=None):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, groups), out)
    return (out / "services.tf").read_text()


def _service(tf: str, name: str) -> str:
    m = re.search(
        r'resource "aws_ecs_service" "%s" \{.*?\n\}\n' % re.escape(name), tf, re.S
    )
    assert m, name
    return m.group(0)


class TestRender:
    def test_default_renders_binpack(self, tmp_path):
        s = _service(_render(tmp_path, {"w": _svc("w")}), "w")
        assert BINPACK in s
        assert "instanceId" not in s

    def test_explicit_binpack_identical_to_default(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _render(tmp_path / "a", {"w": _svc("w")})
        b = _render(tmp_path / "b", {"w": _svc("w", "binpack")})
        assert a == b

    def test_spread_renders_instance_id(self, tmp_path):
        s = _service(_render(tmp_path, {"w": _svc("w", "spread")}), "w")
        assert SPREAD in s
        assert "binpack" not in s.split("ordered_placement_strategy", 1)[1].split("}")[0]
        assert s.count("ordered_placement_strategy") >= 1

    def test_spread_keeps_az_rebalancing_disabled(self, tmp_path):
        s = _service(_render(tmp_path, {"w": _svc("w", "spread")}), "w")
        assert s.count('availability_zone_rebalancing = "DISABLED"') == 1

    def test_binpack_and_spread_same_rebalancing(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _service(_render(tmp_path / "a", {"w": _svc("w")}), "w")
        b = _service(_render(tmp_path / "b", {"w": _svc("w", "spread")}), "w")
        line = 'availability_zone_rebalancing = "DISABLED"'
        assert a.count(line) == b.count(line) == 1

    def test_only_the_spread_service_changes(self, tmp_path):
        tf = _render(tmp_path, {"a": _svc("a", "spread"), "b": _svc("b")})
        assert SPREAD in _service(tf, "a")
        assert BINPACK in _service(tf, "b")


class TestValidation:
    @pytest.mark.parametrize("v", ["binpack", "spread", None])
    def test_schema_accepts(self, v):
        _parse_service("w", {"launch_type": "EC2", "placement": v}).validate()

    @pytest.mark.parametrize("v", ["random", "Spread", "", True, False, 1, ["spread"]])
    def test_schema_rejects(self, v):
        sv = _parse_service("lathe", {"launch_type": "EC2", "placement": v})
        with pytest.raises(ConfigError, match=r"'lathe'.*placement"):
            sv.validate()

    def test_default_is_unset(self):
        assert _parse_service("w", {}).placement is None

    @pytest.mark.parametrize("v", ["random", True, 3])
    def test_provider_rejects(self, tmp_path, v):
        with pytest.raises(ConfigError, match=r"'lathe'.*placement"):
            _render(tmp_path, {"lathe": _svc("lathe", v)})

    def test_fargate_with_spread_is_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match=r"'lathe'.*placement: spread.*EC2"):
            _render(tmp_path, {"lathe": _svc("lathe", "spread", launch_type="FARGATE")})

    def test_fargate_with_binpack_is_harmless(self, tmp_path):
        tf = _render(tmp_path, {"w": _svc("w", "binpack", launch_type="FARGATE")})
        assert "ordered_placement_strategy" not in tf


class TestGroups:
    def test_group_disagreement_is_config_error(self):
        groups = {"g": TaskGroupV2(name="g", services=["a", "b"])}
        specs = {"a": _svc("a", "spread"), "b": _svc("b")}
        with pytest.raises(ConfigError, match=r"placement"):
            validate_task_groups(groups, specs)
