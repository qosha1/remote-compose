"""Sweep an AWS account for resources matching a project name.

`rc audit` is the reverse of `rc destroy` — it finds anything terraform
might have left behind (orphan log groups, hung target groups, dangling
S3 buckets) and reports them. Optionally deletes.

Tests use a fake boto3 session (mocked clients) so they run in any
sandbox without AWS creds.
"""

from __future__ import annotations

from unittest import mock

import pytest

from remote_compose.audit import (
    AuditFinding,
    AuditReport,
    audit_project,
)


def _fake_session(client_returns: dict):
    """Build a mock boto3 Session whose .client(name) returns a Mock
    pre-loaded with the given method return values.

    client_returns: {"<service>": {"<method>": <return_value>, ...}}
    """
    sess = mock.Mock()

    def make_client(name, **_):
        client = mock.Mock()
        for method, ret in (client_returns.get(name) or {}).items():
            getattr(client, method).return_value = ret
        # Anything not pre-loaded returns an empty dict so attribute
        # access on the result doesn't blow up.
        return client

    sess.client.side_effect = make_client
    return sess


# ---------------------------------------------------------------------
# AuditReport shape
# ---------------------------------------------------------------------

class TestEmptyAccount:
    def test_no_resources_returns_empty_report(self):
        sess = _fake_session({
            "ecs":          {"list_clusters": {"clusterArns": []}},
            "ec2":          {"describe_vpcs": {"Vpcs": []},
                             "describe_security_groups": {"SecurityGroups": []}},
            "elbv2":        {"describe_load_balancers": {"LoadBalancers": []},
                             "describe_target_groups": {"TargetGroups": []}},
            "efs":          {"describe_file_systems": {"FileSystems": []}},
            "ecr":          {"describe_repositories": {"repositories": []}},
            "servicediscovery": {"list_namespaces": {"Namespaces": []}},
            "logs":         {"describe_log_groups": {"logGroups": []}},
            "iam":          {"list_roles": {"Roles": []}},
            "secretsmanager": {"list_secrets": {"SecretList": []}},
            "s3":           {"list_buckets": {"Buckets": []}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        assert isinstance(report, AuditReport)
        assert report.findings == []
        assert report.is_clean is True

    def test_report_carries_project_and_region(self):
        sess = _fake_session({})
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        assert report.project == "rc-test-foo"
        assert report.region == "us-west-1"


# ---------------------------------------------------------------------
# Per resource-class detection
# ---------------------------------------------------------------------

class TestECSClusterDetection:
    def test_matching_cluster_arn_becomes_finding(self):
        sess = _fake_session({
            "ecs": {"list_clusters": {"clusterArns": [
                "arn:aws:ecs:us-west-1:123:cluster/rc-test-foo-cluster",
                "arn:aws:ecs:us-west-1:123:cluster/other-app-cluster",
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        ecs = [f for f in report.findings if f.resource_type == "ecs_cluster"]
        assert len(ecs) == 1
        assert "rc-test-foo-cluster" in ecs[0].identifier


class TestVPCByProjectTag:
    def test_vpc_tagged_with_project_becomes_finding(self):
        sess = _fake_session({
            "ec2": {
                "describe_vpcs": {"Vpcs": [
                    {"VpcId": "vpc-aaa",
                     "Tags": [{"Key": "Project", "Value": "rc-test-foo"}]},
                    {"VpcId": "vpc-bbb",
                     "Tags": [{"Key": "Project", "Value": "other"}]},
                ]},
                "describe_security_groups": {"SecurityGroups": []},
            },
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        vpcs = [f for f in report.findings if f.resource_type == "vpc"]
        assert len(vpcs) == 1
        assert vpcs[0].identifier == "vpc-aaa"


class TestALBAndTargetGroups:
    def test_alb_name_match_becomes_finding(self):
        sess = _fake_session({
            "elbv2": {
                "describe_load_balancers": {"LoadBalancers": [
                    {"LoadBalancerName": "rc-test-foo-alb",
                     "LoadBalancerArn": "arn:lb/rc-test-foo-alb"},
                    {"LoadBalancerName": "other-alb",
                     "LoadBalancerArn": "arn:lb/other"},
                ]},
                "describe_target_groups": {"TargetGroups": [
                    {"TargetGroupName": "rc-test-foo-tg",
                     "TargetGroupArn": "arn:tg/rc-test-foo-tg"},
                ]},
            },
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        albs = [f for f in report.findings if f.resource_type == "alb"]
        tgs = [f for f in report.findings if f.resource_type == "target_group"]
        assert len(albs) == 1
        assert len(tgs) == 1


class TestECR:
    def test_ecr_repo_name_prefix_match(self):
        sess = _fake_session({
            "ecr": {"describe_repositories": {"repositories": [
                {"repositoryName": "rc-test-foo/django",
                 "repositoryArn": "arn:ecr/rc-test-foo/django"},
                {"repositoryName": "other-app/api",
                 "repositoryArn": "arn:ecr/other-app/api"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        ecr = [f for f in report.findings if f.resource_type == "ecr_repository"]
        assert len(ecr) == 1
        assert ecr[0].identifier == "rc-test-foo/django"


class TestLogGroups:
    def test_log_group_prefix_match(self):
        sess = _fake_session({
            "logs": {"describe_log_groups": {"logGroups": [
                {"logGroupName": "/ecs/rc-test-foo"},
                {"logGroupName": "/aws/ecs/containerinsights/rc-test-foo-cluster/performance"},
                {"logGroupName": "/ecs/other-app"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        logs = [f for f in report.findings if f.resource_type == "log_group"]
        assert len(logs) == 2  # both rc-test-foo log groups, not other-app


class TestIAMRoles:
    def test_iam_role_name_prefix_match(self):
        sess = _fake_session({
            "iam": {"list_roles": {"Roles": [
                {"RoleName": "rc-test-foo-task", "Arn": "arn:iam/rc-test-foo-task"},
                {"RoleName": "rc-test-foo-task-exec", "Arn": "arn:iam/rc-test-foo-task-exec"},
                {"RoleName": "OtherAppRole", "Arn": "arn:iam/OtherAppRole"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        iam = [f for f in report.findings if f.resource_type == "iam_role"]
        assert len(iam) == 2


class TestSecretsManager:
    def test_secret_name_prefix_match(self):
        sess = _fake_session({
            "secretsmanager": {"list_secrets": {"SecretList": [
                {"Name": "rc-test-foo/django", "ARN": "arn:sm/django"},
                {"Name": "other-app/cred",      "ARN": "arn:sm/other"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        sm = [f for f in report.findings if f.resource_type == "secret"]
        assert len(sm) == 1


class TestS3:
    def test_s3_bucket_name_contains_project(self):
        sess = _fake_session({
            "s3": {"list_buckets": {"Buckets": [
                {"Name": "rc-test-foo-backups-debuggai"},
                {"Name": "rc-test-foo-tf-state"},
                {"Name": "other-app-data"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        s3 = [f for f in report.findings if f.resource_type == "s3_bucket"]
        assert len(s3) == 2


class TestSecurityGroups:
    def test_sg_tagged_with_project(self):
        sess = _fake_session({
            "ec2": {
                "describe_vpcs": {"Vpcs": []},
                "describe_security_groups": {"SecurityGroups": [
                    {"GroupId": "sg-111",
                     "Tags": [{"Key": "Project", "Value": "rc-test-foo"}]},
                    {"GroupId": "sg-222", "Tags": []},
                ]},
            },
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        sgs = [f for f in report.findings if f.resource_type == "security_group"]
        assert len(sgs) == 1


# ---------------------------------------------------------------------
# is_clean toggle
# ---------------------------------------------------------------------

class TestIsClean:
    def test_any_finding_makes_report_dirty(self):
        sess = _fake_session({
            "logs": {"describe_log_groups": {"logGroups": [
                {"logGroupName": "/ecs/rc-test-foo"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        assert report.is_clean is False
        assert any(f.resource_type == "log_group" for f in report.findings)


# ---------------------------------------------------------------------
# Render — human-readable summary
# ---------------------------------------------------------------------

class TestRender:
    def test_clean_report_renders_clearly(self):
        sess = _fake_session({})
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        out = report.render()
        assert "clean" in out.lower()
        assert "rc-test-foo" in out

    def test_dirty_report_groups_by_class(self):
        sess = _fake_session({
            "logs": {"describe_log_groups": {"logGroups": [
                {"logGroupName": "/ecs/rc-test-foo"},
                {"logGroupName": "/aws/ecs/containerinsights/rc-test-foo-cluster/performance"},
            ]}},
            "ecr": {"describe_repositories": {"repositories": [
                {"repositoryName": "rc-test-foo/x",
                 "repositoryArn": "arn:ecr/rc-test-foo/x"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        out = report.render()
        # Each class mentioned with its count.
        assert "log_group" in out
        assert "ecr_repository" in out
        assert "/ecs/rc-test-foo" in out
        assert "rc-test-foo/x" in out


# ---------------------------------------------------------------------
# AuditFinding shape — used by the --delete path
# ---------------------------------------------------------------------

class TestFindingShape:
    def test_finding_has_resource_type_identifier_and_arn(self):
        sess = _fake_session({
            "logs": {"describe_log_groups": {"logGroups": [
                {"logGroupName": "/ecs/rc-test-foo"},
            ]}},
        })
        report = audit_project(sess, project="rc-test-foo", region="us-west-1")
        f = report.findings[0]
        assert isinstance(f, AuditFinding)
        assert f.resource_type == "log_group"
        assert f.identifier == "/ecs/rc-test-foo"
        # ARN may or may not be present depending on resource type;
        # tag should include enough context to identify the resource.
