"""rc-fg83: `rc run` could not launch a one-off task on an EC2 stack.

    InvalidParameterException: Task definition does not support
    launch_type FARGATE

Not a hardcode so much as a fallback that was wrong exactly when it fired.
`launchType` and `capacityProviderStrategy` are mutually exclusive on an ECS
service, and rc renders EC2 services with a capacity provider strategy — so
describe_services returns NO launchType for them, and
`svc.get("launchType") or "FARGATE"` sent FARGATE for a task definition whose
requiresCompatibilities is ["EC2"].

That took out the migrate-before-roll step, which is the whole reason that
ordering exists.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import resolve_run_launch

EC2_STRATEGY = [{"capacityProvider": "app-ec2-cp", "weight": 1, "base": 1}]


class TestResolveRunLaunch:
    def test_ec2_service_runs_on_its_own_capacity_provider(self):
        """The one-off lands on the same ASG the services use, and engages
        that provider's managed scaling — which a bare launchType=EC2
        would not."""
        assert resolve_run_launch(
            {"capacityProviderStrategy": EC2_STRATEGY},
            {"requiresCompatibilities": ["EC2"]},
        ) == {"capacityProviderStrategy": EC2_STRATEGY}

    def test_fargate_service_behaviour_is_unchanged(self):
        assert resolve_run_launch(
            {"launchType": "FARGATE"}, {"requiresCompatibilities": ["FARGATE"]}
        ) == {"launchType": "FARGATE"}

    def test_never_returns_both_keys(self):
        """RunTask rejects launchType alongside capacityProviderStrategy."""
        out = resolve_run_launch(
            {"capacityProviderStrategy": EC2_STRATEGY, "launchType": "EC2"},
            {"requiresCompatibilities": ["EC2"]},
        )
        assert set(out) == {"capacityProviderStrategy"}

    def test_falls_back_to_task_def_compatibilities(self):
        """No service hint at all — read what the task def actually allows."""
        assert resolve_run_launch({}, {"requiresCompatibilities": ["EC2"]}) == {
            "launchType": "EC2"
        }

    def test_task_def_declaring_both_prefers_fargate(self):
        assert resolve_run_launch(
            {}, {"requiresCompatibilities": ["EC2", "FARGATE"]}
        ) == {"launchType": "FARGATE"}

    def test_unknown_defers_to_the_cluster_default(self):
        """Better to let ECS apply the cluster's default capacity provider
        strategy than to guess a launch type it may reject."""
        assert resolve_run_launch({}, {}) == {}

    @pytest.mark.parametrize("compat", [["ec2"], ["Ec2"]])
    def test_compatibilities_are_case_insensitive(self, compat):
        assert resolve_run_launch({}, {"requiresCompatibilities": compat}) == {
            "launchType": "EC2"
        }


def _ctx() -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=Path("/tmp/docker-compose.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-2", "cluster": "myapp-prod"}},
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services={"django": ServiceSpec(name="django", cpu=512, memory=1024)},
    )


def _session(service_attrs, compatibilities):
    ecs = mock.MagicMock()
    ecs.describe_services.return_value = {
        "services": [
            {
                "taskDefinition": "arn:aws:ecs:us-west-2:111:task-definition/d:7",
                "networkConfiguration": {
                    "awsvpcConfiguration": {"subnets": ["subnet-a"]}
                },
                **service_attrs,
            }
        ]
    }
    ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [{"name": "django"}],
            "requiresCompatibilities": compatibilities,
        }
    }
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:111:task/myapp-prod/abc"}],
        "failures": [],
    }
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "lastStatus": "STOPPED",
                "stoppedReason": "done",
                "containers": [{"name": "django", "exitCode": 0}],
            }
        ]
    }
    session = mock.MagicMock()
    session.client.return_value = ecs
    return session, ecs


class TestRunOneOffEndToEnd:
    def test_ec2_stack_no_longer_sends_fargate(self):
        """The reported failure: this exact call raised
        InvalidParameterException in production."""
        session, ecs = _session({"capacityProviderStrategy": EC2_STRATEGY}, ["EC2"])
        provider = ECSProvider(session_factory=lambda _c: session)
        provider.run_one_off(
            _ctx(), "django", ["python", "manage.py", "migrate"], wait=False
        )
        kwargs = ecs.run_task.call_args.kwargs
        assert kwargs.get("launchType") is None
        assert kwargs["capacityProviderStrategy"] == EC2_STRATEGY

    def test_fargate_stack_is_byte_for_byte_unchanged(self):
        session, ecs = _session({"launchType": "FARGATE"}, ["FARGATE"])
        provider = ECSProvider(session_factory=lambda _c: session)
        provider.run_one_off(_ctx(), "django", ["echo", "hi"], wait=False)
        kwargs = ecs.run_task.call_args.kwargs
        assert kwargs["launchType"] == "FARGATE"
        assert "capacityProviderStrategy" not in kwargs

    def test_ec2_service_without_a_strategy_uses_the_task_def(self):
        """An adopted service may carry launchType=EC2 directly."""
        session, ecs = _session({"launchType": "EC2"}, ["EC2"])
        ECSProvider(session_factory=lambda _c: session).run_one_off(
            _ctx(), "django", ["echo"], wait=False
        )
        assert ecs.run_task.call_args.kwargs["launchType"] == "EC2"

    def test_service_with_neither_reads_requires_compatibilities(self):
        session, ecs = _session({}, ["EC2"])
        ECSProvider(session_factory=lambda _c: session).run_one_off(
            _ctx(), "django", ["echo"], wait=False
        )
        assert ecs.run_task.call_args.kwargs["launchType"] == "EC2"

    def test_network_config_and_overrides_still_carried(self):
        session, ecs = _session({"capacityProviderStrategy": EC2_STRATEGY}, ["EC2"])
        ECSProvider(session_factory=lambda _c: session).run_one_off(
            _ctx(), "django", ["python", "manage.py", "migrate"], wait=False
        )
        kwargs = ecs.run_task.call_args.kwargs
        assert kwargs["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == [
            "subnet-a"
        ]
        assert kwargs["overrides"]["containerOverrides"] == [
            {"name": "django", "command": ["python", "manage.py", "migrate"]}
        ]


class TestOneOffCapacityWarning:
    """The hazard that survives the fix: on Fargate a one-off always has
    somewhere to run; on EC2 it needs a free slot on an existing instance."""

    def _emit(self, tmp_path, services, trunking=None):
        provider = ECSProvider()
        ctx = DeployContext(
            project="app",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={
                "ecs": {
                    "region": "us-east-2",
                    "cluster": "c",
                    "vpc_cidr": "10.0.0.0/16",
                    "default_launch_type": "EC2",
                    "ec2_capacity": {
                        "instance_type": "m5.xlarge",
                        "desired": 2,
                        "max": 4,
                    },
                }
            },
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services=services,
        )
        ctx.eni_trunking = trunking
        provider.emit_terraform(ctx, tmp_path / "tf")
        return provider._warnings

    def test_warns_when_a_task_mode_hook_runs_on_ec2(self, tmp_path):
        warnings = self._emit(
            tmp_path,
            {
                "django": ServiceSpec(
                    name="django",
                    cpu=512,
                    memory=1024,
                    lifecycle={"migrate": {"mode": "task"}},
                )
            },
        )
        [w] = [x for x in warnings if "one-off" in x]
        assert "django.migrate" in w
        assert "PENDING" in w
        assert "mode: exec" in w

    def test_reports_the_actual_slack(self, tmp_path):
        warnings = self._emit(
            tmp_path,
            {
                "django": ServiceSpec(
                    name="django",
                    cpu=512,
                    memory=1024,
                    lifecycle={"migrate": {"mode": "task"}},
                )
            },
            trunking=True,
        )
        [w] = [x for x in warnings if "one-off" in x]
        # m5.xlarge trunked = 20 slots x desired 2 = 40, 1 task declared.
        assert "about 40 awsvpc task(s)" in w
        assert "39 slot(s)" in w

    def test_exec_mode_hooks_do_not_warn(self, tmp_path):
        """`mode: exec` runs inside an existing task — no new slot needed."""
        warnings = self._emit(
            tmp_path,
            {
                "django": ServiceSpec(
                    name="django",
                    cpu=512,
                    memory=1024,
                    lifecycle={"migrate": {"mode": "exec"}},
                )
            },
        )
        assert not [x for x in warnings if "one-off" in x]

    def test_fargate_service_with_a_task_hook_does_not_warn(self, tmp_path):
        warnings = self._emit(
            tmp_path,
            {
                "django": ServiceSpec(
                    name="django",
                    cpu=512,
                    memory=1024,
                    launch_type="FARGATE",
                    lifecycle={"migrate": {"mode": "task"}},
                ),
                "worker": ServiceSpec(name="worker", cpu=256, memory=512),
            },
        )
        assert not [x for x in warnings if "one-off" in x]

    def test_no_hooks_no_warning(self, tmp_path):
        warnings = self._emit(
            tmp_path,
            {"django": ServiceSpec(name="django", cpu=512, memory=1024)},
        )
        assert not [x for x in warnings if "one-off" in x]
