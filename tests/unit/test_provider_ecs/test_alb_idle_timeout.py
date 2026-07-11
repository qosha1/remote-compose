"""provider_config.ecs.idle_timeout -> aws_lb.main idle_timeout (tracked, not drift).

AWS's ALB idle timeout defaults to 60s and silently drops long-lived connections
(WebSockets / SSE / streaming responses) at that boundary. Raising it on the live LB
out-of-band shows as PERPETUAL plan drift (every `plan` wants to revert it); declaring
it here writes the value into terraform so the LB is the tracked source of truth.
Default 60 == AWS default, so a stack that doesn't set it sees no change.
"""

from __future__ import annotations

import re
from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, *, idle_timeout: int | None) -> DeployContext:
    ecs: dict = {"region": "us-west-2", "cluster": "myapp-prod", "vpc_cidr": "10.0.0.0/16"}
    if idle_timeout is not None:
        ecs["idle_timeout"] = idle_timeout
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            # public service so the provider emits the ALB (aws_lb.main)
            "web": ServiceSpec(
                name="web", cpu=256, memory=512, type="proxy",
                public=True, port=80, health_check_path="/",
            ),
        },
        secrets=[],
    )


def _alb_tf(tmp_path: Path, *, idle_timeout: int | None) -> str:
    out = tmp_path / "terraform"
    ECSProvider().emit_terraform(_ctx(tmp_path, idle_timeout=idle_timeout), out)
    return (out / "alb.tf").read_text()


def test_declared_idle_timeout_is_emitted(tmp_path):
    assert re.search(r"idle_timeout\s*=\s*300", _alb_tf(tmp_path, idle_timeout=300))


def test_default_idle_timeout_is_aws_default_60(tmp_path):
    # Unset -> 60 == AWS default, so an existing stack shows no diff on first plan.
    assert re.search(r"idle_timeout\s*=\s*60", _alb_tf(tmp_path, idle_timeout=None))
