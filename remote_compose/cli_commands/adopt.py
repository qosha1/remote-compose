"""rc adopt — bring a live AWS stack under terraform management.

Two modes:
  rc adopt                                 default: from-scratch (v1→v2 cutover)
  rc adopt --from-local-tfstate <path>     copy existing local state to the
                                            configured s3 backend in one shot

Closes the gap left by `rc v1 migrate apply` when the boto3-only cutover
skips the optional `import_state` phase: after `rc adopt`, terraform
state matches live AWS and `rc deploy` becomes a no-op apply + image
rebuild + force-roll.
"""

from __future__ import annotations

from pathlib import Path

import click


@click.command(name="adopt")
@click.option(
    "--from-local-tfstate",
    "from_local_tfstate",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to an existing terraform.tfstate to copy into the "
    "configured s3 backend. Use when migrating a stack that "
    "was previously deployed under backend.type=local.",
)
@click.pass_context
def adopt_cmd(ctx, from_local_tfstate):
    """Bring a live AWS stack under terraform management.

    \b
    Default: walks AWS via boto3, generates import addresses, runs
    terraform import for every resource. Idempotent — second run is
    a no-op. Partial failures preserve consistency; re-run to retry
    just the failed set.

    \b
    With --from-local-tfstate: copies the local state file into the
    configured s3 backend in one shot, then verifies via terraform
    plan (must report no diff).
    """
    from remote_compose.state_backend.adopt import adopt_v1_to_v2

    config_path = ctx.obj.get("config_path") or "rc.yml"
    rc_path = Path(config_path).resolve()
    if not rc_path.exists():
        click.echo(f"Error: {rc_path} not found.", err=True)
        raise click.exceptions.Exit(1)

    if from_local_tfstate:
        # TODO: implement state-copy mode (5h8 follow-up). For v1 of
        # this feature, default mode is the priority.
        raise click.ClickException(
            "--from-local-tfstate is not yet implemented; "
            "drop the flag to use default from-scratch adoption."
        )

    working_dir = rc_path.parent / "terraform" / "ecs"
    working_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"\nrc adopt — {rc_path}")
    click.echo(f"  working_dir: {working_dir}")
    click.echo("  walking AWS + generating imports...")

    result = adopt_v1_to_v2(rc_yml_path=rc_path, working_dir=working_dir)

    click.echo(f"\n  imported: {result.imported}")
    click.echo(f"  skipped (already in state): {result.skipped}")
    click.echo(f"  failed: {len(result.failed)}")
    click.echo(f"  duration: {result.duration_s:.1f}s")

    if result.failed:
        click.echo("\n  failures:")
        for address, rid, err in result.failed:
            click.echo(f"    {address} ({rid}): {err}")
        click.echo(
            "\n  Re-run `rc adopt` to retry only the failed set "
            "(already-imported resources are no-ops).",
            err=True,
        )
        raise click.exceptions.Exit(1)

    click.echo("\n  Stack is now under terraform management.")
    click.echo("  Run `rc plan` to verify no drift, then `rc deploy` " "as needed.")
