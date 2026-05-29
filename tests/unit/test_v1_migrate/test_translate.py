"""Per-resource translators: v1 stack + inventory → v2 rc.yml + terraform imports.

Each translator is focused (mirror the copilot-import pattern):

    translate_v1_to_v2_schema(stack)           -> (rc_yml_dict, warnings)
    translate_efs_in_place(inv)                 -> (overrides, imports, warnings)
    translate_alb_in_place(inv)                 -> (overrides, imports, warnings)
    translate_acm_in_place(inv)                 -> (overrides, imports, warnings)
    translate_secrets_keep_arn(inv)             -> (rc_yml_secrets, warnings)
    translate_vpc_in_place(inv)                 -> (overrides, imports, warnings)
    translate_iam_keep_external(inv)            -> (overrides, warnings)  # no imports
    translate_ecr_reuse(inv)                    -> (overrides, warnings)  # no imports
    translate_ecs_cluster_in_place(inv)         -> (overrides, imports, warnings)

`imports` is a list of TerraformImportBlock dataclasses (terraform 1.5+
import blocks: `import { id = "..."; to = "module.x.aws_y.z" }`),
which the planner emits into the generated v2 terraform tree.

The contract here is the foundation of zero-data-loss: any translator
that returns IMPORT for a stateful resource (EFS, ALB, ACM, VPC) must
NOT emit a recreate path, ever. Tests pin that property.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.v1_migrate.discover import ResourceInventory, V1Stack
from remote_compose.v1_migrate.translate import (
    TerraformImportBlock,
    translate_acm_in_place,
    translate_alb_in_place,
    translate_ecr_reuse,
    translate_ecs_cluster_in_place,
    translate_efs_in_place,
    translate_iam_keep_external,
    translate_secrets_keep_arn,
    translate_v1_to_v2_schema,
    translate_vpc_in_place,
)

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "v1_migrate"
V1_RC_YML = FIXTURES / "ss-debuggai-prod.rc.yml"
INVENTORY_JSON = FIXTURES / "inventory.json"


@pytest.fixture
def stack() -> V1Stack:
    return V1Stack.from_yaml(V1_RC_YML)


@pytest.fixture
def inv() -> ResourceInventory:
    return ResourceInventory.from_json(INVENTORY_JSON)


# ---------------------------------------------------------------------
# v1 → v2 schema (the rc.yml shape change)
# ---------------------------------------------------------------------


class TestTranslateV1V2Schema:
    def test_emits_v2_top_level(self, stack):
        rc_yml, _ = translate_v1_to_v2_schema(stack)
        assert rc_yml["version"] == 2
        assert rc_yml["project"] == "ss-debuggai"
        assert rc_yml["provider"] == "ecs"

    def test_provider_config_carries_cluster_region(self, stack):
        rc_yml, _ = translate_v1_to_v2_schema(stack)
        ecs = rc_yml["provider_config"]["ecs"]
        assert ecs["cluster"] == "ss-debuggai-prod"
        assert ecs["region"] == "us-west-2"
        assert ecs["aws_profile"] == "debuggai"

    def test_django_service_translated(self, stack):
        rc_yml, _ = translate_v1_to_v2_schema(stack)
        django = rc_yml["services"]["django"]
        assert django["cpu"] == 1024
        assert django["memory"] == 4096
        assert django["type"] == "application"
        assert django["health_check_path"] == "/api/health/"

    def test_nginx_proxy_translated(self, stack):
        rc_yml, _ = translate_v1_to_v2_schema(stack)
        nginx = rc_yml["services"]["nginx"]
        assert nginx["public"] is True
        assert nginx["default_target"] is True
        assert nginx["domain"] == "api.startsimpli.com"

    def test_v1_compose_file_recorded_for_audit(self, stack):
        # The migration plan keeps a record of which compose file v1
        # pointed at, so we can diff when reconciling with v2's
        # docker-compose.local.yml path.
        rc_yml, warnings = translate_v1_to_v2_schema(stack)
        assert any(
            "docker-compose.ecs.yml" in str(w.message) for w in warnings
        ), "expected a warning recording the v1 compose_file path"


# ---------------------------------------------------------------------
# EFS — IMPORT (preserve 133GB volume in-place)
# ---------------------------------------------------------------------


class TestTranslateEfsInPlace:
    def test_emits_import_block_not_recreate(self, inv):
        overrides, imports, warnings = translate_efs_in_place(inv)
        ids = [i.id for i in imports]
        assert "fs-0e8a2f9d1e006af95" in ids, (
            "EFS file system MUST be imported, never recreated — "
            "recreating drops 133GB of postgres data"
        )

    def test_live_postgres_access_point_imported(self, inv):
        _, imports, _ = translate_efs_in_place(inv)
        ap_ids = [i.id for i in imports]
        # The live mount path is the one that matters most.
        assert "fsap-004097e867c7bb755" in ap_ids

    def test_all_seven_access_points_imported(self, inv):
        # 7 access points exist in prod; missing any one would orphan
        # storage state.
        _, imports, _ = translate_efs_in_place(inv)
        ap_imports = [i for i in imports if i.id.startswith("fsap-")]
        assert len(ap_imports) == 7

    def test_no_destroy_action_in_overrides(self, inv):
        overrides, _, _ = translate_efs_in_place(inv)
        # Belt-and-suspenders: the overrides dict must never carry a
        # `_destroy: true` flag for EFS.
        assert overrides.get("_destroy") is not True


# ---------------------------------------------------------------------
# ALB — IMPORT (preserves DNS chain to api.startsimpli.com)
# ---------------------------------------------------------------------


class TestTranslateAlbInPlace:
    def test_alb_imported(self, inv):
        _, imports, _ = translate_alb_in_place(inv)
        assert any(
            i.id == inv.alb.arn for i in imports
        ), "ALB ARN must be in the import set — DNS at registrar points here"

    def test_listeners_imported(self, inv):
        _, imports, _ = translate_alb_in_place(inv)
        listener_arns = [i.id for i in imports if ":listener/" in i.id]
        # 2 listeners in prod (HTTP→redirect, HTTPS→forward)
        assert len(listener_arns) == 2

    def test_target_groups_imported(self, inv):
        _, imports, _ = translate_alb_in_place(inv)
        tg_arns = [i.id for i in imports if ":targetgroup/" in i.id]
        assert len(tg_arns) == 2


# ---------------------------------------------------------------------
# ACM — IMPORT (cert ARN is referenced in HTTPS listener; recreate breaks SSL)
# ---------------------------------------------------------------------


class TestTranslateAcmInPlace:
    def test_cert_imported_not_recreated(self, inv):
        _, imports, _ = translate_acm_in_place(inv)
        cert_arns = [i.id for i in imports if ":certificate/" in i.id]
        assert inv.acm_cert.arn in cert_arns


# ---------------------------------------------------------------------
# Secrets — REFERENCE BY ARN (zero SM mutation, preserves 30d name lock)
# ---------------------------------------------------------------------


class TestTranslateSecretsKeepArn:
    def test_emits_arn_source_not_env_file_auto(self, inv):
        rc_secrets, _ = translate_secrets_keep_arn(inv)
        sources = {s["source"] for s in rc_secrets}
        assert sources == {"arn"}, (
            "All 32 secrets must use source=arn — env_file_auto would "
            "rewrite SM values and brick running tasks during the "
            "30-day name-reservation window"
        )

    def test_all_thirty_two_secrets_referenced(self, inv):
        # Inventory has 15 detailed + 16 truncated = 32 in prod.
        # The fixture only lists 15; so this test pins what we can see
        # from the fixture, plus a separate assertion that the reader
        # must not silently drop the truncation marker.
        rc_secrets, warnings = translate_secrets_keep_arn(inv)
        # Either we have all 32 (when called against real AWS), or we
        # have a warning explicitly noting the truncation.
        if len(rc_secrets) < 32:
            assert any(
                "truncat" in w.message.lower() for w in warnings
            ), "fixture-truncated secrets must surface as a warning"

    def test_secret_arns_include_revision_suffix(self, inv):
        rc_secrets, _ = translate_secrets_keep_arn(inv)
        # SM ARNs end in -XXXXXX revision; rc v2 must carry the full
        # ARN, not the bare name (otherwise resolve fails).
        for s in rc_secrets:
            assert s["arn"].count(":secret:") == 1
            assert s["arn"].split(":")[-1].count("-") >= 1


# ---------------------------------------------------------------------
# VPC — IMPORT (already remote-compose:managed=true, friendly to import)
# ---------------------------------------------------------------------


class TestTranslateVpcInPlace:
    def test_vpc_imported(self, inv):
        _, imports, _ = translate_vpc_in_place(inv)
        assert any(i.id == inv.vpc.id for i in imports)

    def test_subnets_imported(self, inv):
        _, imports, _ = translate_vpc_in_place(inv)
        subnet_ids = [i.id for i in imports if i.id.startswith("subnet-")]
        assert sorted(subnet_ids) == sorted(inv.vpc.subnets)

    def test_security_groups_imported(self, inv):
        _, imports, _ = translate_vpc_in_place(inv)
        sg_ids = [i.id for i in imports if i.id.startswith("sg-")]
        assert sorted(sg_ids) == sorted(inv.vpc.security_groups)


# ---------------------------------------------------------------------
# IAM — REFERENCE BY ARN (account-wide, NOT project-managed)
# ---------------------------------------------------------------------


class TestTranslateIamKeepExternal:
    def test_no_imports_emitted(self, inv):
        # IAM roles are external — v2 references by ARN, never imports.
        result = translate_iam_keep_external(inv)
        # Tuple shape distinguishes external translators from in-place ones.
        overrides, warnings = result
        assert overrides["task_execution_role_arn"].endswith("/ecsTaskExecutionRole")
        assert overrides["task_role_arn"].endswith("/ecsTaskRole")


# ---------------------------------------------------------------------
# ECR — REUSE (same repo names work across rc v1/v2)
# ---------------------------------------------------------------------


class TestTranslateEcrReuse:
    def test_emits_repo_uris_for_reuse(self, inv):
        overrides, _ = translate_ecr_reuse(inv)
        repos = overrides.get("ecr_repositories", {})
        # The 6 prod ECR repos must be carried forward by URI.
        assert "ss-debuggai/django" in repos
        assert "ss-debuggai/celery-worker" in repos
        assert repos["ss-debuggai/django"].endswith("amazonaws.com/ss-debuggai/django")


# ---------------------------------------------------------------------
# ECS cluster — IMPORT (preserves Service Connect namespace, log groups)
# ---------------------------------------------------------------------


class TestTranslateEcsClusterInPlace:
    def test_cluster_imported(self, inv):
        _, imports, _ = translate_ecs_cluster_in_place(inv)
        assert any(i.id == inv.ecs_cluster.arn for i in imports)

    def test_warns_if_running_tasks_will_drain_during_cutover(self, inv):
        _, _, warnings = translate_ecs_cluster_in_place(inv)
        # 7 running tasks; cutover replaces task defs → 7 brief
        # restarts. That MUST surface as an explicit warning so the
        # operator factors it into the maintenance window budget.
        assert any("running task" in w.message.lower() for w in warnings)


# ---------------------------------------------------------------------
# TerraformImportBlock dataclass shape
# ---------------------------------------------------------------------


class TestTerraformImportBlockShape:
    def test_block_has_id_and_to(self):
        block = TerraformImportBlock(
            id="fs-0e8a2f9d1e006af95",
            to="module.efs.aws_efs_file_system.this",
        )
        # Must render to terraform 1.5+ import-block syntax.
        rendered = block.render_hcl()
        assert "import {" in rendered
        assert 'id = "fs-0e8a2f9d1e006af95"' in rendered
        assert (
            "to  = module.efs.aws_efs_file_system.this" in rendered
            or "to = module.efs.aws_efs_file_system.this" in rendered
        )
