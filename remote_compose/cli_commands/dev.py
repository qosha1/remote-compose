"""rc dev — hot-reload iteration on a deployed dev-mode stack (rc-e5u.45.9)."""

from __future__ import annotations

import sys
from pathlib import Path

import click


@click.group(name='dev')
def dev_group():
    """Hot-reload iteration on a deployed dev-mode stack.

    \b
    Workflow:
      1. Add `dev_volumes:` entries to services in rc.yml.
      2. `rc up --dev` — deploys with EFS-backed bind mounts at the
         declared paths. The container will start empty until you push.
      3. `rc dev push <service>` — streams local source into the EFS
         mount via `aws ecs execute-command`. Django runserver et al.
         auto-reload on file change.
      4. `rc dev push --watch <service>` — keeps streaming on every
         local edit (debounced ~250ms).
    """


@dev_group.command(name='push')
@click.argument('service', required=False)
@click.option('--watch', 'watch', is_flag=True,
              help='Watch local sources and re-push on every change '
                   '(debounced ~250ms). Requires fswatch (macOS) or '
                   'inotifywait (Linux).')
@click.pass_context
def dev_push_cmd(ctx, service, watch):
    """Push local dev_volume source(s) to a running task via EFS.

    With no SERVICE arg, pushes EVERY service that declares dev_volumes.
    With --watch, runs forever, re-pushing on every local edit.
    """
    from remote_compose.dev_push import (
        DevPushError, push_all, watch_and_push,
    )

    config_path = ctx.obj.get('config_path') or 'rc.yml'
    rc_path = Path(config_path)
    if not rc_path.exists():
        click.echo(f"Error: {rc_path} not found.", err=True)
        sys.exit(1)

    def _progress(msg: str) -> None:
        click.echo(msg)

    try:
        if watch:
            watch_and_push(rc_path, service, progress=_progress)
        else:
            results = push_all(rc_path, service, progress=_progress)
            total = sum(r["elapsed_s"] for r in results)
            click.echo(
                f"\n  pushed {len(results)} dev_volume(s) in {total:.1f}s."
            )
    except DevPushError as exc:
        click.echo(f"\n  rc dev push: {exc}", err=True)
        sys.exit(1)
