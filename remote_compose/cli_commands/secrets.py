"""rc secrets — push secrets from env files referenced in rc.yml.

v2 path uploads one SM secret per file block (JSON-encoded keys, matches
the ECS JSON-key syntax the provider emits in task defs). v1 path uses
Django models + SecretsService.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._dispatchers import _secrets_push_v2
from ._legacy import _bootstrap_django, _load_config


@click.group(name='secrets')
def secrets_group():
    """Manage secrets for the deployment."""
    pass


@secrets_group.command(name='push')
@click.option('--rollout/--no-rollout', default=True,
              help='Force new ECS deployments so running tasks pick up the new secrets.')
@click.pass_context
def secrets_push(ctx, rollout):
    """Push secrets from env files defined in rc.yml."""
    config_path = ctx.obj.get('config_path')

    # v2 path: read rc.yml directly; push one SM secret per file block,
    # uploaded as JSON so ECS JSON-key selectors resolve per-key env vars.
    if _secrets_push_v2(config_path, rollout=rollout):
        return

    # Legacy v1 path below — requires Django models + rc provision.
    config = _load_config(config_path)
    _bootstrap_django(config)

    secrets_files = config.get('secrets', [])
    if not secrets_files:
        click.echo("No secrets files configured in rc.yml.")
        return

    from remote_compose.models import ECSCluster
    from remote_compose.services import SecretsService

    try:
        cluster = ECSCluster.objects.get(name=config['cluster'])
    except ECSCluster.DoesNotExist:
        click.echo(
            f"Error: Cluster '{config['cluster']}' not found. "
            f"Run 'rc provision' first.",
            err=True,
        )
        sys.exit(1)

    svc = SecretsService()

    click.echo(f"\nRemote Compose — pushing secrets for {config['project_name']}\n")

    total = 0
    for env_file in secrets_files:
        path = Path.cwd() / env_file
        if not path.exists():
            click.echo(f"  Warning: {env_file} not found, skipping")
            continue

        click.echo(f"  Pushing {env_file}...", nl=False)
        try:
            arns = svc.push_env_file(cluster=cluster, env_file_path=str(path))
            count = len(arns)
            total += count
            click.echo(f" done ({count} secrets)")
        except Exception as e:
            click.echo(f" FAILED ({e})")

    click.echo(f"\n  Total: {total} secrets pushed")
