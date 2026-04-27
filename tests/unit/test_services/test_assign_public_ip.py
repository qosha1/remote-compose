"""Tests for assignPublicIp inference on Fargate tasks (remote-compose-gbq).

Earlier code hardcoded ``assignPublicIp='ENABLED'`` for every Fargate
task launch — including those in private subnets where the public IP
is not just unnecessary but actively wrong. The fix:

1. ``run_task`` accepts ``assign_public_ip`` kwarg (None=infer).
2. ``_infer_assign_public_ip`` reads ``cluster.has_public_subnets``
   when present, else falls back to describe_subnets, else True.
3. ECSService model service_def respects ``cluster.has_public_subnets``
   when set, else defaults to ENABLED for backward compat.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from remote_compose.services.ecs_service import ECSService


@pytest.fixture
def svc():
    s = ECSService.__new__(ECSService)
    s._observers = []
    s.log_info = MagicMock()
    s.log_warning = MagicMock()
    s.log_error = MagicMock()
    s.notify_observers = MagicMock()
    s.default_region = "us-west-1"
    return s


class TestInferAssignPublicIp:
    def test_explicit_public_subnets_flag_true(self, svc):
        cluster = MagicMock(has_public_subnets=True)
        assert svc._infer_assign_public_ip(cluster) is True

    def test_explicit_public_subnets_flag_false(self, svc):
        cluster = MagicMock(has_public_subnets=False)
        assert svc._infer_assign_public_ip(cluster) is False

    def test_no_subnets_defaults_true(self, svc):
        # No has_public_subnets attr, no subnet_ids — fall through to
        # the safe-for-back-compat default.
        cluster = MagicMock(spec=["aws_region", "aws_credential", "name"])
        cluster.subnet_ids = []
        assert svc._infer_assign_public_ip(cluster) is True

    def test_describe_subnets_all_public_returns_true(self, svc):
        cluster = MagicMock(spec=[
            "aws_region", "aws_credential", "name", "subnet_ids",
        ])
        cluster.subnet_ids = ["subnet-1", "subnet-2"]
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {
            "Subnets": [
                {"SubnetId": "subnet-1", "MapPublicIpOnLaunch": True},
                {"SubnetId": "subnet-2", "MapPublicIpOnLaunch": True},
            ]
        }
        svc._get_ec2_client = MagicMock(return_value=ec2)
        assert svc._infer_assign_public_ip(cluster) is True

    def test_describe_subnets_any_private_returns_false(self, svc):
        cluster = MagicMock(spec=[
            "aws_region", "aws_credential", "name", "subnet_ids",
        ])
        cluster.subnet_ids = ["subnet-pub", "subnet-priv"]
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {
            "Subnets": [
                {"SubnetId": "subnet-pub", "MapPublicIpOnLaunch": True},
                {"SubnetId": "subnet-priv", "MapPublicIpOnLaunch": False},
            ]
        }
        svc._get_ec2_client = MagicMock(return_value=ec2)
        # Mixed-public-private means safer to set DISABLED. False signals
        # "not all public" — caller can override per-task with the kwarg.
        assert svc._infer_assign_public_ip(cluster) is False

    def test_describe_subnets_failure_falls_back_to_true(self, svc):
        cluster = MagicMock(spec=[
            "aws_region", "aws_credential", "name", "subnet_ids",
        ])
        cluster.subnet_ids = ["subnet-1"]
        ec2 = MagicMock()
        ec2.describe_subnets.side_effect = RuntimeError("AccessDenied")
        svc._get_ec2_client = MagicMock(return_value=ec2)
        # Conservative: default ENABLED so a missing IAM perm doesn't
        # break previously-working deploys (warning logged).
        assert svc._infer_assign_public_ip(cluster) is True
        svc.log_warning.assert_called()


class TestRunTaskRespectsAssignPublicIp:
    def test_explicit_kwarg_false_emits_disabled(self, svc):
        from remote_compose.models import ECSCluster
        cluster = MagicMock(
            launch_type=ECSCluster.LaunchType.FARGATE,
            aws_region="us-west-1",
            aws_credential=None,
            aws_cluster_arn="arn:cluster",
            aws_cluster_name="c1",
            subnet_ids=["subnet-1"],
            security_group_ids=["sg-1"],
            name="c1",
            has_public_subnets=True,  # would default ENABLED, but kwarg overrides
        )
        task_def = MagicMock(
            aws_task_definition_arn="arn:taskdef",
            full_arn="arn:taskdef",
        )
        ecs = MagicMock()
        ecs.run_task.return_value = {"failures": [], "tasks": []}
        svc._get_ecs_client = MagicMock(return_value=ecs)

        svc.run_task(cluster, task_def, assign_public_ip=False)

        kwargs = ecs.run_task.call_args.kwargs
        nc = kwargs["networkConfiguration"]["awsvpcConfiguration"]
        assert nc["assignPublicIp"] == "DISABLED"

    def test_default_None_infers_from_cluster_flag_disabled(self, svc):
        from remote_compose.models import ECSCluster
        cluster = MagicMock(
            launch_type=ECSCluster.LaunchType.FARGATE,
            aws_region="us-west-1",
            aws_credential=None,
            aws_cluster_arn="arn:cluster",
            aws_cluster_name="c1",
            subnet_ids=["subnet-priv"],
            security_group_ids=["sg-1"],
            name="c1",
            has_public_subnets=False,
        )
        task_def = MagicMock(
            aws_task_definition_arn="arn:taskdef",
            full_arn="arn:taskdef",
        )
        ecs = MagicMock()
        ecs.run_task.return_value = {"failures": [], "tasks": []}
        svc._get_ecs_client = MagicMock(return_value=ecs)

        svc.run_task(cluster, task_def)  # no explicit kwarg

        nc = ecs.run_task.call_args.kwargs[
            "networkConfiguration"
        ]["awsvpcConfiguration"]
        assert nc["assignPublicIp"] == "DISABLED"
