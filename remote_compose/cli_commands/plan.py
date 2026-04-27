"""rc plan — show terraform plan for the current rc.yml v2 config."""

from __future__ import annotations

import click


@click.command(name='plan')
@click.pass_context
def plan_cmd(ctx):
    """Show terraform plan for the current rc.yml v2 config."""
    from remote_compose.cli_v2 import dispatch_if_v2
    if dispatch_if_v2(ctx.obj.get('config_path'), 'plan'):
        return
    click.echo("rc plan requires a rc.yml v2 config. Run `rc migrate` first.",
               err=True)
    raise click.exceptions.Exit(1)
