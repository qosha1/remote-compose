"""rc lifecycle — run a named hook declared on a service in rc.yml v2."""

from __future__ import annotations

from pathlib import Path

import click


@click.command(name="lifecycle")
@click.argument("hook")
@click.argument("service", required=False, default=None)
@click.pass_context
def lifecycle_cmd(ctx, hook, service):
    """Run a named lifecycle hook declared on a service in rc.yml.

    \b
    Examples:
      rc lifecycle migrate                  # one service declares it
      rc lifecycle migrate django           # disambiguate explicitly
      rc lifecycle createsuperuser
    """
    config_path = ctx.obj.get("config_path")
    path = Path(config_path) if config_path else Path.cwd() / "rc.yml"
    if not path.exists():
        click.echo(f"rc lifecycle: {path} not found.", err=True)
        raise click.exceptions.Exit(1)

    from remote_compose.cli_v2 import (
        build_deploy_context,
        load_rc_yml,
        resolve_provider,
    )

    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        click.echo(
            "rc lifecycle requires rc.yml v2 (declares services[*].lifecycle).",
            err=True,
        )
        raise click.exceptions.Exit(1)

    declarers = [
        name for name, svc in v2.services.items() if hook in (svc.lifecycle or {})
    ]
    if service is not None:
        if service not in v2.services:
            click.echo(f"rc lifecycle: unknown service {service!r}.", err=True)
            raise click.exceptions.Exit(1)
        if service not in declarers:
            click.echo(
                f"rc lifecycle: service {service!r} does not declare hook {hook!r}.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        target = service
    else:
        if not declarers:
            click.echo(
                f"rc lifecycle: no service declares hook {hook!r}. "
                f"Add a `lifecycle.{hook}` block to a service in rc.yml.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        if len(declarers) > 1:
            click.echo(
                f"rc lifecycle: multiple services declare hook {hook!r}: "
                f"{', '.join(declarers)}. Disambiguate: rc lifecycle {hook} <service>.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        target = declarers[0]

    spec = v2.services[target].lifecycle[hook]
    deploy_ctx = build_deploy_context(v2, raw, path)
    provider = resolve_provider(v2)

    # mode 'task' runs a one-off task on the service's task def (gets the task
    # role + SM secrets); 'exec' (default) execs into a running task. Probe +
    # command both honor the hook's mode so secret-dependent hooks (migrate,
    # template sync) work — exec child processes don't get SM secrets.
    def _run(command: list) -> object:
        if spec.mode == "task":
            return provider.run_one_off(deploy_ctx, target, list(command))
        return provider.exec(
            deploy_ctx, target, list(command), interactive=spec.interactive
        )

    # run_once: probe first; skip if probe exits 0.
    if spec.run_once and spec.probe:
        probe_result = _run(list(spec.probe))
        if probe_result.exit_code == 0:
            click.echo(
                f"rc lifecycle: {hook} on {target} already done (probe exit 0); skipping.",
            )
            return

    click.echo(f"rc lifecycle: running {hook} on {target} (mode={spec.mode})...")
    result = _run(list(spec.command))
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    if result.exit_code != 0:
        raise click.exceptions.Exit(result.exit_code)
