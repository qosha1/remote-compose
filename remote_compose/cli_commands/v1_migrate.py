"""rc v1 migrate — port a v1-shaped rc.yml stack to the v2 deploy path.

Two-phase flow (mirrors how rc plan + rc deploy split):

    rc v1 migrate plan <v1_rc.yml>      # discover + build_plan + emit
                                        # writes ./v2-migration/{rc.yml,
                                        # imports.tf, MIGRATION_SUMMARY.md,
                                        # runbook.json}. NO AWS mutation.

    rc v1 migrate apply <v1_rc.yml>     # runs apply phases. Requires
        --sandbox-tfstate <path>        # cp -r copy of live tfstate; refuses
                                        # to run without it.
        [--phase <name>]                # run a single phase (default: full
                                        # sequence with prompts)
        [--auto-approve]                # CI-only

The split makes the destructive surface explicit: `plan` is read-only;
`apply` is the gated mutation step.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from remote_compose.v1_migrate.apply import (
    DecommissionV1Phase,
    EmitV2TerraformPhase,
    ImportStatePhase,
    SandboxStateGuardError,
    ServicesCutoverPhase,
    ValidatePhase,
)
from remote_compose.v1_migrate.discover import DiscoveryError, discover
from remote_compose.v1_migrate.plan import PlanSafetyError, build_plan
from remote_compose.v1_migrate.runbook import (
    RunbookEntry,
    format_undo_runbook,
    write_runbook_json,
)


@click.group(name="v1")
def v1_group():
    """v1 → v2 migration for stacks deployed with rc v1."""


@v1_group.group(name="migrate")
def migrate_group():
    """Port a v1-shaped rc.yml stack to the v2 deploy path."""


# ---------------------------------------------------------------------
# rc v1 migrate plan <v1_rc.yml>
# ---------------------------------------------------------------------

@migrate_group.command(name="plan")
@click.argument(
    "v1_rc_yml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--out", "out_dir",
    default="./v2-migration", show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Where to write rc.yml.v2, imports.tf, MIGRATION_SUMMARY.md, runbook.json.",
)
@click.option(
    "--inventory-snapshot", "inventory_snapshot",
    default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Pre-recorded boto3 inventory JSON (test mode; skips live AWS calls).",
)
@click.option(
    "--aws-profile", "aws_profile",
    default=None,
    help="boto3 profile to use. Defaults to the v1 rc.yml's aws_profile field.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files in --out.")
def migrate_plan(v1_rc_yml, out_dir, inventory_snapshot, aws_profile, force):
    """Discover + build_plan + emit. NO AWS mutation."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "MIGRATION_SUMMARY.md"
    rc_yml_v2_path = out_dir / "rc.yml.v2"
    if not force and (summary_path.exists() or rc_yml_v2_path.exists()):
        click.echo(
            f"refusing to overwrite existing files in {out_dir} — "
            "re-run with --force.", err=True,
        )
        raise click.exceptions.Exit(1)

    aws_session = None
    if inventory_snapshot is None:
        try:
            import boto3
        except ImportError:
            click.echo("boto3 required for live discovery (pip install boto3)", err=True)
            raise click.exceptions.Exit(1)
        # Parse the v1 yaml first to grab region+profile, then build session.
        from remote_compose.v1_migrate.discover import V1Stack
        try:
            stack_pre = V1Stack.from_yaml(v1_rc_yml)
        except DiscoveryError as exc:
            click.echo(f"rc v1 migrate plan: {exc}", err=True)
            raise click.exceptions.Exit(1)
        aws_session = boto3.Session(
            region_name=stack_pre.region,
            profile_name=aws_profile or stack_pre.aws_profile or None,
        )

    try:
        stack, inv = discover(
            rc_v1_yml_path=v1_rc_yml,
            aws_session=aws_session,
            inventory_snapshot=inventory_snapshot,
        )
    except DiscoveryError as exc:
        click.echo(f"rc v1 migrate plan: {exc}", err=True)
        raise click.exceptions.Exit(1)

    try:
        plan = build_plan(stack, inv)
    except PlanSafetyError as exc:
        click.echo(f"rc v1 migrate plan: SAFETY ABORT: {exc}", err=True)
        raise click.exceptions.Exit(3)

    # Emit terraform + rc.yml.v2 + summary.
    EmitV2TerraformPhase(plan=plan, output_dir=out_dir).run()
    summary_path.write_text(plan.render_summary_md())

    # Pre-fill runbook with phase descriptors (entries get filled by `apply`).
    runbook_seed = [
        {
            "phase": p.name, "started_at": "", "finished_at": None,
            "ok": False, "undo_command": p.undo, "details": "",
        }
        for p in plan.phases
    ]
    (out_dir / "runbook.json").write_text(json.dumps(runbook_seed, indent=2))

    click.echo(f"\nrc v1 migrate plan — {stack.cluster} ({stack.region})")
    click.echo(f"  source:    {v1_rc_yml}")
    click.echo(f"  imports:   {len(plan.terraform_imports)}")
    click.echo(f"  secrets:   {len(plan.secret_arn_map)}")
    click.echo(f"  warnings:  {len(plan.warnings)}")
    click.echo(f"\n  wrote {out_dir/'main.tf'}")
    click.echo(f"  wrote {out_dir/'imports.tf'}")
    click.echo(f"  wrote {rc_yml_v2_path}")
    click.echo(f"  wrote {summary_path}")
    click.echo(f"  wrote {out_dir/'runbook.json'}")
    click.echo(
        "\n  Next: review MIGRATION_SUMMARY.md, then\n"
        f"  `rc v1 migrate apply {v1_rc_yml} --out {out_dir}`"
    )


# ---------------------------------------------------------------------
# rc v1 migrate apply <v1_rc.yml>
# ---------------------------------------------------------------------

@migrate_group.command(name="apply")
@click.argument(
    "v1_rc_yml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--out", "out_dir",
    default="./v2-migration", show_default=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Output dir from `rc v1 migrate plan`.",
)
@click.option(
    "--sandbox-tfstate", "sandbox_tfstate",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Path to a cp -r copy of the live tfstate. Required only for the "
        "opt-in ImportStatePhase (--phase import_state). The default boto3-only "
        "cutover (validate -> services_cutover -> decommission_v1) does not "
        "need this since v1 prod is not terraform-managed."
    ),
)
@click.option(
    "--inventory-snapshot", "inventory_snapshot",
    default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Pre-recorded inventory JSON (test mode).",
)
@click.option(
    "--phase", "single_phase",
    default=None,
    type=click.Choice([
        "validate", "emit_v2_terraform", "import_state",
        "services_cutover", "decommission_v1",
    ]),
    help=(
        "Run a single named phase. Default: validate -> services_cutover -> "
        "decommission_v1 (boto3-only). The terraform phases (emit_v2_terraform, "
        "import_state) are opt-in."
    ),
)
@click.option("--auto-approve", is_flag=True, help="Skip per-phase prompts (CI-only).")
def migrate_apply(
    v1_rc_yml, out_dir, sandbox_tfstate, inventory_snapshot, single_phase,
    auto_approve,
):
    """Run the apply phases. Default: validate -> services_cutover ->
    decommission_v1 (boto3-only). Each phase prompts unless --auto-approve.
    """
    out_dir = Path(out_dir).resolve()

    aws_session = None
    if inventory_snapshot is None:
        try:
            import boto3
        except ImportError:
            click.echo("boto3 required (pip install boto3)", err=True)
            raise click.exceptions.Exit(1)
        from remote_compose.v1_migrate.discover import V1Stack
        stack_pre = V1Stack.from_yaml(v1_rc_yml)
        aws_session = boto3.Session(
            region_name=stack_pre.region,
            profile_name=stack_pre.aws_profile or None,
        )

    try:
        stack, inv = discover(
            rc_v1_yml_path=v1_rc_yml,
            aws_session=aws_session,
            inventory_snapshot=inventory_snapshot,
        )
        plan = build_plan(stack, inv)
    except (DiscoveryError, PlanSafetyError) as exc:
        click.echo(f"rc v1 migrate apply: {exc}", err=True)
        raise click.exceptions.Exit(3)

    ecs_client = aws_session.client("ecs") if aws_session else None
    phase_factories = {
        "validate": lambda: ValidatePhase(plan=plan, aws_session=aws_session),
        "emit_v2_terraform": lambda: EmitV2TerraformPhase(plan=plan, output_dir=out_dir),
        "import_state": lambda: ImportStatePhase(
            plan=plan, output_dir=out_dir, sandbox_tfstate=sandbox_tfstate,
        ),
        "services_cutover": lambda: ServicesCutoverPhase(
            plan=plan, ecs_client=ecs_client,
        ),
        "decommission_v1": lambda: DecommissionV1Phase(
            plan=plan, aws_session=aws_session,
            v1_rc_yml_path=v1_rc_yml,
            archive_dir=out_dir / "archive",
        ),
    }

    # Default sequence: boto3-only cutover (no terraform phases).
    # v1 prod stacks are deployed imperatively, so there's no
    # terraform state to import. The terraform phases stay available
    # via --phase for future "v2 takes over GitOps" work.
    DEFAULT_BOTO3_SEQUENCE = ["validate", "services_cutover", "decommission_v1"]

    if single_phase:
        sequence = [single_phase]
    else:
        sequence = DEFAULT_BOTO3_SEQUENCE
        click.echo(
            "Default sequence: " + " -> ".join(sequence) + "\n"
            "(boto3-only cutover — no terraform state mutation. To run "
            "the optional terraform phases, use --phase emit_v2_terraform "
            "or --phase import_state explicitly.)"
        )

    # Guard: import_state requires --sandbox-tfstate.
    if "import_state" in sequence and sandbox_tfstate is None:
        click.echo(
            "import_state requires --sandbox-tfstate <path-to-cp-r-of-live-tfstate>",
            err=True,
        )
        raise click.exceptions.Exit(3)

    entries: list[RunbookEntry] = []
    for name in sequence:
        if not auto_approve:
            click.confirm(f"\nRun phase '{name}'?", abort=True)
        entry = RunbookEntry.begin(
            phase=name,
            undo_command=next(
                (p.undo for p in plan.phases if p.name == name), "",
            ),
        )
        try:
            result = phase_factories[name]().run()
        except SandboxStateGuardError as exc:
            entry.finish(ok=False, details=f"SandboxStateGuardError: {exc}")
            entries.append(entry)
            click.echo(f"  ABORT: {exc}", err=True)
            click.echo(format_undo_runbook(entries), err=True)
            write_runbook_json(entries, out_dir / "runbook.json")
            raise click.exceptions.Exit(3)
        entry.finish(ok=result.ok, details=result.details)
        entries.append(entry)
        click.echo(
            f"  [{name}] {'OK' if result.ok else 'FAIL'} "
            f"({result.elapsed_sec:.2f}s): {result.details[:200]}"
        )
        if not result.ok:
            click.echo(format_undo_runbook(entries), err=True)
            write_runbook_json(entries, out_dir / "runbook.json")
            raise click.exceptions.Exit(2)

    write_runbook_json(entries, out_dir / "runbook.json")
    click.echo(f"\nrc v1 migrate apply — done. runbook: {out_dir/'runbook.json'}")


