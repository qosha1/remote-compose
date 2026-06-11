"""provider_config.ecs.ignore_task_definition_changes (rc-6o3 follow-up).

When set, every aws_ecs_task_definition gets
``lifecycle { ignore_changes = [container_definitions] }`` so terraform
stops fighting container defs owned out-of-band (adopted / --no-state
stacks whose secrets are reconciled + images force-rolled outside tf).
Default (unset) keeps terraform managing container defs.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, *, ignore_td: bool | None) -> DeployContext:
    ecs: dict = {
        "region": "us-east-2",
        "cluster": "myapp-prod",
        "vpc_cidr": "10.0.0.0/16",
    }
    if ignore_td is not None:
        ecs["ignore_task_definition_changes"] = ignore_td
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=[],
    )


def _services_tf(tmp_path: Path, *, ignore_td: bool | None) -> str:
    out = tmp_path / "terraform"
    ECSProvider().emit_terraform(_ctx(tmp_path, ignore_td=ignore_td), out)
    return (out / "services.tf").read_text()


def test_flag_on_emits_ignore_changes(tmp_path):
    tf = _services_tf(tmp_path, ignore_td=True)
    assert "lifecycle {" in tf
    assert "ignore_changes = [container_definitions]" in tf


def test_default_off_no_lifecycle_ignore(tmp_path):
    tf = _services_tf(tmp_path, ignore_td=None)
    assert "ignore_changes = [container_definitions]" not in tf


def test_explicit_false_no_lifecycle_ignore(tmp_path):
    tf = _services_tf(tmp_path, ignore_td=False)
    assert "ignore_changes = [container_definitions]" not in tf


def test_flag_on_service_ignores_task_definition(tmp_path):
    # The whole point of the flag: reconcile/force-roll own the task def
    # revision out-of-band. terraform must NOT revert the service's
    # task_definition pointer (which rolls the service back to a stale,
    # stripped-env revision) — so the service ALSO needs ignore_changes.
    tf = _services_tf(tmp_path, ignore_td=True)
    assert "ignore_changes = [task_definition]" in tf


def test_default_off_service_no_task_definition_ignore(tmp_path):
    tf = _services_tf(tmp_path, ignore_td=None)
    assert "ignore_changes = [task_definition]" not in tf
