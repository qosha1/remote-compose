"""rc run — run a one-off command as a fresh ECS task.

Unlike ``rc exec`` (``aws ecs execute-command`` into a running task, whose
child process does NOT inherit the task's Secrets-Manager secrets), ``rc run``
launches a new task from the service's task definition — so the command gets
the task role AND the SM secrets injected by ECS. This is the right primitive
for secret-dependent management commands (Django/Rails migrate, template
sync, ...). v2-only.
"""

from __future__ import annotations

import click


@click.command(
    name="run",
    context_settings=dict(ignore_unknown_options=True),
)
@click.argument("service")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--no-wait",
    is_flag=True,
    help="Launch the task and print its ARN without waiting for it to finish.",
)
@click.option(
    "--timeout",
    default=900,
    show_default=True,
    type=int,
    help="Seconds to wait for the one-off task to stop.",
)
@click.option(
    "--container",
    default=None,
    help="Container to run in (default: the one named like the service).",
)
@click.pass_context
def run_cmd(ctx, service, command, no_wait, timeout, container):
    """Run COMMAND as a one-off ECS task on SERVICE's task definition.

    \b
    Unlike `rc exec` (execute-command into a running task — whose child
    process does NOT inherit the task's Secrets-Manager secrets), `rc run`
    launches a fresh task from the service's task def, so the command gets
    the task role AND the SM secrets. Use it for secret-dependent management
    commands. Streams the task's logs, waits for it to stop, and exits with
    the command's real exit code.

    \b
    Examples:
      rc run django -- python manage.py migrate
      rc run django -- python manage.py sync_workflow_templates
      rc run django --no-wait -- python manage.py some_long_job
    """
    if not command:
        click.echo("Error: no command. Use -- before the command.", err=True)
        click.echo("Example: rc run django -- python manage.py migrate", err=True)
        raise click.exceptions.Exit(2)

    from remote_compose.cli_v2 import build_deploy_context, resolve_provider

    from ._dispatchers import _load_v2_if_present

    loaded = _load_v2_if_present(ctx.obj.get("config_path"), strict=True)
    if loaded is None:
        click.echo(
            "rc run: requires an rc.yml v2 config (provider-managed stack).",
            err=True,
        )
        raise click.exceptions.Exit(1)
    path, raw, v2 = loaded

    if service not in v2.services:
        click.echo(
            f"rc run: service {service!r} not in rc.yml services. "
            f"Available: {', '.join(sorted(v2.services))}",
            err=True,
        )
        raise click.exceptions.Exit(1)

    deploy_ctx = build_deploy_context(v2, raw, path)
    provider = resolve_provider(v2)

    result = provider.run_one_off(
        deploy_ctx,
        service,
        list(command),
        wait=not no_wait,
        timeout=timeout,
        container=container,
    )
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    if result.exit_code != 0:
        raise click.exceptions.Exit(result.exit_code)
