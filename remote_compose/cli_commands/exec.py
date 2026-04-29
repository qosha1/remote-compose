"""rc exec — run a command inside a running ECS task.

v2 path goes through Provider.exec (sentinel-bracketed stdout, real
exit code). v1 fallback shells out to `aws ecs execute-command` directly.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import click

from ._dispatchers import _exec_v2
from ._legacy import _bootstrap_django, _load_config


@click.command(
    name='exec',
    context_settings=dict(ignore_unknown_options=True),
)
@click.argument('service')
@click.argument('command', nargs=-1, type=click.UNPROCESSED)
@click.option('--container', default=None, help='Container name (default: first container)')
@click.pass_context
def exec_cmd(ctx, service, command, container):
    """Execute a command in a running container of a service.

    \b
    Examples:
      rc exec django -- python manage.py migrate
      rc exec django -- /bin/bash
      rc exec django --container sidecar -- /bin/sh
    """
    if not command:
        click.echo("Error: No command specified. Use -- before the command.", err=True)
        click.echo("Example: rc exec django -- python manage.py shell", err=True)
        sys.exit(1)

    if not shutil.which('session-manager-plugin'):
        click.echo(
            "Error: session-manager-plugin is not installed.\n"
            "Install it: https://docs.aws.amazon.com/systems-manager/latest/"
            "userguide/session-manager-working-with-install-plugin.html",
            err=True,
        )
        sys.exit(1)

    if _exec_v2(ctx.obj.get('config_path'), service, list(command)):
        return

    config = _load_config(ctx.obj.get('config_path'))
    _bootstrap_django(config)

    project_name = config['project_name']

    from remote_compose.models import ECSCluster, ECSService as ECSServiceModel
    from remote_compose.services import ECSService

    try:
        cluster = ECSCluster.objects.get(name=config['cluster'])
    except ECSCluster.DoesNotExist:
        click.echo(f"Error: Cluster '{config['cluster']}' not found.", err=True)
        sys.exit(1)

    ecs_svc = ECSService()

    services = ECSServiceModel.objects.filter(cluster=cluster)
    svc = services.filter(name=service).first() or \
          services.filter(name=f"{project_name}-{service}").first()

    if not svc:
        available = [s.name for s in services]
        click.echo(f"Error: Service '{service}' not found.", err=True)
        if available:
            click.echo(f"Available services: {', '.join(available)}", err=True)
        sys.exit(1)

    try:
        task_arns = ecs_svc.list_tasks(cluster, service_name=svc.name)
    except Exception as e:
        click.echo(f"Error listing tasks: {e}", err=True)
        sys.exit(1)

    if not task_arns:
        click.echo(f"Error: No running tasks for service '{svc.name}'.", err=True)
        sys.exit(1)

    task_arn = task_arns[0]

    if not container:
        try:
            tasks = ecs_svc.describe_tasks(cluster, [task_arn])
            if tasks and tasks[0].get('containers'):
                container = tasks[0]['containers'][0]['name']
            else:
                click.echo("Error: Could not determine container name.", err=True)
                sys.exit(1)
        except Exception as e:
            click.echo(f"Error describing task: {e}", err=True)
            sys.exit(1)

    cmd_str = ' '.join(command)
    cluster_ref = cluster.aws_cluster_arn or cluster.aws_cluster_name

    click.echo(f"Connecting to {svc.name} ({container})...")

    aws_cmd = [
        'aws', 'ecs', 'execute-command',
        '--cluster', cluster_ref,
        '--task', task_arn,
        '--container', container,
        '--interactive',
        '--command', cmd_str,
    ]

    region = cluster.aws_region
    if region:
        aws_cmd.extend(['--region', region])

    result = subprocess.run(aws_cmd)
    sys.exit(result.returncode)
