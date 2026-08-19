"""rc preflight — report every missing deploy prerequisite at once.

rc-g3jy. Moving a stack from `--no-state` onto full terraform cost three
failed production deploys in a row, each surfacing exactly one missing
prerequisite. This command renders the terraform, derives the IAM actions it
will need from what it just rendered, and checks the whole list — binary,
state access, lock, permissions — in one pass.
"""

from __future__ import annotations

from pathlib import Path

import click


@click.command(name="preflight")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the report as JSON instead of a table.",
)
@click.pass_context
def preflight_cmd(ctx, as_json):
    """Check every deploy prerequisite before touching anything."""
    from ..cli_v2 import build_deploy_context, load_rc_yml, resolve_provider
    from ..config._schema_types import ConfigError

    path = Path(ctx.obj.get("config_path") or "") or Path.cwd() / "rc.yml"
    if not path.exists():
        path = Path.cwd() / "rc.yml"
    if not path.exists():
        click.echo("No rc.yml found. `rc preflight` requires a v2 config.", err=True)
        raise click.exceptions.Exit(1)

    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim to the user
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        click.echo(
            "rc preflight requires a rc.yml v2 config. Run `rc migrate` first.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    try:
        deploy_ctx = build_deploy_context(v2, raw, path, require_compose_file=True)
    except ConfigError as exc:
        click.echo(f"rc.yml at {path}: {exc}", err=True)
        raise click.exceptions.Exit(1)

    provider = resolve_provider(v2)
    if not hasattr(provider, "deploy_preflight"):
        click.echo(f"provider {v2.provider!r} has no preflight checks.", err=True)
        raise click.exceptions.Exit(1)

    # preflight() resolves aws_profile (rc-rigk) before anything reads
    # credentials; emit_terraform then produces the .tf the IAM action set is
    # derived from.
    provider.preflight(deploy_ctx)
    out_dir = Path(deploy_ctx.working_dir) / "terraform"
    provider.emit_terraform(deploy_ctx, out_dir)

    from ..provider.base import ProviderConfigError

    try:
        report = provider.deploy_preflight(deploy_ctx, out_dir, force=True)
    except ProviderConfigError as exc:
        # deploy_preflight raises on the deploy path so a broken principal
        # stops the deploy. Here the report IS the output, so render it and
        # exit non-zero rather than re-raising as a traceback.
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1)

    if report is None:
        click.echo("  Preflight did not run (RC_SKIP_PREFLIGHT set?).")
        return

    if as_json:
        import json

        click.echo(
            json.dumps(
                {
                    "ok": report.ok,
                    "checks": [
                        {
                            "name": c.name,
                            "status": c.status,
                            "detail": c.detail,
                            "remedy": c.remedy,
                        }
                        for c in report.checks
                    ],
                    "missing_actions": report.missing_actions,
                    "unmodeled_resource_types": report.unmodeled_resource_types,
                },
                indent=2,
            )
        )
        return

    click.echo(f"\n  Preflight for {v2.project}:\n")
    click.echo(report.render_table())
    click.echo("\n  All prerequisites satisfied.")
