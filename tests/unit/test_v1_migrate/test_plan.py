"""Migration plan composer: V1Stack + ResourceInventory → MigrationPlan.

A MigrationPlan is the full pre-flight document the operator approves
before any side-effecting AWS calls are made. It contains:

    - rc_v2_yml           : the new rc.yml dict (translated)
    - terraform_imports   : list[TerraformImportBlock] to splice into
                            the generated terraform tree before apply
    - secret_arn_map      : {short_name: full_arn} for env wiring
    - ecr_reuse_map       : {repo_name: uri}
    - external_iam        : {role_kind: arn}  # not project-managed
    - phases              : ordered list of MigrationPhase descriptors
                            (validate → emit → import-state → cutover →
                            decom). Each phase carries an undo command.
    - warnings            : list[TranslationWarning]
    - blast_radius        : dict summarizing what is touched (for the
                            human approval prompt)

The plan must be IDEMPOTENT: building the same plan twice from the
same inputs returns equal MigrationPlan objects (modulo timestamps).
This is what makes the dry-run trustworthy.

The plan must NEVER contain a destroy action for stateful resources
(EFS, ALB, ACM, VPC, SM secrets). A safety check at build_plan() time
fails loudly if any translator slipped one in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.v1_migrate.discover import ResourceInventory, V1Stack
from remote_compose.v1_migrate.plan import (
    MigrationPlan,
    PlanSafetyError,
    build_plan,
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
# build_plan composes per-resource translators
# ---------------------------------------------------------------------

class TestBuildPlanShape:
    def test_returns_migration_plan(self, stack, inv):
        plan = build_plan(stack, inv)
        assert isinstance(plan, MigrationPlan)

    def test_rc_v2_yml_present(self, stack, inv):
        plan = build_plan(stack, inv)
        assert plan.rc_v2_yml["version"] == 2
        assert plan.rc_v2_yml["project"] == "ss-debuggai"

    def test_terraform_imports_aggregated(self, stack, inv):
        plan = build_plan(stack, inv)
        ids = {i.id for i in plan.terraform_imports}
        # Critical resources must all appear.
        assert "fs-0e8a2f9d1e006af95" in ids        # EFS
        assert "fsap-004097e867c7bb755" in ids       # live postgres mount
        assert inv.alb.arn in ids                    # ALB
        assert inv.acm_cert.arn in ids               # ACM
        assert "vpc-053d0dfca255b6219" in ids        # VPC
        assert inv.ecs_cluster.arn in ids            # ECS cluster

    def test_secret_arn_map_populated(self, stack, inv):
        plan = build_plan(stack, inv)
        assert "POSTGRES_PASSWORD" in plan.secret_arn_map
        assert plan.secret_arn_map["POSTGRES_PASSWORD"].startswith(
            "arn:aws:secretsmanager:"
        )

    def test_external_iam_recorded_not_imported(self, stack, inv):
        plan = build_plan(stack, inv)
        assert plan.external_iam["task_execution_role_arn"].endswith(
            "/ecsTaskExecutionRole"
        )
        # IAM ARNs must NOT appear in terraform_imports.
        import_ids = {i.id for i in plan.terraform_imports}
        assert plan.external_iam["task_execution_role_arn"] not in import_ids


# ---------------------------------------------------------------------
# Idempotence — dry-run trustworthiness
# ---------------------------------------------------------------------

class TestBuildPlanIdempotence:
    def test_two_builds_equal(self, stack, inv):
        a = build_plan(stack, inv)
        b = build_plan(stack, inv)
        # Compare on the load-bearing fields. Timestamps (if any) are
        # excluded by MigrationPlan.equivalent_to().
        assert a.equivalent_to(b)

    def test_terraform_imports_stable_ordering(self, stack, inv):
        a = build_plan(stack, inv)
        b = build_plan(stack, inv)
        a_ids = [i.id for i in a.terraform_imports]
        b_ids = [i.id for i in b.terraform_imports]
        assert a_ids == b_ids


# ---------------------------------------------------------------------
# Phase descriptors with undo
# ---------------------------------------------------------------------

class TestPlanPhases:
    def test_phases_in_safe_order(self, stack, inv):
        plan = build_plan(stack, inv)
        names = [p.name for p in plan.phases]
        # validate FIRST, decom LAST.
        assert names[0] == "validate"
        assert names[-1] == "decommission_v1"
        # state import MUST happen before cutover, otherwise terraform
        # apply on cutover would try to recreate stateful resources.
        assert names.index("import_state") < names.index("services_cutover")

    def test_each_phase_carries_undo(self, stack, inv):
        plan = build_plan(stack, inv)
        for phase in plan.phases:
            if phase.name == "validate":
                continue  # validate is read-only, no undo
            assert phase.undo is not None and phase.undo.strip(), (
                f"phase {phase.name} missing undo runbook"
            )


# ---------------------------------------------------------------------
# Safety: plan rejects destructive translator output
# ---------------------------------------------------------------------

class TestPlanSafety:
    def test_rejects_destroy_on_stateful_resource(self, stack, inv, monkeypatch):
        # Inject a faulty translator output: an "EFS recreate" override.
        # build_plan must raise PlanSafetyError, not silently produce
        # a destructive plan.
        from remote_compose.v1_migrate import translate as t

        def _bad_translate(_inv):
            return ({"_destroy": True}, [], [])

        monkeypatch.setattr(t, "translate_efs_in_place", _bad_translate)
        with pytest.raises(PlanSafetyError, match="EFS"):
            build_plan(stack, inv)

    def test_rejects_missing_live_postgres_mount(self, stack, inv, monkeypatch):
        # If the EFS translator drops the live postgres access point,
        # the plan must refuse to build — losing this AP is data loss.
        from remote_compose.v1_migrate import translate as t

        def _missing_live_ap(_inv):
            from remote_compose.v1_migrate.translate import TerraformImportBlock
            return (
                {},
                [TerraformImportBlock(
                    id="fs-0e8a2f9d1e006af95",
                    to="module.efs.aws_efs_file_system.this",
                )],
                [],
            )

        monkeypatch.setattr(t, "translate_efs_in_place", _missing_live_ap)
        with pytest.raises(PlanSafetyError, match="postgres"):
            build_plan(stack, inv)


# ---------------------------------------------------------------------
# Blast radius summary (operator-facing)
# ---------------------------------------------------------------------

class TestBlastRadius:
    def test_blast_radius_summarizes_resources(self, stack, inv):
        plan = build_plan(stack, inv)
        br = plan.blast_radius
        assert br["efs_size_gb"] >= 100        # 133 GB
        assert br["secrets_count"] >= 15        # at least the 15 in fixture
        assert br["running_tasks"] == 7
        assert br["dns_managed_externally"] is True
