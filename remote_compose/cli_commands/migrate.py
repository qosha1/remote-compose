"""rc migrate — convert a v1 rc.yml to v2 schema."""

from __future__ import annotations

import click


@click.command(name="migrate")
@click.option(
    "--in",
    "in_path",
    default="rc.yml",
    show_default=True,
    help="Path to rc.yml v1 input.",
)
@click.option(
    "--out",
    "out_path",
    default="rc.v2.yml",
    show_default=True,
    help="Path to write rc.yml v2 output.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Write output even if unmigratable fields are present.",
)
def migrate_cmd(in_path, out_path, force):
    """Convert a v1 rc.yml to v2 schema."""
    import yaml
    from remote_compose.config import v1_schema
    from remote_compose.config.migrate import migrate as _migrate

    raw = v1_schema.load(in_path)
    if not v1_schema.is_v1(raw):
        click.echo(f"{in_path} is already v2; nothing to migrate.")
        return

    result = _migrate(raw, strict=False)

    for w in result.warnings:
        click.echo(f"warning: {w}", err=True)
    for u in result.unmigratable:
        click.echo(f"unmigratable: {u}", err=True)

    if result.unmigratable and not force:
        click.echo(
            f"refusing to write {out_path}: {len(result.unmigratable)} "
            f"unmigratable field(s). Re-run with --force to write anyway.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    with open(out_path, "w") as f:
        yaml.safe_dump(result.v2, f, sort_keys=False)
    click.echo(f"Wrote {out_path} (version 2).")
