"""Phase execution: apply a MigrationPlan to a target tfstate.

Phases (in safe order):

    1. ValidatePhase            : read-only — re-discovers, diffs vs
                                  plan, refuses to proceed if drift.
    2. EmitV2TerraformPhase     : write generated v2 .tf into output_dir,
                                  splice in the import {} blocks.
    3. ImportStatePhase         : `terraform init` + `terraform plan`
                                  (must show ONLY in-place imports, no
                                  destroys), then `terraform apply` of
                                  the import-only changeset.
    4. ServicesCutoverPhase     : update task definitions to point at
                                  v2-shaped image refs + secrets ARNs;
                                  rolling-update each service.
    5. DecommissionV1Phase      : delete v1 SQLite tracking, archive the
                                  v1 rc.yml, NEVER touch SM/EFS/ALB.

Hard guard: phases 2-5 refuse to run unless a `--sandbox-tfstate-copy`
arg points at a freshly-cp'd backup of the live tfstate. The atomic
state swap happens only after a successful sandbox dry-run.

Tests use a fake terraform runner so no real apply happens. Real-AWS
e2e is in tests/integration/test_v1_migrate_e2e.py (separate bead).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote_compose.v1_migrate.apply import (
    DecommissionV1Phase,
    EmitV2TerraformPhase,
    ImportStatePhase,
    Phase,
    PhaseResult,
    SandboxStateGuardError,
    ServicesCutoverPhase,
    ValidatePhase,
)
from remote_compose.v1_migrate.discover import ResourceInventory, V1Stack
from remote_compose.v1_migrate.plan import build_plan


FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "v1_migrate"
V1_RC_YML = FIXTURES / "ss-debuggai-prod.rc.yml"
INVENTORY_JSON = FIXTURES / "inventory.json"


@pytest.fixture
def plan():
    stack = V1Stack.from_yaml(V1_RC_YML)
    inv = ResourceInventory.from_json(INVENTORY_JSON)
    return build_plan(stack, inv)


@pytest.fixture
def sandbox_tfstate(tmp_path: Path) -> Path:
    """Fake tfstate copy — represents the cp -r of live state."""
    s = tmp_path / "tfstate.copy"
    s.write_text(json.dumps({
        "version": 4,
        "terraform_version": "1.6.0",
        "resources": [],  # empty — we'll import into it
    }))
    return s


# ---------------------------------------------------------------------
# Phase ABC
# ---------------------------------------------------------------------

class TestPhaseAbc:
    def test_phase_is_abstract(self):
        with pytest.raises(TypeError):
            Phase()  # cannot instantiate abstract base

    def test_phase_result_shape(self):
        pr = PhaseResult(
            name="validate",
            ok=True,
            details="0 drift, 0 missing imports",
            undo_invoked=False,
        )
        assert pr.ok is True
        assert pr.name == "validate"


# ---------------------------------------------------------------------
# ValidatePhase — read-only
# ---------------------------------------------------------------------

class TestValidatePhase:
    def test_no_aws_calls_in_dry_run(self, plan, monkeypatch):
        # ValidatePhase MUST be safe to run against prod without any
        # mutating call. We inject a session that fails on any non-Get call.
        calls = []

        class StrictReadOnlySession:
            def __getattr__(self, name):
                if name.startswith("create_") or name.startswith("delete_") \
                        or name.startswith("update_") or name.startswith("put_"):
                    raise AssertionError(
                        f"ValidatePhase called mutating API: {name}"
                    )
                calls.append(name)
                return lambda *a, **k: None

        phase = ValidatePhase(plan=plan, aws_session=StrictReadOnlySession())
        result = phase.run()
        assert result.ok is True

    def test_drift_detected(self, plan, monkeypatch):
        # If the live state has drifted from inventory.json since
        # discover ran, ValidatePhase must refuse to proceed.
        class DriftedSession:
            def re_discover(self):
                # simulate: the EFS file system id changed
                from remote_compose.v1_migrate.discover import ResourceInventory
                d = ResourceInventory.from_json(INVENTORY_JSON).to_dict()
                d["efs"]["file_system_id"] = "fs-DIFFERENT"
                return ResourceInventory.from_dict(d)

        phase = ValidatePhase(plan=plan, aws_session=DriftedSession())
        result = phase.run()
        assert result.ok is False
        assert "drift" in result.details.lower()


# ---------------------------------------------------------------------
# EmitV2TerraformPhase — writes .tf, splices import {}
# ---------------------------------------------------------------------

class TestEmitV2TerraformPhase:
    def test_writes_main_tf(self, plan, tmp_path):
        out = tmp_path / "tf"
        phase = EmitV2TerraformPhase(plan=plan, output_dir=out)
        phase.run()
        assert (out / "main.tf").exists()

    def test_imports_spliced_into_dedicated_file(self, plan, tmp_path):
        # Keep import blocks in their own file for reviewability.
        out = tmp_path / "tf"
        phase = EmitV2TerraformPhase(plan=plan, output_dir=out)
        phase.run()
        imports_tf = out / "imports.tf"
        assert imports_tf.exists()
        content = imports_tf.read_text()
        assert "fs-0e8a2f9d1e006af95" in content
        assert "fsap-004097e867c7bb755" in content


# ---------------------------------------------------------------------
# ImportStatePhase — sandbox-state-copy guard + import-only changeset
# ---------------------------------------------------------------------

class TestImportStatePhase:
    def test_refuses_without_sandbox_copy(self, plan, tmp_path):
        # The hard rule: this phase MUST NOT touch the live tfstate.
        phase = ImportStatePhase(
            plan=plan,
            output_dir=tmp_path / "tf",
            sandbox_tfstate=None,
        )
        with pytest.raises(SandboxStateGuardError, match="sandbox"):
            phase.run()

    def test_refuses_if_sandbox_copy_path_missing(self, plan, tmp_path):
        phase = ImportStatePhase(
            plan=plan,
            output_dir=tmp_path / "tf",
            sandbox_tfstate=tmp_path / "does-not-exist",
        )
        with pytest.raises(SandboxStateGuardError, match="not found"):
            phase.run()

    def test_aborts_if_terraform_plan_shows_destroy(
        self, plan, tmp_path, sandbox_tfstate, monkeypatch
    ):
        # If terraform plan output contains any "- destroy" line,
        # ImportStatePhase must abort BEFORE running apply. This is
        # the last line of defense before mutation.
        out = tmp_path / "tf"
        out.mkdir()

        class FakeTerraform:
            def init(self, *a, **k): return ("", 0)
            def plan(self, *a, **k):
                return (
                    "Terraform will perform the following actions:\n"
                    "  # aws_efs_file_system.this will be destroyed\n",
                    2,
                )
            def apply(self, *a, **k):
                raise AssertionError("apply must not run when plan shows destroy")

        phase = ImportStatePhase(
            plan=plan,
            output_dir=out,
            sandbox_tfstate=sandbox_tfstate,
            terraform=FakeTerraform(),
        )
        result = phase.run()
        assert result.ok is False
        assert "destroy" in result.details.lower()

    def test_happy_path_runs_apply(
        self, plan, tmp_path, sandbox_tfstate
    ):
        out = tmp_path / "tf"
        out.mkdir()
        applied = []

        class FakeTerraform:
            def init(self, *a, **k): return ("", 0)
            def plan(self, *a, **k):
                return ("Plan: 6 to import, 0 to add, 0 to change, 0 to destroy.", 0)
            def apply(self, *a, **k):
                applied.append(True)
                return ("Apply complete! Resources: 6 imported.", 0)

        phase = ImportStatePhase(
            plan=plan,
            output_dir=out,
            sandbox_tfstate=sandbox_tfstate,
            terraform=FakeTerraform(),
        )
        result = phase.run()
        assert result.ok is True
        assert applied == [True]


# ---------------------------------------------------------------------
# ServicesCutoverPhase
# ---------------------------------------------------------------------

class TestServicesCutoverPhase:
    def test_updates_task_definitions_for_each_service(self, plan, monkeypatch):
        updated = []

        class FakeEcs:
            def register_task_definition(self, **kwargs):
                updated.append(kwargs["family"])
                return {"taskDefinition": {"taskDefinitionArn": "arn:..."}}

            def update_service(self, **kwargs):
                return {"service": {"serviceArn": "arn:..."}}

        phase = ServicesCutoverPhase(plan=plan, ecs_client=FakeEcs())
        phase.run()
        # 7 services in prod; all 7 must be updated.
        assert len(updated) == 7

    def test_secrets_referenced_by_arn_in_new_task_def(self, plan):
        # The new task def must put secrets[].valueFrom = full_arn,
        # never re-create or rename. Otherwise tasks fail to start.
        registered = []

        class FakeEcs:
            def register_task_definition(self, **kwargs):
                registered.append(kwargs)
                return {"taskDefinition": {"taskDefinitionArn": "arn:..."}}

            def update_service(self, **kwargs):
                return {"service": {"serviceArn": "arn:..."}}

        phase = ServicesCutoverPhase(plan=plan, ecs_client=FakeEcs())
        phase.run()
        # Find the django task def — it must carry SM ARNs.
        django_td = next(
            t for t in registered if t["family"].endswith("django")
        )
        secrets = django_td["containerDefinitions"][0].get("secrets", [])
        arns = [s["valueFrom"] for s in secrets]
        assert any("POSTGRES_PASSWORD" in arn for arn in arns)


# ---------------------------------------------------------------------
# DecommissionV1Phase — must NEVER touch SM/EFS/ALB
# ---------------------------------------------------------------------

class TestDecommissionV1Phase:
    def test_no_aws_destroy_calls(self, plan):
        forbidden = [
            "delete_secret",
            "delete_file_system",
            "delete_load_balancer",
            "delete_certificate",
        ]
        called = []

        class TripwireSession:
            def __getattr__(self, name):
                if name in forbidden:
                    raise AssertionError(
                        f"DecommissionV1Phase tried to call {name} — "
                        f"this would destroy live data"
                    )
                called.append(name)
                return lambda *a, **k: None

        phase = DecommissionV1Phase(plan=plan, aws_session=TripwireSession())
        phase.run()  # must complete without raising

    def test_archives_v1_rc_yml(self, plan, tmp_path):
        v1_yml = tmp_path / "rc.yml"
        v1_yml.write_text("# v1 rc.yml\ncluster: ss-debuggai-prod\n")
        phase = DecommissionV1Phase(
            plan=plan,
            v1_rc_yml_path=v1_yml,
            archive_dir=tmp_path / "archive",
        )
        phase.run()
        # The original is moved (not deleted) — recoverable.
        assert not v1_yml.exists()
        archived = list((tmp_path / "archive").glob("rc.yml.*"))
        assert len(archived) == 1
