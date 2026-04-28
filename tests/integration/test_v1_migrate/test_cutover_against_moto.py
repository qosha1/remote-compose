"""End-to-end ServicesCutoverPhase against a moto-stood-up ECS cluster.

Stand up a sandbox ECS cluster with v1-shaped task defs and services,
run ServicesCutoverPhase, verify each service rolls to a v2-shaped
revision (secrets[]=ARN, env keys colliding with secrets dropped,
image+mounts preserved).

This proves the boto3 cutover path works against the real AWS API
shape that moto simulates, not just unit-test stubs.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from remote_compose.v1_migrate.apply import ServicesCutoverPhase
from remote_compose.v1_migrate.discover import V1Stack, ResourceInventory
from remote_compose.v1_migrate.plan import build_plan


pytestmark = pytest.mark.integration


FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "v1_migrate"


def _v1_task_def_input(family: str, image: str) -> dict:
    """register_task_definition kwargs for a v1-shaped task def
    (envfile-injected secrets in environment)."""
    return {
        "family": family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "1024",
        "memory": "2048",
        "containerDefinitions": [{
            "name": family.split("-")[-1],
            "image": image,
            "essential": True,
            "environment": [
                {"name": "DEBUG", "value": "False"},
                # v1 envfile-injected (real secret leaked into env):
                {"name": "POSTGRES_PASSWORD", "value": "leaked-real-secret"},
                {"name": "SECRET_KEY", "value": "leaked-real-django-key"},
            ],
            "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
        }],
    }


@pytest.fixture
def sandbox_ecs():
    """Stand up a sandbox ECS cluster with 3 v1-shaped services in moto."""
    with mock_aws():
        sess = boto3.Session(region_name="us-west-2")
        ec2 = sess.client("ec2")
        vpc = ec2.create_vpc(CidrBlock="10.42.0.0/16")["Vpc"]
        ec2.create_tags(Resources=[vpc["VpcId"]], Tags=[
            {"Key": "remote-compose:cluster", "Value": "migrate-test-cluster"},
            {"Key": "remote-compose:managed", "Value": "true"},
        ])
        subnet = ec2.create_subnet(
            VpcId=vpc["VpcId"], CidrBlock="10.42.1.0/24",
            AvailabilityZone="us-west-2a",
        )["Subnet"]

        sg_pre = ec2.create_security_group(
            GroupName="task-sg", Description="x", VpcId=vpc["VpcId"],
        )
        ecs = sess.client("ecs")
        ecs.create_cluster(clusterName="migrate-test-cluster")

        # Pre-register v1-shaped task defs + create services.
        services = ["django", "postgres", "redis"]
        for s in services:
            family = f"migrate-test-{s}"
            ecs.register_task_definition(
                **_v1_task_def_input(family, f"placeholder/{s}:v1"),
            )
            ecs.create_service(
                cluster="migrate-test-cluster",
                serviceName=family,
                taskDefinition=family,
                desiredCount=1,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": [subnet["SubnetId"]],
                        "securityGroups": [sg_pre["GroupId"]],
                        "assignPublicIp": "ENABLED",
                    },
                },
            )

        # SM secrets (just enough to populate plan.secret_arn_map).
        sm = sess.client("secretsmanager")
        sm.create_secret(
            Name="migrate-test/POSTGRES_PASSWORD",
            SecretString="real-pg-pass",
        )
        sm.create_secret(
            Name="migrate-test/SECRET_KEY",
            SecretString="real-django-key",
        )

        # Stand up the rest of the resources discover() expects.
        elbv2 = sess.client("elbv2")
        sg = ec2.create_security_group(
            GroupName="x", Description="x", VpcId=vpc["VpcId"],
        )
        subnet_b = ec2.create_subnet(
            VpcId=vpc["VpcId"], CidrBlock="10.42.2.0/24",
            AvailabilityZone="us-west-2b",
        )["Subnet"]
        elbv2.create_load_balancer(
            Name="migrate-test-alb",
            Subnets=[subnet["SubnetId"], subnet_b["SubnetId"]],
            SecurityGroups=[sg["GroupId"]],
            Scheme="internet-facing",
            Type="application",
        )
        sess.client("acm").request_certificate(
            DomainName="api.migrate-test.example.com",
        )
        efs = sess.client("efs")
        fs = efs.create_file_system(CreationToken="x")
        efs.create_access_point(
            FileSystemId=fs["FileSystemId"],
            PosixUser={"Uid": 0, "Gid": 0},
            RootDirectory={"Path": "/migrate-test/postgres_data"},
        )

        # v1 rc.yml.
        rc_yml = Path(__file__).parent / "_sandbox.rc.yml"
        rc_yml.write_text(
            "cluster: migrate-test-cluster\n"
            "region: us-west-2\n"
            "aws_profile: default\n"
            "compose_file: docker-compose.ecs.yml\n"
            "project_name: migrate-test\n"
            "vpc_cidr: 10.42.0.0/16\n"
            "domain: api.migrate-test.example.com\n"
            "secrets:\n  - .envs/.production/.django\n"
            "services:\n"
            "  django:\n    cpu: 1024\n    memory: 2048\n    type: application\n"
            "  postgres:\n    cpu: 512\n    memory: 1024\n    type: infrastructure\n"
            "  redis:\n    cpu: 256\n    memory: 512\n    type: infrastructure\n"
        )

        try:
            yield {"session": sess, "rc_yml": rc_yml, "services": services}
        finally:
            rc_yml.unlink(missing_ok=True)


class TestCutoverAgainstMoto:
    def test_cutover_rolls_each_service_with_v2_secrets(self, sandbox_ecs):
        sess = sandbox_ecs["session"]
        ecs = sess.client("ecs")
        cluster = "migrate-test-cluster"

        # Discover + plan.
        from remote_compose.v1_migrate.discover import discover
        stack, inv = discover(
            rc_v1_yml_path=sandbox_ecs["rc_yml"],
            aws_session=sess,
        )
        plan = build_plan(stack, inv)

        # Sanity check: plan has both SM secrets.
        assert "POSTGRES_PASSWORD" in plan.secret_arn_map
        assert "SECRET_KEY" in plan.secret_arn_map

        # Pre-cutover: each task def has secrets leaked into environment.
        for s in sandbox_ecs["services"]:
            td = ecs.describe_task_definition(
                taskDefinition=f"migrate-test-{s}",
            )["taskDefinition"]
            env_names = {
                e["name"] for e in td["containerDefinitions"][0]
                .get("environment", [])
            }
            assert "POSTGRES_PASSWORD" in env_names
            secret_block = td["containerDefinitions"][0].get("secrets", [])
            assert secret_block == [], (
                f"v1 fixture should have no secrets[] block; got {secret_block}"
            )

        # Run the cutover.
        result = ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()
        assert result.ok, result.details

        # Post-cutover: each service points at a new revision; new revision
        # has secrets as ARN refs and no env collisions.
        for s in sandbox_ecs["services"]:
            svc_name = f"migrate-test-{s}"
            desc = ecs.describe_services(
                cluster=cluster, services=[svc_name],
            )
            current_td_arn = desc["services"][0]["taskDefinition"]
            assert current_td_arn.endswith(":2"), (
                f"expected revision 2, got {current_td_arn}"
            )
            td = ecs.describe_task_definition(
                taskDefinition=current_td_arn,
            )["taskDefinition"]
            c0 = td["containerDefinitions"][0]
            secret_names = {sec["name"] for sec in c0.get("secrets", [])}
            assert "POSTGRES_PASSWORD" in secret_names
            assert "SECRET_KEY" in secret_names
            for sec in c0["secrets"]:
                assert sec["valueFrom"].startswith(
                    "arn:aws:secretsmanager:"
                )
            # env collision dropped:
            env_names = {e["name"] for e in c0.get("environment", [])}
            assert "POSTGRES_PASSWORD" not in env_names
            assert "SECRET_KEY" not in env_names
            # non-collision preserved:
            assert "DEBUG" in env_names
            # image preserved:
            assert c0["image"] == f"placeholder/{s}:v1"

    def test_cutover_does_not_mutate_efs_or_alb_or_secrets(self, sandbox_ecs):
        # Tripwire: capture SM secret values + EFS file system id + ALB ARN
        # before cutover, assert they're untouched after.
        sess = sandbox_ecs["session"]
        ecs = sess.client("ecs")
        sm = sess.client("secretsmanager")
        efs = sess.client("efs")
        elbv2 = sess.client("elbv2")

        sm_pg_before = sm.get_secret_value(
            SecretId="migrate-test/POSTGRES_PASSWORD",
        )["SecretString"]
        fs_id_before = efs.describe_file_systems()["FileSystems"][0]["FileSystemId"]
        alb_arn_before = elbv2.describe_load_balancers()[
            "LoadBalancers"][0]["LoadBalancerArn"]

        from remote_compose.v1_migrate.discover import discover
        stack, inv = discover(
            rc_v1_yml_path=sandbox_ecs["rc_yml"],
            aws_session=sess,
        )
        plan = build_plan(stack, inv)
        ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()

        # All three resources exactly as before.
        assert sm.get_secret_value(
            SecretId="migrate-test/POSTGRES_PASSWORD",
        )["SecretString"] == sm_pg_before
        assert efs.describe_file_systems()[
            "FileSystems"][0]["FileSystemId"] == fs_id_before
        assert elbv2.describe_load_balancers()[
            "LoadBalancers"][0]["LoadBalancerArn"] == alb_arn_before
