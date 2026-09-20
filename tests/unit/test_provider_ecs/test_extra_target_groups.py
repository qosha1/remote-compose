"""services.<svc>.extra_target_groups: registering with a target group rc
does not own.

rc creates exactly one ALB and emits exactly one ``load_balancer {}`` block per
public service. Any other load balancer in front of the same tasks — an
internal one, an NLB, another stack's — has a target group rc knows nothing
about, and attaching a service to it out-of-band does not survive: the
aws_ecs_service rc renders ignores changes only to [task_definition], so the
next `terraform apply` removes the extra attachment and the deploy still
reports success. That silence is the failure mode this key exists to remove.

Covered here: the block is emitted, it is emitted for PRIVATE services too
(the whole point of a second load balancer is often that it is internal),
several groups can be declared, the config is validated, and a config without
the key emits byte-identical terraform to before.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config.v2_schema import ConfigError, parse
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

pytestmark = pytest.mark.unit

TG_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:targetgroup/"
    "dbgtnl20260919/8f1ad3bbf0b5c4a1"
)
OTHER_TG_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:targetgroup/"
    "dbgtnl20260920/1a2b3c4d5e6f7a8b"
)


def _ctx(tmp_path: Path, services: dict[str, ServiceSpec]) -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "myapp-prod",
                "aws_profile": "default",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _services_tf(tmp_path: Path, services: dict[str, ServiceSpec]) -> str:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services), out)
    return (out / "services.tf").read_text()


def _service_block(hcl: str, name: str) -> str:
    m = re.search(
        rf'resource "aws_ecs_service" "{name}" \{{(.*?)^}}',
        hcl,
        re.DOTALL | re.MULTILINE,
    )
    assert m, f"no aws_ecs_service block for {name!r}"
    return m.group(1)


def _load_balancers(block: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for lb in re.findall(r"load_balancer \{(.*?)\n  \}", block, re.DOTALL):
        entry = {}
        for key in ("target_group_arn", "container_name", "container_port"):
            m = re.search(rf"{key}\s*=\s*(.+)", lb)
            if m:
                entry[key] = m.group(1).strip().strip('"')
        out.append(entry)
    return out


def test_public_service_keeps_its_own_target_group_and_gains_the_declared_one(tmp_path):
    hcl = _services_tf(
        tmp_path,
        {
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                public=True,
                port=80,
                extra_ports=[8443],
                extra_target_groups=[{"arn": TG_ARN, "container_port": 8443}],
            )
        },
    )
    lbs = _load_balancers(_service_block(hcl, "web"))
    assert len(lbs) == 2, lbs
    # rc's own target group still comes first, unchanged.
    assert lbs[0]["target_group_arn"] == "aws_lb_target_group.default.arn"
    assert lbs[0]["container_port"] == "80"
    assert lbs[1] == {
        "target_group_arn": TG_ARN,
        "container_name": "web",
        "container_port": "8443",
    }


def test_private_service_can_register_with_a_declared_target_group(tmp_path):
    """A service with public=false has no rc target group at all. It can still
    sit behind someone else's load balancer — an internal one, typically."""
    hcl = _services_tf(
        tmp_path,
        {
            "worker": ServiceSpec(
                name="worker",
                cpu=256,
                memory=512,
                type="worker",
                port=9000,
                extra_target_groups=[{"arn": TG_ARN, "container_port": 9000}],
            )
        },
    )
    block = _service_block(hcl, "worker")
    lbs = _load_balancers(block)
    assert lbs == [
        {
            "target_group_arn": TG_ARN,
            "container_name": "worker",
            "container_port": "9000",
        }
    ]
    # No ALB listener to wait for: the depends_on belongs to rc's own ALB path.
    assert "depends_on" not in block


def test_several_target_groups_are_all_emitted(tmp_path):
    hcl = _services_tf(
        tmp_path,
        {
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                public=True,
                port=80,
                extra_target_groups=[
                    {"arn": TG_ARN, "container_port": 8443},
                    {"arn": OTHER_TG_ARN, "container_port": 80},
                ],
            )
        },
    )
    arns = [lb["target_group_arn"] for lb in _load_balancers(_service_block(hcl, "web"))]
    assert arns == ["aws_lb_target_group.default.arn", TG_ARN, OTHER_TG_ARN]


def test_no_key_means_byte_identical_terraform(tmp_path):
    """Back-compat contract: a config that does not use the key must emit
    exactly what it emitted before."""
    spec = dict(name="web", cpu=256, memory=512, public=True, port=80)
    without = _services_tf(tmp_path / "a", {"web": ServiceSpec(**spec)})
    empty = _services_tf(
        tmp_path / "b", {"web": ServiceSpec(**spec, extra_target_groups=[])}
    )
    assert without == empty


# --------------------------------------------------------------------------
# config validation
# --------------------------------------------------------------------------


def _service(**kwargs):
    from remote_compose.config._schema_types import ServiceV2

    base = dict(name="web", cpu=256, memory=512)
    base.update(kwargs)
    return ServiceV2(**base)


def test_valid_entry_passes_validation():
    _service(extra_target_groups=[{"arn": TG_ARN, "container_port": 8443}]).validate()


@pytest.mark.parametrize(
    "entries,message",
    [
        ({"arn": TG_ARN}, "must be a list"),
        (["not-a-mapping"], "must be a mapping"),
        ([{"arn": TG_ARN}], "container_port must be a port number"),
        ([{"container_port": 8443}], "arn must be a target group ARN"),
        ([{"arn": "myapp-tg", "container_port": 8443}], "arn must be a target group ARN"),
        (
            [{"arn": "arn:aws:elasticloadbalancing:us-east-2:1:loadbalancer/app/x/y",
              "container_port": 8443}],
            "is not a target group ARN",
        ),
        ([{"arn": TG_ARN, "container_port": "8443"}], "container_port must be a port number"),
        ([{"arn": TG_ARN, "container_port": 0}], "container_port must be a port number"),
        ([{"arn": TG_ARN, "container_port": 8443, "protocol": "HTTP"}], "unknown key(s)"),
        (
            [{"arn": TG_ARN, "container_port": 8443}, {"arn": TG_ARN, "container_port": 80}],
            "declared more than once",
        ),
    ],
)
def test_invalid_entries_are_rejected(entries, message):
    with pytest.raises(ConfigError) as exc:
        _service(extra_target_groups=entries).validate()
    assert message in str(exc.value)


def test_parsed_from_an_rc_yml_mapping():
    """Straight through the v2 parser, so the key cannot be silently dropped
    between rc.yml and the dataclass — the failure mode that makes a stale
    RC_REF report success while the attachment never happens."""
    cfg = parse(
        {
            "version": 2,
            "project": "myapp",
            "compose_file": "docker-compose.yml",
            "provider": "ecs",
            "services": {
                "web": {
                    "cpu": 256,
                    "memory": 512,
                    "public": True,
                    "port": 80,
                    "extra_target_groups": [
                        {"arn": TG_ARN, "container_port": 8443},
                    ],
                }
            },
        }
    )
    assert cfg.services["web"].extra_target_groups == [
        {"arn": TG_ARN, "container_port": 8443}
    ]


def test_absent_key_defaults_to_empty():
    cfg = parse(
        {
            "version": 2,
            "project": "myapp",
            "compose_file": "docker-compose.yml",
            "provider": "ecs",
            "services": {"web": {"cpu": 256, "memory": 512}},
        }
    )
    assert cfg.services["web"].extra_target_groups == []
