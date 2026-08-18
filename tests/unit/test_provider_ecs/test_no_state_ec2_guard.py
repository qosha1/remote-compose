"""--no-state deploy must surface launch_type: EC2, not silently ignore it.

`_deploy_no_state` never runs `emit_terraform`, so it can never create the
EC2 capacity provider / ASG / launch template a `launch_type: EC2` service
needs (those only exist in capacity.tf.j2) -- it only force-rolls whatever
ECS already has live. That's often correct (a service already running as EC2
via a prior terraform apply, Copilot, CloudFormation, ...), so this warns
rather than blocks: raising would break every already-working --no-state
deploy of a service that legitimately runs on EC2.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider


def _ctx(**services: ServiceSpec) -> DeployContext:
    return DeployContext(
        project="test-proj",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-1", "cluster": "test-proj-prod"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services=services,
        secrets=[],
        skip_terraform=True,
    )


def _ecr_session():
    ecr = MagicMock()
    ecr.get_paginator.return_value.paginate.return_value = [{"repositories": []}]
    session = MagicMock()
    session.client.return_value = ecr
    return session


def test_ec2_launch_type_warns_but_still_deploys():
    ctx = _ctx(
        worker=ServiceSpec(
            name="worker", cpu=512, memory=1024, type="worker", launch_type="EC2"
        ),
        web=ServiceSpec(name="web", cpu=512, memory=1024, type="application"),
    )
    provider = ECSProvider()

    with (
        patch.object(provider, "session_factory", lambda c: _ecr_session()),
        patch.object(provider, "_build_and_push_images", return_value=[]),
        patch.object(provider, "_force_new_deployments"),
    ):
        result = provider.deploy(ctx, None, None)

    assert any("launch_type: EC2" in w and "['worker']" in w for w in result.warnings)


def test_fargate_only_no_state_deploy_has_no_ec2_warning():
    ctx = _ctx(web=ServiceSpec(name="web", cpu=512, memory=1024, type="application"))
    provider = ECSProvider()

    with (
        patch.object(provider, "session_factory", lambda c: _ecr_session()),
        patch.object(provider, "_build_and_push_images", return_value=[]),
        patch.object(provider, "_force_new_deployments") as force_roll,
    ):
        result = provider.deploy(ctx, None, None)

    assert not any("launch_type: EC2" in w for w in result.warnings)
    force_roll.assert_not_called()  # nothing "pushed" -> nothing to roll
