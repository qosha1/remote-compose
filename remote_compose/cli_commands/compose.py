"""rc compose — docker-compose interop helpers.

Today: `rc compose import` scaffolds a starter rc.yml from an existing
docker-compose.yml. The auto-import path makes services[] OPTIONAL —
compose drives the deploy set with cpu=256/memory=512 defaults.
"""

from __future__ import annotations

from pathlib import Path

import click


@click.group(name="compose")
def compose_group():
    """docker-compose interop helpers."""
    pass


@compose_group.command(name="import")
@click.option(
    "--from",
    "compose_file",
    default="./docker-compose.yml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the source docker-compose.yml.",
)
@click.option(
    "--out",
    "out_path",
    default="./rc.yml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Where to write the scaffolded rc.yml.",
)
@click.option(
    "--project",
    "project_name",
    default=None,
    help="rc.yml v2 project field. Defaults to the parent dir name "
    "of the compose file.",
)
@click.option(
    "--exclude",
    "exclude_csv",
    default=None,
    help="Comma-separated list of compose service names to drop "
    "from the deploy set (lands under compose.exclude in "
    "the output). Useful for dev-only sidecars: e.g. "
    "--exclude=ngrok,docs-builder,eval-app.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing rc.yml at --out.")
def compose_import(compose_file, out_path, project_name, exclude_csv, force):
    """Scaffold a starter rc.yml v2 from an existing docker-compose.yml.

    \b
    Reads docker-compose.yml and writes an rc.yml shell with project +
    provider + provider_config defaults, plus per-service overrides for
    things we can detect (public ports, db services with volume hints,
    worker-shaped names). env_file refs surface as commented stubs in the
    secrets: block.

    \b
    The auto-import path makes services[] OPTIONAL — compose drives the
    deploy set with cpu=256/memory=512 defaults. Add a service entry only
    to OVERRIDE those defaults.

    \b
    Examples:
      rc compose import
      rc compose import --from docker-compose.prod.yml --project myapp
      rc compose import --exclude=ngrok,eval-app,docs
    """
    from remote_compose.compose_import import scaffold_rc_yml

    src = Path(compose_file).resolve()
    dst = Path(out_path).resolve()

    if dst.exists() and not force:
        click.echo(
            f"refusing to overwrite {dst} — re-run with --force to replace.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    excluded = (
        [s.strip() for s in exclude_csv.split(",") if s.strip()]
        if exclude_csv
        else None
    )
    try:
        rc_yml = scaffold_rc_yml(src, project=project_name, exclude=excluded)
    except ValueError as exc:
        click.echo(f"rc compose import: {exc}", err=True)
        raise click.exceptions.Exit(1)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rc_yml)

    click.echo("\nrc compose import")
    click.echo(f"  source:  {src}")
    click.echo(f"  wrote:   {dst}")
    click.echo(
        f"\n  Next: edit {dst.name} (provider region, secrets), " f"then `rc plan`."
    )
