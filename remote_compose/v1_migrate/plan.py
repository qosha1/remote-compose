"""Migration plan composer: V1Stack + ResourceInventory → MigrationPlan."""

from __future__ import annotations

from dataclasses import dataclass, field

from .discover import ResourceInventory, V1Stack
from . import translate as _translate
from .translate import (
    TerraformImportBlock,
    TranslationWarning,
)


class PlanSafetyError(Exception):
    """Raised when build_plan would otherwise produce a destructive plan."""


@dataclass
class MigrationPhase:
    name: str
    undo: str = ""
    description: str = ""


@dataclass
class MigrationPlan:
    rc_v2_yml: dict = field(default_factory=dict)
    terraform_imports: list[TerraformImportBlock] = field(default_factory=list)
    secret_arn_map: dict[str, str] = field(default_factory=dict)
    ecr_reuse_map: dict[str, str] = field(default_factory=dict)
    external_iam: dict[str, str] = field(default_factory=dict)
    phases: list[MigrationPhase] = field(default_factory=list)
    warnings: list[TranslationWarning] = field(default_factory=list)
    blast_radius: dict = field(default_factory=dict)

    def render_summary_md(self) -> str:
        """Operator-facing migration plan summary."""
        cluster = (
            self.rc_v2_yml.get("provider_config", {})
            .get("ecs", {})
            .get("cluster", "(unknown)")
        )
        region = (
            self.rc_v2_yml.get("provider_config", {})
            .get("ecs", {})
            .get("region", "(unknown)")
        )
        lines: list[str] = []
        lines.append(f"# Migration Plan — {cluster} ({region})")
        lines.append("")
        lines.append("## Blast radius")
        lines.append("")
        for k, v in self.blast_radius.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")
        lines.append("## Terraform imports (preserve in-place)")
        lines.append("")
        lines.append("| Resource ID | Module address |")
        lines.append("| --- | --- |")
        for imp in self.terraform_imports:
            lines.append(f"| `{imp.id}` | `{imp.to}` |")
        lines.append("")
        lines.append("## Secrets (referenced by ARN — zero SM mutation)")
        lines.append("")
        for short, arn in sorted(self.secret_arn_map.items()):
            lines.append(f"- `{short}` → `{arn}`")
        lines.append("")
        lines.append("## External IAM (account-wide, not project-managed)")
        lines.append("")
        for k, v in self.external_iam.items():
            lines.append(f"- **{k}**: `{v}`")
        lines.append("")
        lines.append("## ECR repositories (reused)")
        lines.append("")
        for name, uri in sorted(self.ecr_reuse_map.items()):
            lines.append(f"- `{name}` → `{uri}`")
        lines.append("")
        lines.append("## Phases (with undo commands)")
        lines.append("")
        for phase in self.phases:
            lines.append(f"### {phase.name}")
            lines.append("")
            if phase.description:
                lines.append(phase.description)
            if phase.undo:
                lines.append("")
                lines.append(f"**Undo:** `{phase.undo}`")
            lines.append("")
        if self.warnings:
            lines.append("## Warnings")
            lines.append("")
            for w in self.warnings:
                tag = w.severity.upper() if hasattr(w, "severity") else "INFO"
                where = w.service or "(global)"
                lines.append(f"- [{tag}] {where}: {w.message}")
            lines.append("")
        return "\n".join(lines)

    def equivalent_to(self, other: "MigrationPlan") -> bool:
        if self.rc_v2_yml != other.rc_v2_yml:
            return False
        a_imports = sorted((i.id, i.to) for i in self.terraform_imports)
        b_imports = sorted((i.id, i.to) for i in other.terraform_imports)
        if a_imports != b_imports:
            return False
        if self.secret_arn_map != other.secret_arn_map:
            return False
        if self.ecr_reuse_map != other.ecr_reuse_map:
            return False
        if self.external_iam != other.external_iam:
            return False
        if [p.name for p in self.phases] != [p.name for p in other.phases]:
            return False
        return True


# ---------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------

_STATEFUL_RESOURCE_KINDS = ("EFS", "ALB", "ACM", "VPC", "secret")


def _check_no_destroy(overrides: dict, resource_label: str) -> None:
    if overrides.get("_destroy") is True:
        raise PlanSafetyError(
            f"{resource_label} translator emitted _destroy=True; "
            "this would drop live data. Refusing to build plan."
        )


def _check_live_postgres_imported(
    inv: ResourceInventory,
    imports: list[TerraformImportBlock],
) -> None:
    if inv.efs is None:
        return
    try:
        live = inv.efs.live_postgres_access_point()
    except Exception:
        raise PlanSafetyError(
            "EFS translator did not preserve a live postgres access point"
        )
    ids = {i.id for i in imports}
    if live.ap_id not in ids:
        raise PlanSafetyError(
            f"live postgres access point {live.ap_id} missing from "
            "terraform_imports — would orphan postgres data"
        )


# ---------------------------------------------------------------------
# Phase descriptors
# ---------------------------------------------------------------------


def _build_phases() -> list[MigrationPhase]:
    return [
        MigrationPhase(
            name="validate",
            undo="",
            description="re-discover live state and diff against plan inventory",
        ),
        MigrationPhase(
            name="emit_v2_terraform",
            undo="rm -rf terraform/v2-generated/",
            description="write generated v2 .tf + imports.tf into output_dir",
        ),
        MigrationPhase(
            name="import_state",
            undo="cp live.tfstate.bak live.tfstate",
            description=(
                "terraform plan + apply against sandbox-tfstate-copy first; "
                "operator manually swaps to live state after sandbox green"
            ),
        ),
        MigrationPhase(
            name="services_cutover",
            undo=(
                "for each service: aws ecs update-service "
                "--task-definition <prev_arn>"
            ),
            description="register v2 task defs + update each ECS service rolling",
        ),
        MigrationPhase(
            name="decommission_v1",
            undo="mv archive/rc.yml.<ts> rc.yml",
            description="archive v1 rc.yml; NEVER touch SM/EFS/ALB/ACM",
        ),
    ]


# ---------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------


def build_plan(stack: V1Stack, inv: ResourceInventory) -> MigrationPlan:
    rc_v2_yml, schema_warnings = _translate.translate_v1_to_v2_schema(stack)

    efs_overrides, efs_imports, efs_warnings = _translate.translate_efs_in_place(inv)
    _check_no_destroy(efs_overrides, "EFS")
    _check_live_postgres_imported(inv, efs_imports)

    alb_overrides, alb_imports, alb_warnings = _translate.translate_alb_in_place(inv)
    _check_no_destroy(alb_overrides, "ALB")

    acm_overrides, acm_imports, acm_warnings = _translate.translate_acm_in_place(inv)
    _check_no_destroy(acm_overrides, "ACM")

    rc_secrets, secret_warnings = _translate.translate_secrets_keep_arn(inv)

    vpc_overrides, vpc_imports, vpc_warnings = _translate.translate_vpc_in_place(inv)
    _check_no_destroy(vpc_overrides, "VPC")

    iam_overrides, iam_warnings = _translate.translate_iam_keep_external(inv)
    ecr_overrides, ecr_warnings = _translate.translate_ecr_reuse(inv)

    cluster_overrides, cluster_imports, cluster_warnings = (
        _translate.translate_ecs_cluster_in_place(inv)
    )
    _check_no_destroy(cluster_overrides, "ECS cluster")

    rc_v2_yml["secrets"] = rc_secrets

    secret_arn_map: dict[str, str] = {}
    for s in rc_secrets:
        secret_arn_map[s["name"]] = s["arn"]

    external_iam = {
        "task_execution_role_arn": iam_overrides["task_execution_role_arn"],
        "task_role_arn": iam_overrides["task_role_arn"],
    }

    # Aggregate imports (stable order: by id then to).
    all_imports = (
        efs_imports + alb_imports + acm_imports + vpc_imports + cluster_imports
    )
    all_imports = sorted(all_imports, key=lambda i: (i.id, i.to))

    # Aggregate warnings.
    all_warnings: list[TranslationWarning] = []
    for w in (
        schema_warnings,
        efs_warnings,
        alb_warnings,
        acm_warnings,
        secret_warnings,
        vpc_warnings,
        iam_warnings,
        ecr_warnings,
        cluster_warnings,
    ):
        all_warnings.extend(w)

    blast_radius = {
        "efs_size_gb": (inv.efs.size_bytes // (1024**3)) if inv.efs else 0,
        "secrets_count": len(inv.secrets),
        "running_tasks": (
            inv.ecs_cluster.running_tasks_count if inv.ecs_cluster else 0
        ),
        "dns_managed_externally": (
            inv.route53_zone.apex_managed_externally if inv.route53_zone else False
        ),
    }

    return MigrationPlan(
        rc_v2_yml=rc_v2_yml,
        terraform_imports=all_imports,
        secret_arn_map=secret_arn_map,
        ecr_reuse_map=ecr_overrides.get("ecr_repositories", {}),
        external_iam=external_iam,
        phases=_build_phases(),
        warnings=all_warnings,
        blast_radius=blast_radius,
    )
