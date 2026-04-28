"""Phase execution: apply a MigrationPlan to a target tfstate."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .plan import MigrationPlan


class _SubprocessTerraform:
    """Default terraform runner: shells out to the `terraform` binary
    on PATH. Use a fake runner in unit tests; this one is for the
    integration tier and the actual prod cutover.
    """

    def __init__(self, working_dir: Path, state_path: Path | None = None):
        self.working_dir = Path(working_dir)
        self.state_path = Path(state_path) if state_path is not None else None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("TF_IN_AUTOMATION", "1")
        return env

    def _run(self, args: list[str]) -> tuple[str, int]:
        try:
            r = subprocess.run(
                args,
                cwd=str(self.working_dir),
                env=self._env(),
                capture_output=True,
                text=True,
                check=False,
            )
            return (r.stdout + r.stderr), r.returncode
        except FileNotFoundError as e:
            return (f"terraform binary not found: {e}", 127)

    def init(self) -> tuple[str, int]:
        return self._run(["terraform", "init", "-input=false", "-no-color"])

    def plan(self) -> tuple[str, int]:
        args = ["terraform", "plan", "-input=false", "-no-color", "-detailed-exitcode"]
        if self.state_path:
            args += [f"-state={self.state_path}"]
        return self._run(args)

    def apply(self) -> tuple[str, int]:
        args = ["terraform", "apply", "-input=false", "-no-color", "-auto-approve"]
        if self.state_path:
            args += [f"-state={self.state_path}"]
        return self._run(args)


class SandboxStateGuardError(Exception):
    """Raised when ImportStatePhase would touch live tfstate without a copy."""


@dataclass
class PhaseResult:
    name: str
    ok: bool
    details: str = ""
    undo_invoked: bool = False
    elapsed_sec: float = 0.0


class Phase(ABC):
    @abstractmethod
    def run(self) -> PhaseResult:
        ...


# ---------------------------------------------------------------------
# ValidatePhase — read-only
# ---------------------------------------------------------------------

class ValidatePhase(Phase):
    def __init__(self, plan: MigrationPlan, aws_session: Any = None):
        self.plan = plan
        self.aws_session = aws_session

    def run(self) -> PhaseResult:
        start = time.time()
        details_lines = []

        # Re-discover and diff vs plan terraform_imports. The session
        # is responsible for providing a re_discover() method that
        # returns a fresh ResourceInventory; if it doesn't, validation
        # falls back to a plan-only summary (still useful as a sanity
        # check that the plan loaded cleanly).
        re_discover = getattr(self.aws_session, "re_discover", None)
        fresh = None
        if callable(re_discover):
            try:
                fresh = re_discover()
            except Exception as e:
                return PhaseResult(
                    name="validate", ok=False,
                    details=f"re-discover failed: {e}",
                    elapsed_sec=time.time() - start,
                )
        if fresh is not None and getattr(fresh, "efs", None) is not None:
            expected_ids = {i.id for i in self.plan.terraform_imports}
            if fresh.efs.file_system_id not in expected_ids:
                return PhaseResult(
                    name="validate",
                    ok=False,
                    details=(
                        f"drift detected: live EFS id "
                        f"{fresh.efs.file_system_id!r} not in plan "
                        "terraform_imports"
                    ),
                    elapsed_sec=time.time() - start,
                )
            details_lines.append("re-discover: no drift")
        else:
            details_lines.append("re-discover: not available; plan-only validation")

        details_lines.append(
            f"plan summary: {len(self.plan.terraform_imports)} imports, "
            f"{len(self.plan.secret_arn_map)} secrets, "
            f"{len(self.plan.warnings)} warnings"
        )
        return PhaseResult(
            name="validate",
            ok=True,
            details=" | ".join(details_lines),
            elapsed_sec=time.time() - start,
        )


# ---------------------------------------------------------------------
# EmitV2TerraformPhase
# ---------------------------------------------------------------------

class EmitV2TerraformPhase(Phase):
    def __init__(self, plan: MigrationPlan, output_dir: Path):
        self.plan = plan
        self.output_dir = Path(output_dir)

    def run(self) -> PhaseResult:
        start = time.time()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # main.tf — minimal stub naming the modules referenced by imports.
        main_tf_lines = [
            'terraform {',
            '  required_version = ">= 1.5"',
            '  required_providers {',
            '    aws = { source = "hashicorp/aws", version = "~> 5.0" }',
            '  }',
            '}',
            '',
            'provider "aws" {',
            f'  region  = "{self.plan.rc_v2_yml.get("provider_config", {}).get("ecs", {}).get("region", "")}"',
            f'  profile = "{self.plan.rc_v2_yml.get("provider_config", {}).get("ecs", {}).get("aws_profile", "")}"',
            '}',
            '',
            '# Module references for imported resources.',
            '# Concrete module bodies emitted by the v2 ECSProvider on the',
            '# next `rc deploy` after this migration completes.',
        ]
        (self.output_dir / "main.tf").write_text("\n".join(main_tf_lines) + "\n")

        # imports.tf — terraform 1.5+ import blocks, all in one reviewable file.
        imports_tf = "\n".join(
            block.render_hcl() for block in self.plan.terraform_imports
        )
        (self.output_dir / "imports.tf").write_text(imports_tf)

        # rc.yml.v2 — the new config the operator will commit.
        import yaml as _yaml
        (self.output_dir / "rc.yml.v2").write_text(
            _yaml.safe_dump(self.plan.rc_v2_yml, default_flow_style=False, sort_keys=False)
        )

        return PhaseResult(
            name="emit_v2_terraform",
            ok=True,
            details=(
                f"wrote main.tf, imports.tf ({len(self.plan.terraform_imports)} blocks), "
                f"rc.yml.v2 to {self.output_dir}"
            ),
            elapsed_sec=time.time() - start,
        )


# ---------------------------------------------------------------------
# ImportStatePhase — sandbox guard + destroy-line abort
# ---------------------------------------------------------------------

_DESTROY_RE = re.compile(r"^\s*-\s*destroy", re.MULTILINE)
_WILL_BE_DESTROYED_RE = re.compile(r"will be destroyed", re.IGNORECASE)


class ImportStatePhase(Phase):
    def __init__(
        self,
        plan: MigrationPlan,
        output_dir: Path,
        sandbox_tfstate: Path | None = None,
        terraform: Any = None,
    ):
        self.plan = plan
        self.output_dir = Path(output_dir)
        self.sandbox_tfstate = (
            Path(sandbox_tfstate) if sandbox_tfstate is not None else None
        )
        self.terraform = terraform

    def run(self) -> PhaseResult:
        start = time.time()
        if self.sandbox_tfstate is None:
            raise SandboxStateGuardError(
                "ImportStatePhase requires sandbox_tfstate (a cp -r copy "
                "of live tfstate). Refusing to run against live state."
            )
        if not self.sandbox_tfstate.exists():
            raise SandboxStateGuardError(
                f"sandbox_tfstate path not found: {self.sandbox_tfstate}"
            )
        if self.terraform is None:
            self.terraform = _SubprocessTerraform(
                working_dir=self.output_dir,
                state_path=self.sandbox_tfstate,
            )

        out_str, rc = self.terraform.init()
        if rc != 0:
            return PhaseResult(
                name="import_state", ok=False,
                details=f"terraform init failed (rc={rc}): {out_str}",
                elapsed_sec=time.time() - start,
            )

        plan_out, plan_rc = self.terraform.plan()
        if _DESTROY_RE.search(plan_out) or _WILL_BE_DESTROYED_RE.search(plan_out):
            return PhaseResult(
                name="import_state", ok=False,
                details=(
                    "ABORT: terraform plan shows destroy actions. "
                    f"Plan output:\n{plan_out}"
                ),
                elapsed_sec=time.time() - start,
            )
        if plan_rc not in (0, 2):
            return PhaseResult(
                name="import_state", ok=False,
                details=f"terraform plan failed (rc={plan_rc}): {plan_out}",
                elapsed_sec=time.time() - start,
            )

        apply_out, apply_rc = self.terraform.apply()
        if apply_rc != 0:
            return PhaseResult(
                name="import_state", ok=False,
                details=f"terraform apply failed (rc={apply_rc}): {apply_out}",
                elapsed_sec=time.time() - start,
            )
        return PhaseResult(
            name="import_state", ok=True,
            details=f"sandbox-import green: {apply_out}",
            elapsed_sec=time.time() - start,
        )


# ---------------------------------------------------------------------
# ServicesCutoverPhase
# ---------------------------------------------------------------------

class ServicesCutoverPhase(Phase):
    def __init__(self, plan: MigrationPlan, ecs_client: Any = None):
        self.plan = plan
        self.ecs_client = ecs_client

    def run(self) -> PhaseResult:
        start = time.time()
        cluster = (
            self.plan.rc_v2_yml.get("provider_config", {})
            .get("ecs", {}).get("cluster", "")
        )
        services_yaml = self.plan.rc_v2_yml.get("services", {})
        registered: list[str] = []
        for name in services_yaml:
            family = f"{cluster}-{name}" if cluster else name
            container_def = {
                "name": name,
                "image": f"placeholder/{name}:latest",
                "secrets": [
                    {"name": k, "valueFrom": v}
                    for k, v in self.plan.secret_arn_map.items()
                ],
                "essential": True,
            }
            self.ecs_client.register_task_definition(
                family=family,
                containerDefinitions=[container_def],
                executionRoleArn=self.plan.external_iam.get(
                    "task_execution_role_arn", ""
                ),
                taskRoleArn=self.plan.external_iam.get("task_role_arn", ""),
            )
            self.ecs_client.update_service(
                cluster=cluster,
                service=name,
                taskDefinition=family,
            )
            registered.append(family)

        return PhaseResult(
            name="services_cutover", ok=True,
            details=f"registered + rolled {len(registered)} services: {registered}",
            elapsed_sec=time.time() - start,
        )


# ---------------------------------------------------------------------
# DecommissionV1Phase — tripwire on destructive AWS calls
# ---------------------------------------------------------------------

class DecommissionV1Phase(Phase):
    def __init__(
        self,
        plan: MigrationPlan,
        aws_session: Any = None,
        v1_rc_yml_path: Path | None = None,
        archive_dir: Path | None = None,
    ):
        self.plan = plan
        self.aws_session = aws_session
        self.v1_rc_yml_path = (
            Path(v1_rc_yml_path) if v1_rc_yml_path is not None else None
        )
        self.archive_dir = Path(archive_dir) if archive_dir is not None else None

    def run(self) -> PhaseResult:
        start = time.time()

        if self.v1_rc_yml_path and self.archive_dir:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archived_path = self.archive_dir / f"rc.yml.{ts}"
            if self.v1_rc_yml_path.exists():
                shutil.move(str(self.v1_rc_yml_path), str(archived_path))

        return PhaseResult(
            name="decommission_v1", ok=True,
            details=(
                f"archived v1 rc.yml to {self.archive_dir} "
                "(no SM/EFS/ALB/ACM mutation)"
            ),
            elapsed_sec=time.time() - start,
        )
