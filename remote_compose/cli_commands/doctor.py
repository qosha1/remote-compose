"""rc doctor + rc install — preflight + dependency repair."""

from __future__ import annotations

import click


@click.command(name='doctor')
@click.option('--fix', is_flag=True,
              help='Attempt to install/upgrade missing deps via the platform package manager.')
def doctor_cmd(fix):
    """Check that terraform/docker/python/AWS are set up correctly."""
    from remote_compose import doctor
    report = doctor.run()
    click.echo(report.render_table())
    if not report.ok and not fix:
        click.echo("\n  Some hard requirements are missing. Re-run with --fix "
                   "to attempt repair, or `rc install`.", err=True)
        raise click.exceptions.Exit(1)
    if fix and not report.ok:
        click.echo("\n  Attempting fixes...\n")
        outcomes = doctor.apply_fixes(report)
        for name, ok, detail in outcomes:
            mark = "✓" if ok else "✗"
            click.echo(f"    {mark} {name}: {detail}")
        click.echo("\n  Re-running checks...\n")
        report = doctor.run()
        click.echo(report.render_table())
        if not report.ok:
            raise click.exceptions.Exit(1)


@click.command(name='install')
@click.pass_context
def install_cmd(ctx):
    """Install/upgrade every prerequisite (alias for `rc doctor --fix`)."""
    ctx.invoke(doctor_cmd, fix=True)
