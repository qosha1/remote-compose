"""ECS managed termination protection must be ENABLED on rc's EC2 capacity.

capacity.tf.j2 rendered managed_termination_protection = "DISABLED" and left
the ASG's protect_from_scale_in unset. With managed scaling ENABLED, ECS
lowers desired_capacity when CapacityProviderReservation drops below target,
and the ASG then picks WHICH instance to terminate by its own termination
policy -- not by "has no tasks". On startsimpli-prod (desired-1 postgres on
EFS, every service on 1-2 boxes) that can terminate the loaded box.

With ENABLED + protect_from_scale_in, every instance launches protected and
ECS removes protection only from an instance it has emptied, so scale-in can
only ever take an empty box. AWS rejects ENABLED unless the ASG protects new
instances, so both must render together. force_delete keeps `rc destroy`
from hanging on protected instances (the predrain already stopped their
tasks).
"""

from __future__ import annotations

import re
from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path) -> DeployContext:
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-2",
            "cluster": "c",
            "vpc_cidr": "10.0.0.0/16",
            "default_launch_type": "EC2",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={"django": ServiceSpec(name="django", cpu=512, memory=1024, replicas=1)},
    )


def _capacity_tf(tmp_path: Path) -> str:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path), out)
    return (out / "capacity.tf").read_text()


def _block(tf: str, header: str) -> str:
    start = tf.index(header)
    depth, i = 0, tf.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(tf[i], 0)
        if depth == 0:
            return tf[start:i + 1]
        i += 1


def test_capacity_provider_enables_managed_termination_protection(tmp_path):
    cp = _block(_capacity_tf(tmp_path), 'resource "aws_ecs_capacity_provider" "ec2"')
    assert re.search(r'managed_termination_protection\s*=\s*"ENABLED"', cp), cp


def test_asg_protects_new_instances_from_scale_in(tmp_path):
    asg = _block(_capacity_tf(tmp_path), 'resource "aws_autoscaling_group" "ec2"')
    assert re.search(r"protect_from_scale_in\s*=\s*true", asg), asg


def test_asg_force_deletes_so_destroy_does_not_hang_on_protected_instances(tmp_path):
    asg = _block(_capacity_tf(tmp_path), 'resource "aws_autoscaling_group" "ec2"')
    assert re.search(r"force_delete\s*=\s*true", asg), asg
