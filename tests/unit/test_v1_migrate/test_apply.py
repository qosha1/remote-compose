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
    s.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.6.0",
                "resources": [],  # empty — we'll import into it
            }
        )
    )
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
                if (
                    name.startswith("create_")
                    or name.startswith("delete_")
                    or name.startswith("update_")
                    or name.startswith("put_")
                ):
                    raise AssertionError(f"ValidatePhase called mutating API: {name}")
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
            def init(self, *a, **k):
                return ("", 0)

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

    def test_happy_path_runs_apply(self, plan, tmp_path, sandbox_tfstate):
        out = tmp_path / "tf"
        out.mkdir()
        applied = []

        class FakeTerraform:
            def init(self, *a, **k):
                return ("", 0)

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


class _FakeEcs:
    """boto3 ecs.client stub: serves describe_services + describe_task_definition,
    captures register_task_definition + update_service calls.
    """

    def __init__(self, services: dict[str, dict]):
        # services: {service_name: existing_task_def_dict}
        self._services = services
        self.registered: list[dict] = []
        self.updates: list[dict] = []

    def list_services(self, cluster):
        return {
            "serviceArns": [
                f"arn:aws:ecs:us-west-2:0:service/{cluster}/{n}" for n in self._services
            ],
        }

    def describe_services(self, cluster, services):
        out = []
        for name in services:
            if name in self._services:
                out.append(
                    {
                        "serviceName": name,
                        "taskDefinition": (
                            f"arn:aws:ecs:us-west-2:0:task-definition/" f"{name}:1"
                        ),
                    }
                )
        return {"services": out}

    def describe_task_definition(self, taskDefinition):
        # taskDefinition is "...:family:rev"; family is parent of name
        family = taskDefinition.split("/")[-1].split(":")[0]
        return {"taskDefinition": self._services[family]}

    def register_task_definition(self, **kwargs):
        self.registered.append(kwargs)
        return {
            "taskDefinition": {
                "taskDefinitionArn": (
                    f"arn:aws:ecs:us-west-2:0:task-definition/" f"{kwargs['family']}:2"
                ),
            },
        }

    def update_service(self, **kwargs):
        self.updates.append(kwargs)
        return {"service": {"serviceArn": "arn:..."}}


def _v1_django_task_def() -> dict:
    """A representative v1-shaped task def (envfile-injected secrets in env)."""
    return {
        "family": "ss-debuggai-django",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "1024",
        "memory": "4096",
        "executionRoleArn": "arn:aws:iam::033937118837:role/ecsTaskExecutionRole",
        "taskRoleArn": "arn:aws:iam::033937118837:role/ecsTaskRole",
        "ephemeralStorage": {"sizeInGiB": 40},
        "containerDefinitions": [
            {
                "name": "django",
                "image": "033937118837.dkr.ecr.us-west-2.amazonaws.com/ss-debuggai/django:abc123",
                "essential": True,
                # v1 envfile injection: real secret values pasted in env (will be stripped).
                "environment": [
                    {
                        "name": "DEBUG",
                        "value": "False",
                    },  # not in plan.secret_arn_map -> kept
                    {
                        "name": "POSTGRES_PASSWORD",
                        "value": "real-secret-leaked",
                    },  # collides -> dropped
                    {"name": "DJANGO_LOG_LEVEL", "value": "INFO"},
                ],
                "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
                "mountPoints": [
                    {
                        "sourceVolume": "static",
                        "containerPath": "/app/static",
                        "readOnly": False,
                    }
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": "/ecs/ss-debuggai-prod",
                        "awslogs-region": "us-west-2",
                        "awslogs-stream-prefix": "django",
                    },
                },
            }
        ],
        "volumes": [
            {
                "name": "static",
                "efsVolumeConfiguration": {
                    "fileSystemId": "fs-0e8a2f9d1e006af95",
                    "rootDirectory": "/",
                    "transitEncryption": "ENABLED",
                    "authorizationConfig": {
                        "accessPointId": "fsap-0027054fe47e721f1",
                        "iam": "DISABLED",
                    },
                },
            }
        ],
        # Fields that describe_task_definition returns but RegisterTaskDefinition rejects:
        "taskDefinitionArn": "arn:aws:ecs:us-west-2:0:task-definition/ss-debuggai-django:1",
        "revision": 1,
        "status": "ACTIVE",
        "registeredAt": "2026-01-01T00:00:00Z",
        "registeredBy": "arn:aws:iam::0:user/ci",
    }


class TestServicesCutoverPhase:
    def test_rolls_each_v1_service_with_v2_secrets(self, plan):
        # Stand up 7 v1 services, all with the same shape skeleton.
        services = {
            f"ss-debuggai-{name}": {
                **_v1_django_task_def(),
                "family": f"ss-debuggai-{name}",
            }
            for name in [
                "django",
                "postgres",
                "redis",
                "nginx",
                "celery-worker",
                "celery-beat",
                "celery-worker-linkedin",
            ]
        }
        ecs = _FakeEcs(services)

        result = ServicesCutoverPhase(
            plan=plan,
            ecs_client=ecs,
        ).run()
        assert result.ok, result.details
        # All 7 services registered + rolled.
        assert len(ecs.registered) == 7
        assert len(ecs.updates) == 7

    def test_secrets_arrayed_by_arn_in_each_new_task_def(self, plan):
        services = {"ss-debuggai-django": _v1_django_task_def()}
        ecs = _FakeEcs(services)
        ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()
        td = ecs.registered[0]
        c0 = td["containerDefinitions"][0]
        secret_names = {s["name"] for s in c0["secrets"]}
        assert "POSTGRES_PASSWORD" in secret_names
        # Every secret valueFrom is a full SM ARN.
        for s in c0["secrets"]:
            assert s["valueFrom"].startswith(
                "arn:aws:secretsmanager:us-west-2:033937118837:secret:"
            )

    def test_drops_env_keys_that_collide_with_secrets(self, plan):
        services = {"ss-debuggai-django": _v1_django_task_def()}
        ecs = _FakeEcs(services)
        ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()
        c0 = ecs.registered[0]["containerDefinitions"][0]
        env_names = {e["name"] for e in c0.get("environment", [])}
        # Collision: dropped.
        assert "POSTGRES_PASSWORD" not in env_names
        # Non-collision: preserved.
        assert "DEBUG" in env_names
        assert "DJANGO_LOG_LEVEL" in env_names

    def test_image_volumes_mounts_preserved(self, plan):
        services = {"ss-debuggai-django": _v1_django_task_def()}
        ecs = _FakeEcs(services)
        ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()
        td = ecs.registered[0]
        c0 = td["containerDefinitions"][0]
        # Image, ports, mounts, log config preserved verbatim.
        assert c0["image"].endswith("django:abc123")
        assert c0["portMappings"] == [{"containerPort": 8000, "protocol": "tcp"}]
        assert c0["mountPoints"][0]["containerPath"] == "/app/static"
        # Volumes (the EFS reference) preserved at the task-def level.
        assert (
            td["volumes"][0]["efsVolumeConfiguration"]["fileSystemId"]
            == "fs-0e8a2f9d1e006af95"
        )
        # Ephemeral storage preserved.
        assert td["ephemeralStorage"] == {"sizeInGiB": 40}

    def test_strips_describe_only_fields(self, plan):
        # RegisterTaskDefinition rejects revision, status, taskDefinitionArn,
        # registeredAt, registeredBy. Make sure we strip them.
        services = {"ss-debuggai-django": _v1_django_task_def()}
        ecs = _FakeEcs(services)
        ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()
        td = ecs.registered[0]
        for forbidden in (
            "revision",
            "status",
            "taskDefinitionArn",
            "registeredAt",
            "registeredBy",
        ):
            assert (
                forbidden not in td
            ), f"{forbidden!r} leaked into RegisterTaskDefinition"

    def test_aborts_on_error_with_rollback_hint(self, plan):
        # If register_task_definition raises mid-flight, the phase must
        # report which services were rolled and how to roll them back.
        services = {
            "ss-debuggai-django": _v1_django_task_def(),
            "ss-debuggai-postgres": _v1_django_task_def(),
            "ss-debuggai-redis": _v1_django_task_def(),
            "ss-debuggai-nginx": _v1_django_task_def(),
            "ss-debuggai-celery-worker": _v1_django_task_def(),
            "ss-debuggai-celery-beat": _v1_django_task_def(),
            "ss-debuggai-celery-worker-linkedin": _v1_django_task_def(),
        }
        ecs = _FakeEcs(services)
        # Make register_task_definition fail on the 3rd call.
        original = ecs.register_task_definition
        call_count = [0]

        def flaky(**kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise RuntimeError("simulated AWS throttle")
            return original(**kwargs)

        ecs.register_task_definition = flaky

        result = ServicesCutoverPhase(plan=plan, ecs_client=ecs).run()
        assert not result.ok
        # Error message names the failing service + rollback hint.
        assert "Manual rollback" in result.details
        assert "throttle" in result.details


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
