"""Per-service ``stop_timeout`` -> ECS container ``stopTimeout`` (rc-wqau)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config._errors import ConfigError
from remote_compose.config._schema_parser import _parse_service
from remote_compose.config._task_group_types import TaskGroupV2
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict, groups=None, **ecs_cfg) -> DeployContext:
    cfg = {"region": "us-west-2", "cluster": "t", "vpc_cidr": "10.0.0.0/16"}
    cfg.update(ecs_cfg)
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        task_groups=groups or {},
        secrets=[],
    )


def _svc(name, stop_timeout=None, **kw):
    return ServiceSpec(
        name=name,
        cpu=1024,
        memory=4096,
        type="worker",
        launch_type="FARGATE",
        stop_timeout=stop_timeout,
        **kw,
    )


def _render(tmp_path, services, groups=None, **cfg):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, groups, **cfg), out)
    return (out / "services.tf").read_text()


def _task_def(tf: str, name: str) -> str:
    m = re.search(
        r'resource "aws_ecs_task_definition" "%s" \{.*?\n\}\n' % re.escape(name),
        tf,
        re.S,
    )
    assert m, name
    return m.group(0)


class TestRender:
    def test_set_renders_stop_timeout_on_container(self, tmp_path):
        td = _task_def(_render(tmp_path, {"lathe": _svc("lathe", 120)}), "lathe")
        assert "stopTimeout = 120" in td
        assert td.index("stopTimeout") > td.index("container_definitions")

    def test_unset_renders_nothing(self, tmp_path):
        tf = _render(tmp_path, {"lathe": _svc("lathe")})
        assert "stopTimeout" not in tf

    def test_changing_value_changes_task_definition(self, tmp_path):
        for d in ("a", "b", "c"):
            (tmp_path / d).mkdir()
        unset = _task_def(_render(tmp_path / "a", {"w": _svc("w")}), "w")
        t60 = _task_def(_render(tmp_path / "b", {"w": _svc("w", 60)}), "w")
        t120 = _task_def(_render(tmp_path / "c", {"w": _svc("w", 120)}), "w")
        assert len({unset, t60, t120}) == 3

    def test_ec2_launch_type_also_renders(self, tmp_path):
        s = _svc("w", 90)
        s.launch_type = "EC2"
        td = _task_def(_render(tmp_path, {"w": s}), "w")
        assert "stopTimeout = 90" in td


class TestGroups:
    def _groups(self):
        return {"g": TaskGroupV2(name="g", services=["a", "b"])}

    def test_group_renders_on_every_container(self, tmp_path):
        tf = _render(
            tmp_path,
            {"a": _svc("a", 100), "b": _svc("b", 100)},
            self._groups(),
        )
        assert _task_def(tf, "g").count("stopTimeout = 100") == 2

    def test_group_disagreement_is_config_error(self, tmp_path):
        from remote_compose.config._task_group_types import validate_task_groups

        specs = {"a": _svc("a", 100), "b": _svc("b", None)}
        with pytest.raises(ConfigError, match=r"stop_timeout"):
            validate_task_groups(self._groups(), specs)


class TestValidation:
    @pytest.mark.parametrize("v", [2, 30, 120])
    def test_accepts_range(self, v):
        _parse_service("w", {"stop_timeout": v}).validate()

    @pytest.mark.parametrize("v", [0, 1, -5, 121, 1000, "60", "abc", True, False, 30.5])
    def test_schema_rejects_bad_values(self, v):
        sv = _parse_service("lathe", {"stop_timeout": v})
        with pytest.raises(ConfigError, match=r"'lathe'.*stop_timeout.*2-120"):
            sv.validate()

    def test_default_is_unset(self):
        assert _parse_service("w", {}).stop_timeout is None

    @pytest.mark.parametrize("v", [0, 121, True, "60"])
    def test_provider_rejects_bad_values(self, tmp_path, v):
        with pytest.raises(ConfigError, match=r"'lathe'.*stop_timeout.*2-120"):
            _render(tmp_path, {"lathe": _svc("lathe", v)})
