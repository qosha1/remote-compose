"""rc status / restart / logs — read-only + nudge service operations."""

from __future__ import annotations

import sys

import click

from ._legacy import _bootstrap_django, _load_config


@click.command(name='status')
@click.pass_context
def status_cmd(ctx):
    """Show service status table."""
    from remote_compose.cli_v2 import dispatch_if_v2
    if dispatch_if_v2(ctx.obj.get('config_path'), 'status'):
        return

    config = _load_config(ctx.obj.get('config_path'))
    _bootstrap_django(config)

    project_name = config['project_name']
    cluster_name = config['cluster']

    from remote_compose.models import ECSCluster, ECSService as ECSServiceModel

    try:
        cluster = ECSCluster.objects.get(name=cluster_name)
    except ECSCluster.DoesNotExist:
        click.echo(
            f"Error: Cluster '{cluster_name}' not found. Run 'rc provision' first.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"\nRemote Compose — {project_name} ({cluster.aws_region})\n")

    services = ECSServiceModel.objects.filter(cluster=cluster)

    if not services.exists():
        click.echo("  No services deployed yet.")
        return

    from remote_compose.services import ECSDeploymentService
    deployment_svc = ECSDeploymentService()

    header = f"  {'SERVICE':<24} {'STATUS':<12} {'TASKS':<8} {'TYPE':<16}"
    click.echo(header)
    click.echo(f"  {'-' * 60}")

    for svc in services:
        svc_type = ''
        svc_config = config.get('services', {}).get(
            svc.name.replace(f"{project_name}-", ''), {}
        )
        svc_type = svc_config.get('type', '')

        try:
            status_info = deployment_svc.get_service_status(svc)
            status_str = status_info.get('status', 'unknown')
            running = status_info.get('running_count', 0)
            desired = status_info.get('desired_count', 0)
            tasks_str = f"{running}/{desired}"
        except Exception:
            status_str = str(svc.status) if svc.status else 'unknown'
            tasks_str = f"{svc.running_count or 0}/{svc.desired_count or 0}"

        click.echo(f"  {svc.name:<24} {status_str:<12} {tasks_str:<8} {svc_type:<16}")

    try:
        if cluster.load_balancer:
            click.echo(f"\n  ALB: {cluster.load_balancer.alb_dns_name}")
    except Exception:
        pass


@click.command(name='restart')
@click.argument('service', required=False)
@click.pass_context
def restart_cmd(ctx, service):
    """Force a new deployment for all or a specific service."""
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

    if service:
        svc = services.filter(name=service).first() or \
              services.filter(name=f"{project_name}-{service}").first()
        if not svc:
            click.echo(f"Error: Service '{service}' not found.", err=True)
            sys.exit(1)
        targets = [svc]
    else:
        targets = list(services)

    if not targets:
        click.echo("No services to restart.")
        return

    click.echo(f"\nRemote Compose — restarting {'all services' if not service else service}\n")

    for svc in targets:
        click.echo(f"  Restarting {svc.name}...", nl=False)
        try:
            ecs_svc.update_service(svc, force_new_deployment=True)
            click.echo(" done")
        except Exception as e:
            click.echo(f" FAILED ({e})")

    click.echo("\n  Waiting for stability...", nl=False)
    for svc in targets:
        try:
            ecs_svc.wait_for_service_stable(svc, timeout=300)
        except Exception:
            pass
    click.echo(" done")


@click.command(name='logs')
@click.argument('service')
@click.option('-n', '--lines', default=50, help='Number of log lines (default: 50)')
@click.pass_context
def logs_cmd(ctx, service, lines):
    """Show recent deployment logs for a service."""
    config = _load_config(ctx.obj.get('config_path'))
    _bootstrap_django(config)

    project_name = config['project_name']

    from remote_compose.models import ECSCluster, Deployment, DeploymentLog

    try:
        cluster = ECSCluster.objects.get(name=config['cluster'])
    except ECSCluster.DoesNotExist:
        click.echo(f"Error: Cluster '{config['cluster']}' not found.", err=True)
        sys.exit(1)

    deployments = Deployment.objects.filter(
        project_name=project_name,
    ).order_by('-started_at')[:5]

    if not deployments.exists():
        click.echo(f"No deployment logs found for {project_name}.")
        return

    click.echo(f"\nRecent deployment logs for {project_name}\n")

    for dep in deployments:
        status_str = dep.status if dep.status else 'unknown'
        started = dep.started_at.strftime('%Y-%m-%d %H:%M:%S') if dep.started_at else '?'
        click.echo(f"  [{started}] {status_str} — {dep.version or 'no version'}")

        log_entries = DeploymentLog.objects.filter(
            deployment=dep,
        ).order_by('-created_at')[:lines]

        for entry in reversed(list(log_entries)):
            ts = entry.created_at.strftime('%H:%M:%S') if entry.created_at else ''
            click.echo(f"    {ts}  {entry.message}")

        click.echo()
