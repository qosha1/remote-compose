"""Unit tests for state_backend.adopt_imports (rc-6o3).

Covers:
  * parse_emitted_addresses reads resource blocks (not data blocks) from
    the emitted *.tf module.
  * build_import_plan resolves every supported resource type to the
    correct terraform import id — deterministic (name-derived) and
    discovered (boto3) — and reports not-live resources as skipped.

All AWS access goes through a fake boto3 Session, so no real calls and no
terraform are made.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# --------------------------------------------------------------------------
# Fake boto3 session
# --------------------------------------------------------------------------


class _ClientError(Exception):
    """Minimal boto3-ClientError lookalike (carries .response['Error'])."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kwargs):
        return list(self._pages)


class _FakeClient:
    def __init__(self, service: str):
        self.service = service

    # --- ec2 -----------------------------------------------------------
    def describe_security_groups(self, Filters):  # noqa: N803
        name = Filters[0]["Values"][0]
        mapping = {
            "ss-debuggai-alb": "sg-alb",
            "ss-debuggai-tasks": "sg-tasks",
        }
        if name not in mapping:
            return {"SecurityGroups": []}
        return {"SecurityGroups": [{"GroupId": mapping[name], "VpcId": "vpc-test"}]}

    # --- ecr -----------------------------------------------------------
    def describe_repositories(self, repositoryNames):  # noqa: N803
        repo = repositoryNames[0]
        if repo in ("ss-debuggai/django", "ss-debuggai/buildcache"):
            return {"repositories": [{"repositoryName": repo}]}
        raise _ClientError("RepositoryNotFoundException")

    def get_lifecycle_policy(self, repositoryName):  # noqa: N803
        if repositoryName == "ss-debuggai/buildcache":
            return {"repositoryName": repositoryName, "lifecyclePolicyText": "{}"}
        raise _ClientError("LifecyclePolicyNotFoundException")

    # --- ecs -----------------------------------------------------------
    def describe_task_definition(self, taskDefinition):  # noqa: N803
        if taskDefinition == "ss-debuggai-django":
            return {
                "taskDefinition": {
                    "family": taskDefinition,
                    "revision": 7,
                    "taskDefinitionArn": (
                        "arn:aws:ecs:us-west-2:111111111111:"
                        "task-definition/ss-debuggai-django:7"
                    ),
                }
            }
        raise _ClientError("ClientException")

    # --- elbv2 ---------------------------------------------------------
    def describe_load_balancers(self, Names):  # noqa: N803
        if Names == ["ss-debuggai-alb"]:
            return {"LoadBalancers": [{"LoadBalancerArn": "alb-arn"}]}
        raise _ClientError("LoadBalancerNotFound")

    def describe_listeners(self, LoadBalancerArn):  # noqa: N803
        return {
            "Listeners": [
                {"Port": 80, "ListenerArn": "l-http"},
                {"Port": 443, "ListenerArn": "l-https"},
            ]
        }

    def describe_rules(self, ListenerArn):  # noqa: N803
        return {
            "Rules": [
                {
                    "RuleArn": "rule-nginx",
                    "Conditions": [
                        {
                            "Field": "host-header",
                            "HostHeaderConfig": {"Values": ["api.example.com"]},
                        }
                    ],
                    "Actions": [{"TargetGroupArn": "tg-nginx"}],
                },
                {"RuleArn": "rule-default", "Conditions": [], "Actions": []},
            ]
        }

    # --- servicediscovery / route53 (paginated) ------------------------
    def get_paginator(self, op):
        if op == "list_namespaces":
            return _Paginator(
                [{"Namespaces": [{"Name": "ss-debuggai.local", "Id": "ns-1"}]}]
            )
        if op == "list_services":
            return _Paginator([{"Services": [{"Name": "django", "Id": "srv-django"}]}])
        if op == "list_hosted_zones":
            return _Paginator(
                [
                    {
                        "HostedZones": [
                            {
                                "Name": "example.com.",
                                "Id": "/hostedzone/ZONE1",
                                "Config": {"PrivateZone": False},
                            }
                        ]
                    }
                ]
            )
        raise AssertionError(f"unexpected paginator {op}")


class _FakeSession:
    def client(self, name, region_name=None):  # noqa: D401
        return _FakeClient(name)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# The emitted module — only the `resource`/`data` block headers matter to
# parse_emitted_addresses. nginx owns an ECR repo here that is NOT live, to
# exercise the skip path.
_EMITTED_TF = """
data "aws_route53_zone" "main" {
  name = "example.com"
}

resource "aws_ecs_cluster" "main" { name = var.cluster_name }
resource "aws_ecs_cluster_capacity_providers" "main" {}
resource "aws_cloudwatch_log_group" "tasks" {}
resource "aws_cloudwatch_log_group" "container_insights" {}
resource "aws_iam_role" "task" {}
resource "aws_iam_role" "task_execution" {}
resource "aws_iam_role_policy" "task_execute_command" {}
resource "aws_iam_role_policy_attachment" "task_execution" {}
resource "aws_ecr_repository" "buildcache" {}
resource "aws_ecr_lifecycle_policy" "buildcache" {}
resource "aws_ecr_repository" "django" {}
resource "aws_ecr_repository" "nginx" {}
resource "aws_security_group" "alb" {}
resource "aws_security_group" "tasks" {}
resource "aws_ecs_task_definition" "django" {}
resource "aws_ecs_service" "django" {}
resource "aws_ecs_service" "nginx" {}
resource "aws_ecs_service" "celery_worker" {}
resource "aws_lb" "main" {}
resource "aws_lb_listener" "http" {}
resource "aws_lb_listener" "https" {}
resource "aws_lb_target_group" "nginx" {}
resource "aws_lb_listener_rule" "nginx" {}
resource "aws_service_discovery_private_dns_namespace" "main" {}
resource "aws_service_discovery_service" "django" {}
resource "aws_route53_record" "app_1" {}
"""


def _write_stack(tmp_path: Path) -> Path:
    rc = {
        "version": 2,
        "project": "ss-debuggai",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {
            "ecs": {
                "region": "us-west-2",
                "cluster": "ss-debuggai-prod",
                "route53_zone": "example.com",
            }
        },
        "terraform": {"backend": {"type": "local"}},
        "services": {
            "django": {"cpu": 1024, "memory": 2048, "type": "application"},
            "nginx": {
                "cpu": 256,
                "memory": 512,
                "type": "proxy",
                "public": True,
                "port": 80,
                "default_target": True,
                "domain": "api.example.com",
            },
            "celery-worker": {"cpu": 1024, "memory": 2048, "type": "worker"},
        },
    }
    (tmp_path / "docker-compose.yml").write_text(
        yaml.safe_dump({"services": {"django": {"image": "busybox"}}})
    )
    (tmp_path / "main.tf").write_text(_EMITTED_TF)
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(rc, sort_keys=False))
    return p


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


class TestParseEmittedAddresses:
    def test_returns_resource_blocks_not_data_blocks(self, tmp_path):
        from remote_compose.state_backend.adopt_imports import (
            parse_emitted_addresses,
        )

        _write_stack(tmp_path)
        found = parse_emitted_addresses(tmp_path)
        addrs = {f"{t}.{n}" for t, n in found}

        assert "aws_ecs_cluster.main" in addrs
        assert "aws_lb_listener_rule.nginx" in addrs
        # data blocks are not importable → excluded.
        assert not any(t == "aws_route53_zone" for t, _ in found)


class TestBuildImportPlan:
    def _plan(self, tmp_path):
        from remote_compose.state_backend.adopt_imports import build_import_plan

        rc_path = _write_stack(tmp_path)
        return build_import_plan(rc_path, tmp_path, session=_FakeSession())

    def test_deterministic_ids(self, tmp_path):
        plan = self._plan(tmp_path)
        ids = dict(plan.imports)

        assert ids["aws_ecs_cluster.main"] == "ss-debuggai-prod"
        assert ids["aws_ecs_cluster_capacity_providers.main"] == "ss-debuggai-prod"
        assert ids["aws_cloudwatch_log_group.tasks"] == "/ecs/ss-debuggai"
        assert (
            ids["aws_cloudwatch_log_group.container_insights"]
            == "/aws/ecs/containerinsights/ss-debuggai-prod/performance"
        )
        assert ids["aws_iam_role.task"] == "ss-debuggai-task"
        assert ids["aws_iam_role.task_execution"] == "ss-debuggai-task-exec"
        assert (
            ids["aws_iam_role_policy.task_execute_command"]
            == "ss-debuggai-task:ss-debuggai-task-exec-cmd"
        )
        assert ids["aws_iam_role_policy_attachment.task_execution"] == (
            "ss-debuggai-task-exec/arn:aws:iam::aws:policy/"
            "service-role/AmazonECSTaskExecutionRolePolicy"
        )

    def test_ecs_service_ids_handle_hyphenated_names(self, tmp_path):
        ids = dict(self._plan(tmp_path).imports)
        assert ids["aws_ecs_service.django"] == "ss-debuggai-prod/django"
        # tf_name celery_worker → live service name celery-worker.
        assert ids["aws_ecs_service.celery_worker"] == "ss-debuggai-prod/celery-worker"

    def test_discovered_ids(self, tmp_path):
        ids = dict(self._plan(tmp_path).imports)

        assert ids["aws_security_group.alb"] == "sg-alb"
        assert ids["aws_security_group.tasks"] == "sg-tasks"
        assert ids["aws_ecr_repository.django"] == "ss-debuggai/django"
        assert ids["aws_ecr_repository.buildcache"] == "ss-debuggai/buildcache"
        assert ids["aws_ecr_lifecycle_policy.buildcache"] == "ss-debuggai/buildcache"
        assert ids["aws_ecs_task_definition.django"] == (
            "arn:aws:ecs:us-west-2:111111111111:task-definition/ss-debuggai-django:7"
        )
        assert ids["aws_lb.main"] == "alb-arn"
        assert ids["aws_lb_listener.http"] == "l-http"
        assert ids["aws_lb_listener.https"] == "l-https"
        assert ids["aws_lb_listener_rule.nginx"] == "rule-nginx"
        assert ids["aws_lb_target_group.nginx"] == "tg-nginx"
        # namespace imports as NAMESPACE_ID:VPC_ID (vpc derived from tasks SG
        # since this test stack sets no vpc_id in config).
        assert (
            ids["aws_service_discovery_private_dns_namespace.main"] == "ns-1:vpc-test"
        )
        assert ids["aws_service_discovery_service.django"] == "srv-django"
        assert ids["aws_route53_record.app_1"] == "ZONE1_api.example.com_A"

    def test_not_live_resource_is_skipped_not_imported(self, tmp_path):
        plan = self._plan(tmp_path)
        ids = dict(plan.imports)
        skipped = dict(plan.skipped)

        # nginx ECR repo is not live → skipped, not imported.
        assert "aws_ecr_repository.nginx" not in ids
        assert "aws_ecr_repository.nginx" in skipped

    def test_parents_import_before_children(self, tmp_path):
        addrs = [a for a, _ in self._plan(tmp_path).imports]
        # cluster before its capacity providers + services
        assert addrs.index("aws_ecs_cluster.main") < addrs.index(
            "aws_ecs_service.django"
        )
        # lb before listener before listener rule
        assert addrs.index("aws_lb.main") < addrs.index("aws_lb_listener.https")
        assert addrs.index("aws_lb_listener.https") < addrs.index(
            "aws_lb_listener_rule.nginx"
        )


class TestEc2CapacityResourcesSafelySkipped:
    """rc-wji.3: capacity.tf.j2's EC2 capacity resources (launch template,
    ASG, capacity provider, ec2_instances SG, ec2_instance IAM role/profile)
    have no dedicated resolver here. Confirms that's SAFE for the forcing
    case -- a --no-state stack newly declaring launch_type: EC2 on a service
    that never had EC2 capacity before -- not a gap that blocks adoption.
    An unrecognized resource TYPE (no dispatch entry) or an unrecognized
    local NAME on a type that IS dispatched (aws_security_group.ec2_instances
    vs. the known "alb"/"tasks") both land in ``skipped``, never ``imports``,
    and build_import_plan never raises for them. terraform creates them
    fresh on the next (non-`--no-state`) apply instead of rc adopt trying
    (and failing) to import a resource that was never live.
    """

    _EC2_CAPACITY_TF = """
resource "aws_ecs_cluster" "main" {}
resource "aws_launch_template" "ec2" {}
resource "aws_autoscaling_group" "ec2" {}
resource "aws_ecs_capacity_provider" "ec2" {}
resource "aws_security_group" "ec2_instances" {}
resource "aws_iam_role" "ec2_instance" {}
resource "aws_iam_role_policy_attachment" "ec2_instance_ecs" {}
resource "aws_iam_role_policy_attachment" "ec2_instance_ssm" {}
resource "aws_iam_instance_profile" "ec2_instance" {}
"""

    _EC2_CAPACITY_ADDRS = {
        "aws_launch_template.ec2",
        "aws_autoscaling_group.ec2",
        "aws_ecs_capacity_provider.ec2",
        "aws_security_group.ec2_instances",
        "aws_iam_role.ec2_instance",
        "aws_iam_role_policy_attachment.ec2_instance_ecs",
        "aws_iam_role_policy_attachment.ec2_instance_ssm",
        "aws_iam_instance_profile.ec2_instance",
    }

    def test_ec2_capacity_resources_skipped_not_errored(self, tmp_path):
        from remote_compose.state_backend.adopt_imports import build_import_plan

        (tmp_path / "docker-compose.yml").write_text(
            yaml.safe_dump({"services": {"worker": {"image": "busybox"}}})
        )
        (tmp_path / "main.tf").write_text(self._EC2_CAPACITY_TF)
        rc = {
            "version": 2,
            "project": "sentinal",
            "compose_file": "docker-compose.yml",
            "provider": "ecs",
            "provider_config": {
                "ecs": {"region": "us-west-2", "cluster": "sentinal-prod"}
            },
            "terraform": {"backend": {"type": "local"}},
            "services": {
                "worker": {
                    "cpu": 256,
                    "memory": 512,
                    "type": "worker",
                    "launch_type": "EC2",
                },
            },
        }
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text(yaml.safe_dump(rc, sort_keys=False))

        plan = build_import_plan(rc_path, tmp_path, session=_FakeSession())

        imported_addrs = {a for a, _ in plan.imports}
        skipped_addrs = {a for a, _ in plan.skipped}
        # None of these are live yet -- must be safely skipped, never
        # imported, and never raise.
        assert self._EC2_CAPACITY_ADDRS.isdisjoint(imported_addrs)
        assert self._EC2_CAPACITY_ADDRS <= skipped_addrs
