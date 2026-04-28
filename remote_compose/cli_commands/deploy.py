"""rc deploy — build images, push to ECR, deploy all services.

v1 imperative path uses Django models + the deployment_pipeline framework.
v2 path goes through cli_v2.dispatch_if_v2.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click

from ._legacy import (
    _bootstrap_django,
    _load_config,
    _resolve_compose_path,
    _write_service_config,
)


def _print_service_table(context):
    """Print a service status table from pipeline context."""
    if not context.ecs_services:
        return

    header = f"  {'SERVICE':<24} {'STATUS':<12} {'TASKS':<8} {'TYPE':<16}"
    click.echo(header)
    click.echo(f"  {'-' * 60}")

    service_resources = context.service_resources or {}

    for svc_name in context.service_order or context.ecs_services.keys():
        svc = context.ecs_services.get(svc_name)
        if not svc:
            continue

        status_str = str(svc.status) if hasattr(svc, 'status') else 'unknown'
        running = getattr(svc, 'running_count', 0) or 0
        desired = getattr(svc, 'desired_count', 0) or 0
        tasks_str = f"{running}/{desired}"

        svc_res = service_resources.get(svc_name, {})
        svc_type = svc_res.get('type', '')

        click.echo(f"  {svc_name:<24} {status_str:<12} {tasks_str:<8} {svc_type:<16}")


@click.command(name='deploy')
@click.option('--no-build', is_flag=True, help='Deploy without rebuilding images')
@click.option('--dry-run', is_flag=True, help='Preview what would happen')
@click.option('--tag', default=None,
              help='Image tag. When set + the tag already exists in ECR, '
                   'rc skips docker build and re-tags the existing image as '
                   ':latest (instant rollback / pinned-image deploy). When '
                   'unset, builds with :latest as today.')
@click.option('--code-only', is_flag=True, help='Deploy only code services (skip infrastructure)')
@click.option('--services', 'selected_services', default=None, help='Comma-separated services to deploy')
@click.option(
    '--ttl', 'ttl', default=None,
    help='Mark deployment ephemeral; auto-reapable via `rc reap` after this '
         'duration (e.g. 5m, 2h, 1d, 4h30m). v2 rc.yml only.',
)
@click.option(
    '--dev', 'dev_mode', is_flag=True,
    help='Dev-mode deploy (rc-e5u.45.8): provisions an EFS file system + '
         'access points for any service declaring dev_volumes, mounted '
         'into the task at the declared paths so `rc dev push` can stream '
         'local source for sub-second iteration. v2 rc.yml only.',
)
@click.option(
    '--reconcile', 'reconcile', is_flag=True,
    help='Auto-detect services running on stale task def revisions and '
         'force-roll only those. Useful after a partial-failure deploy '
         'left some services stuck on older code. v2 rc.yml only. '
         'Mutually exclusive with --services / --tag. (rc-e5u.44.24)',
)
@click.pass_context
def deploy_cmd(ctx, no_build, dry_run, tag, code_only, selected_services, ttl, dev_mode, reconcile):
    """Build images, push to ECR, and deploy all services."""
    services_list = None
    if selected_services:
        services_list = [s.strip() for s in selected_services.split(',') if s.strip()]
    if reconcile:
        if services_list or tag:
            click.echo(
                "Error: --reconcile is mutually exclusive with --services / --tag.",
                err=True,
            )
            sys.exit(1)
        from remote_compose.cli_v2 import (
            build_deploy_context, load_rc_yml, resolve_provider,
        )
        rc_path = Path(ctx.obj.get('config_path') or 'rc.yml')
        if not rc_path.exists():
            click.echo(f"Error: {rc_path} not found.", err=True)
            sys.exit(1)
        try:
            version, raw, v2 = load_rc_yml(rc_path)
        except Exception as exc:
            click.echo(f"rc.yml parse failed: {exc}", err=True)
            sys.exit(1)
        if version != 2 or v2 is None:
            click.echo(
                "Error: --reconcile requires rc.yml v2 (provider-based deploy).",
                err=True,
            )
            sys.exit(1)
        d_ctx = build_deploy_context(v2, raw, rc_path)
        report = resolve_provider(v2).status(d_ctx)
        stale = [s.name for s in report.services if getattr(s, "is_stale", False)]
        if not stale:
            click.echo(
                "  No stale services detected — every running revision "
                "matches its family's latest task def. Nothing to reconcile."
            )
            return
        click.echo(
            f"  Reconciling {len(stale)} stale service(s): "
            f"{', '.join(sorted(stale))}"
        )
        services_list = stale
    from remote_compose.cli_v2 import dispatch_if_v2
    from remote_compose.terraform.runner import TerraformError
    try:
        handled = dispatch_if_v2(
            ctx.obj.get('config_path'), 'deploy',
            ttl=ttl, services=services_list, tag=tag, dev=dev_mode,
        )
    except TerraformError as exc:
        # Friendly rendering for the most common confusing failure: another
        # rc deploy is in flight and holds the s3-backend dynamodb lock.
        # terraform's stock message buries the cause in 12 lines of stack;
        # surface a one-liner the user can act on.
        msg = ((exc.stderr or "") + (exc.stdout or "")).lower()
        if "state lock" in msg or "conditionalcheckfailedexception" in msg:
            raise click.ClickException(
                "another rc deploy is already in flight (terraform state "
                "lock held). Wait for it to finish, then retry. Force-"
                "unlock with `terraform force-unlock <id>` ONLY if you're "
                "sure no concurrent apply is running."
            )
        raise
    if handled:
        return
    if ttl:
        click.echo(
            "Error: --ttl requires an rc.yml v2 config (provider-based "
            "deploy). Legacy v1 deploys cannot be tagged ephemeral.",
            err=True,
        )
        sys.exit(1)

    if code_only and selected_services:
        click.echo("Error: --code-only and --services are mutually exclusive", err=True)
        sys.exit(1)

    config = _load_config(ctx.obj.get('config_path'))
    _bootstrap_django(config)

    compose_path = _resolve_compose_path(config)
    project_name = config['project_name']
    region = config.get('region', 'us-west-2')

    click.echo(f"\nRemote Compose — deploying {project_name} to {region}")
    if code_only:
        click.echo("  (code-only: skipping infrastructure services)\n")
    elif services_list:
        click.echo(f"  (deploying: {', '.join(services_list)})\n")
    elif dry_run:
        click.echo("  (dry run — no changes will be made)\n")
    elif no_build:
        click.echo("  (skipping image builds)\n")
    else:
        click.echo()

    from remote_compose.models import ECSCluster

    try:
        cluster = ECSCluster.objects.get(name=config['cluster'])
    except ECSCluster.DoesNotExist:
        click.echo(f"Error: Cluster '{config['cluster']}' not found. Run 'rc provision' first.", err=True)
        sys.exit(1)

    svc_config_path = _write_service_config(config)

    sc_namespace = getattr(cluster, 'service_connect_namespace', None)
    try:
        sc_namespace = cluster.service_connect_namespace
    except Exception:
        sc_namespace = None

    vpc_infra = None
    try:
        vpc_infra = cluster.vpc_infrastructure
    except Exception:
        pass

    load_balancer = None
    try:
        from remote_compose.models.infrastructure import LoadBalancerConfig
        load_balancer = LoadBalancerConfig.objects.filter(cluster=cluster).first()
    except Exception:
        pass

    from remote_compose.services.deployment_pipeline.pipeline import PipelineBuilder
    from remote_compose.services.deployment_pipeline.context import PipelineContext

    context = PipelineContext(
        cluster=cluster,
        compose_file_path=compose_path,
        project_name=project_name,
        image_tag=tag,
        deployed_by='rc-cli',
        build_images=not no_build,
        wait_for_stable=True,
        timeout=600,
        dry_run=dry_run,
        service_config_path=svc_config_path,
        secrets_files=config.get('secrets', []),
        certificate_arn=config.get('certificate_arn'),
        domain=config.get('domain'),
        service_connect_namespace=sc_namespace,
        vpc_infrastructure=vpc_infra,
        load_balancer=load_balancer,
        code_only=code_only,
        selected_services=services_list,
    )

    pipeline = PipelineBuilder.multi_service_deployment()

    step_num = [0]
    total_steps = len(pipeline.steps)
    step_start = [time.time()]

    def handler(event_type, **kwargs):
        step_name = kwargs.get('step', '')
        if event_type == 'step_started':
            step_num[0] += 1
            step_start[0] = time.time()
            click.echo(f"  [{step_num[0]}/{total_steps}] {step_name}...", nl=False)
        elif event_type == 'step_completed':
            elapsed = time.time() - step_start[0]
            if elapsed > 2:
                click.echo(f" done ({elapsed:.0f}s)")
            else:
                click.echo(" done")
        elif event_type == 'step_skipped':
            click.echo(" skipped")
        elif event_type == 'step_failed':
            click.echo(" FAILED")

    pipeline.attach_event_handler(handler)
    result = pipeline.execute(context)

    try:
        os.unlink(svc_config_path)
    except OSError:
        pass

    click.echo()
    if result.success:
        _print_service_table(context)

        if context.load_balancer:
            click.echo(f"\n  ALB: {context.load_balancer.alb_dns_name}")

        click.echo(f"  Duration: {result.duration_seconds:.0f}s")
    else:
        click.echo(f"  Deployment failed at '{result.failed_step}': {result.error}", err=True)
        sys.exit(1)
