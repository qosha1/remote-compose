"""services.<name>.cpu_shares: EC2-only container cpu weight (startsim-h2x8y.12).

startsimpli-prod runs on one m6i.large whose summed CPU is p95 2.08 vCPU
against 2.0. rc emits container cpu 0 for every container (startsim-u88y
dropped task-level cpu, and container cpu went with it), so under contention
celery-worker's batch bursts get the same weight as django serving requests.
cpu_shares renders a container-level cpu WEIGHT so web outranks batch, while
keeping the total under half a box so it never blocks placement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config._schema_parser import _parse_cpu_shares
from remote_compose.config._schema_types import ConfigError
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, services: dict, launch_type: str = "EC2") -> DeployContext:
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "c",
                "vpc_cidr": "10.0.0.0/16",
                "default_launch_type": launch_type,
                "ec2_capacity": {"instance_type": "m6i.large"},
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
    )


def _services_tf(tmp_path: Path, services: dict, launch_type: str = "EC2") -> str:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, launch_type), out)
    return (out / "services.tf").read_text()


def _spec(name, shares=None):
    return ServiceSpec(name=name, cpu=512, memory=512, replicas=1, cpu_shares=shares)


def test_unset_emits_no_container_cpu(tmp_path):
    tf = _services_tf(tmp_path, {"django": _spec("django")})
    assert not re.search(r"^\s+cpu\s+=\s+\d+\s*$", tf, re.M)


def test_shares_render_as_container_cpu_on_ec2(tmp_path):
    tf = _services_tf(
        tmp_path, {"django": _spec("django", 256), "worker": _spec("worker", 16)}
    )
    assert re.search(r"^\s+cpu\s+=\s+256\s*$", tf, re.M)
    assert re.search(r"^\s+cpu\s+=\s+16\s*$", tf, re.M)
    # task-level cpu stays omitted on EC2 (startsim-u88y)
    assert 'cpu                      = "' not in tf


def test_total_over_half_a_box_is_rejected(tmp_path):
    # m6i.large = 2048 units; budget 1024
    with pytest.raises(ProviderConfigError, match="cpu_shares total 1100"):
        _services_tf(tmp_path, {"a": _spec("a", 600), "b": _spec("b", 500)})


def test_fargate_rejects_cpu_shares(tmp_path):
    with pytest.raises(ProviderConfigError, match="EC2-only"):
        _services_tf(tmp_path, {"a": _spec("a", 64)}, launch_type="FARGATE")


@pytest.mark.parametrize("bad", [1, 0, 10241, "64", True, 3.5])
def test_parser_rejects_out_of_range(bad):
    with pytest.raises(ConfigError, match="cpu_shares"):
        _parse_cpu_shares("svc", bad)


def test_parser_accepts_none_and_ints():
    assert _parse_cpu_shares("svc", None) is None
    assert _parse_cpu_shares("svc", 2) == 2
    assert _parse_cpu_shares("svc", 10240) == 10240
