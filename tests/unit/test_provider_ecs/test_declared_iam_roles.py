"""Terraform emission for the declared ``iam_roles:`` block.

Covers the planner (naming, policy-document rendering) and the rendered HCL,
including the guarantee that a config WITHOUT the block emits exactly what it
did before the feature existed: the shared ``aws_iam_role.task`` still exists
and is still every service's ``task_role_arn``.
"""

from __future__ import annotations

import json

import pytest

from remote_compose.config.v2_schema import ConfigError
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.iam_plan import build_iam_plan

pytestmark = pytest.mark.unit


IAM_ROLES = {
    "media-writer": {
        "description": "S3 media write for the web tier",
        "managed_policies": ["arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"],
        "statements": [
            {
                "sid": "WriteMedia",
                "actions": ["s3:PutObject", "s3:GetObject"],
                "resources": ["arn:aws:s3:::bmgr-media/*"],
                "condition": {"Bool": {"aws:SecureTransport": "true"}},
            }
        ],
        "tags": {"tier": "web"},
    },
    "locked-down": {},
}


def _ctx(tmp_path, *, iam_roles=None, services=None, ecs_cfg=None):
    rc = {
        "version": 2,
        "project": "bmgr",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
    }
    if iam_roles is not None:
        rc["iam_roles"] = iam_roles
    ecs = {"region": "us-west-2", "cluster": "bmgr", "vpc_cidr": "10.0.0.0/16"}
    ecs.update(ecs_cfg or {})
    return DeployContext(
        project="bmgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc,
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=(
            services
            if services is not None
            else {
                "web": ServiceSpec(
                    name="web",
                    cpu=256,
                    memory=512,
                    image="busybox",
                    iam_role="media-writer",
                ),
                "worker": ServiceSpec(
                    name="worker",
                    cpu=512,
                    memory=1024,
                    image="busybox",
                    iam_role="locked-down",
                ),
                "proxy": ServiceSpec(
                    name="proxy", cpu=256, memory=512, image="busybox"
                ),
            }
        ),
        secrets=[],
    )


def _plain():
    return {"w": ServiceSpec(name="w", cpu=256, memory=512, image="x")}


def _emit(tmp_path, **kwargs):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, **kwargs), out)
    return {p.name: p.read_text() for p in out.iterdir() if p.is_file()}


class TestNoIamRolesBlockIsInert:
    def test_shared_task_role_is_still_emitted(self, tmp_path):
        iam = _emit(tmp_path, services=_plain())["iam.tf"]
        assert 'resource "aws_iam_role" "task" {' in iam
        assert 'name = "${var.project}-task"' in iam

    def test_no_declared_role_resources(self, tmp_path):
        iam = _emit(tmp_path, services=_plain())["iam.tf"]
        assert "rc_role_" not in iam
        assert "Declared task roles" not in iam

    def test_services_keep_the_shared_role(self, tmp_path):
        services = _emit(tmp_path, services=_plain())["services.tf"]
        assert "  task_role_arn            = aws_iam_role.task.arn" in services

    def test_outputs_have_no_iam_roles_entry(self, tmp_path):
        assert (
            'output "iam_roles"' not in _emit(tmp_path, services=_plain())["outputs.tf"]
        )

    def test_empty_block_emits_nothing(self, tmp_path):
        """An `iam_roles: {}` block must be as inert as no block at all."""
        with_block = _emit(tmp_path / "a", iam_roles={}, services=_plain())
        without = _emit(tmp_path / "b", services=_plain())
        assert with_block["iam.tf"] == without["iam.tf"]
        assert with_block["services.tf"] == without["services.tf"]

    def test_declared_roles_do_not_change_the_opted_out_service(self, tmp_path):
        """A service with no iam_role is byte-identical either way."""
        declared = _emit(tmp_path / "a", iam_roles=IAM_ROLES)["services.tf"]
        plain = _emit(
            tmp_path / "b",
            services={
                "web": ServiceSpec(name="web", cpu=256, memory=512, image="busybox"),
                "worker": ServiceSpec(
                    name="worker", cpu=512, memory=1024, image="busybox"
                ),
                "proxy": ServiceSpec(
                    name="proxy", cpu=256, memory=512, image="busybox"
                ),
            },
        )["services.tf"]
        proxy_declared = declared.split('resource "aws_ecs_task_definition" "proxy"')[1]
        proxy_plain = plain.split('resource "aws_ecs_task_definition" "proxy"')[1]
        assert proxy_declared.split("container_definitions")[0] == (
            proxy_plain.split("container_definitions")[0]
        )


class TestDeclaredRoleEmission:
    def test_role_resources_are_named_and_sorted(self, tmp_path):
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        assert 'resource "aws_iam_role" "rc_role_media_writer" {' in iam
        assert 'resource "aws_iam_role" "rc_role_locked_down" {' in iam
        assert iam.index("rc_role_locked_down") < iam.index("rc_role_media_writer")

    def test_role_name_and_description(self, tmp_path):
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        assert 'name        = "${var.project}-media-writer"' in iam
        assert 'description = "S3 media write for the web tier"' in iam

    def test_tags_are_emitted(self, tmp_path):
        assert '"tier" = "web"' in _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]

    def test_managed_policy_attachment(self, tmp_path):
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        assert (
            'resource "aws_iam_role_policy_attachment" '
            '"rc_role_media_writer_managed_0"' in iam
        )
        assert 'policy_arn = "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"' in iam

    def test_inline_policy_document_is_valid_json_with_conditions(self, tmp_path):
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        body = iam.split('resource "aws_iam_role_policy" "rc_role_media_writer_app"')[1]
        doc = json.loads(body.split("<<EOT\n")[1].split("\nEOT")[0])
        assert doc["Statement"][0]["Sid"] == "WriteMedia"
        assert doc["Statement"][0]["Effect"] == "Allow"
        assert doc["Statement"][0]["Condition"] == {
            "Bool": {"aws:SecureTransport": "true"}
        }

    def test_role_without_statements_emits_no_inline_app_policy(self, tmp_path):
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        assert "rc_role_locked_down_app" not in iam
        assert "rc_role_locked_down_managed_0" not in iam

    def test_every_declared_role_keeps_ecs_exec_permissions(self, tmp_path):
        """Without ssmmessages:*, `rc exec` / `rc db backup` break silently."""
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        for tf_name in ("rc_role_media_writer", "rc_role_locked_down"):
            assert f'resource "aws_iam_role_policy" "{tf_name}_execute_command"' in iam
        assert iam.count("ssmmessages:OpenDataChannel") == 3  # shared + 2 declared

    def test_shared_role_survives_alongside_declared_ones(self, tmp_path):
        iam = _emit(tmp_path, iam_roles=IAM_ROLES)["iam.tf"]
        assert 'resource "aws_iam_role" "task" {' in iam
        assert 'resource "aws_iam_role" "task_execution" {' in iam

    def test_task_definitions_point_at_the_declared_roles(self, tmp_path):
        services = _emit(tmp_path, iam_roles=IAM_ROLES)["services.tf"]
        assert "task_role_arn = aws_iam_role.rc_role_media_writer.arn" in services
        assert "task_role_arn = aws_iam_role.rc_role_locked_down.arn" in services
        # The service that opted out keeps the shared role, padded as before.
        assert "  task_role_arn            = aws_iam_role.task.arn" in services

    def test_role_arns_are_exported(self, tmp_path):
        outputs = _emit(tmp_path, iam_roles=IAM_ROLES)["outputs.tf"]
        assert 'output "iam_roles"' in outputs
        assert '"media-writer" = aws_iam_role.rc_role_media_writer.arn' in outputs
        assert '"locked-down"  = aws_iam_role.rc_role_locked_down.arn' in outputs

    def test_shared_role_grants_stay_on_the_shared_role(self, tmp_path):
        """provider_config.ecs.iam must not leak onto a declared role."""
        iam = _emit(
            tmp_path,
            iam_roles=IAM_ROLES,
            ecs_cfg={
                "iam": {
                    "statements": [
                        {
                            "sid": "Shared",
                            "actions": ["ses:SendEmail"],
                            "resources": ["*"],
                        }
                    ]
                }
            },
        )["iam.tf"]
        shared = iam.split('resource "aws_iam_role_policy" "task_app"')[1].split(
            "\n}\n"
        )[0]
        assert "ses:SendEmail" in shared
        assert iam.count("ses:SendEmail") == 1

    def test_two_services_sharing_a_role_emit_one_resource(self, tmp_path):
        iam = _emit(
            tmp_path,
            iam_roles={"tier": {}},
            services={
                "a": ServiceSpec(
                    name="a", cpu=256, memory=512, image="x", iam_role="tier"
                ),
                "b": ServiceSpec(
                    name="b", cpu=256, memory=512, image="x", iam_role="tier"
                ),
            },
        )["iam.tf"]
        assert iam.count('resource "aws_iam_role" "rc_role_tier"') == 1


class TestReferenceValidation:
    def test_unknown_role_on_a_service_is_refused(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            iam_roles={"known": {}},
            services={
                "web": ServiceSpec(
                    name="web", cpu=256, memory=512, image="x", iam_role="typo"
                )
            },
        )
        with pytest.raises(ConfigError, match="does not name a declared iam_roles"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_reference_without_any_declared_roles_is_refused(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            services={
                "web": ServiceSpec(
                    name="web", cpu=256, memory=512, image="x", iam_role="nope"
                )
            },
        )
        with pytest.raises(ConfigError, match="known: none"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestPlanner:
    def test_empty_plan(self):
        plan = build_iam_plan({})
        assert plan.is_empty and plan.roles == []

    def test_tf_names_sanitize_and_prefix(self):
        from remote_compose.config._schema_parser import _parse_iam_roles

        plan = build_iam_plan(_parse_iam_roles({"a-b-c": {}}))
        assert plan.roles[0].tf_name == "rc_role_a_b_c"
        assert plan.roles[0].arn_ref == "aws_iam_role.rc_role_a_b_c.arn"

    def test_role_arn_ref_raises_for_unknown_name(self):
        with pytest.raises(KeyError):
            build_iam_plan({}).role_arn_ref("nope")
