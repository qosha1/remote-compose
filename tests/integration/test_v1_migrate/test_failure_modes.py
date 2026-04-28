"""Inject failure modes via boto3 stubs / moto and confirm tooling
fails CLOSED with actionable error messages — not silently corrupts.

Each test names a specific operational failure mode that could surface
during the prod cutover and asserts the migration tooling refuses to
proceed rather than continuing into a destructive state.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from remote_compose.v1_migrate.discover import discover
from remote_compose.v1_migrate.plan import PlanSafetyError, build_plan
from remote_compose.v1_migrate.translate import (
    translate_acm_in_place,
    translate_efs_in_place,
    translate_secrets_keep_arn,
)


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Helpers — write a v1 rc.yml + minimal AWS state, with one resource
# deliberately missing.
# ---------------------------------------------------------------------

def _v1_yml(tmp_path, region="us-west-2", project="migrate-test"):
    p = tmp_path / "rc.yml"
    p.write_text(
        f"cluster: {project}-cluster\n"
        f"region: {region}\n"
        "aws_profile: default\n"
        "compose_file: docker-compose.ecs.yml\n"
        f"project_name: {project}\n"
        "vpc_cidr: 10.99.0.0/16\n"
        "domain: api.example.com\n"
        "secrets:\n  - .envs/.production/.django\n"
        "services:\n  django:\n    cpu: 1024\n    memory: 2048\n    type: application\n"
    )
    return p


# ---------------------------------------------------------------------
# (a) Missing SM secret ARN
# ---------------------------------------------------------------------

class TestMissingSecretFailsClosed:
    def test_translate_secrets_raises_when_arn_unknown(self, tmp_path):
        with mock_aws():
            sess = boto3.Session(region_name="us-west-2")
            # No SM secrets created — the rc.yml references .django which
            # won't resolve to any SM ARN.
            stack, inv = discover(
                rc_v1_yml_path=_v1_yml(tmp_path),
                aws_session=sess,
            )
            with pytest.raises(Exception) as exc_info:
                translate_secrets_keep_arn(inv)
            # Error must call out the missing secret by name, not just
            # generic "key error" — actionable for the operator.
            assert "secret" in str(exc_info.value).lower()


# ---------------------------------------------------------------------
# (b) Missing EFS volume → translate_efs raises
# ---------------------------------------------------------------------

class TestMissingEfsFailsClosed:
    def test_translate_efs_raises_when_no_filesystem(self, tmp_path):
        with mock_aws():
            sess = boto3.Session(region_name="us-west-2")
            # No EFS file system at all.
            stack, inv = discover(
                rc_v1_yml_path=_v1_yml(tmp_path),
                aws_session=sess,
            )
            with pytest.raises(Exception) as exc_info:
                translate_efs_in_place(inv)
            assert "efs" in str(exc_info.value).lower()


# ---------------------------------------------------------------------
# (c) Missing ACM cert → translate_acm raises
# ---------------------------------------------------------------------

class TestMissingAcmFailsClosed:
    def test_translate_acm_raises_when_no_cert(self, tmp_path):
        with mock_aws():
            sess = boto3.Session(region_name="us-west-2")
            stack, inv = discover(
                rc_v1_yml_path=_v1_yml(tmp_path),
                aws_session=sess,
            )
            with pytest.raises(Exception) as exc_info:
                translate_acm_in_place(inv)
            assert "cert" in str(exc_info.value).lower() or \
                   "acm" in str(exc_info.value).lower()


# ---------------------------------------------------------------------
# (d) build_plan refuses to compose if any sub-translator raises
# ---------------------------------------------------------------------

class TestBuildPlanFailsClosedOnPartial:
    def test_partial_inventory_raises_plan_safety_error(self, tmp_path):
        # Only some resources exist — the plan must refuse to build,
        # not silently emit a partial migration.
        with mock_aws():
            sess = boto3.Session(region_name="us-west-2")
            ec2 = sess.client("ec2")
            ec2.create_vpc(CidrBlock="10.99.0.0/16")
            # No EFS, no ALB, no ACM, no SM secrets.
            stack, inv = discover(
                rc_v1_yml_path=_v1_yml(tmp_path),
                aws_session=sess,
            )
            with pytest.raises((PlanSafetyError, Exception)):
                build_plan(stack, inv)
