"""rc provision — v1 imperative VPC + ALB + secrets + Service Connect setup."""

from __future__ import annotations

import os
import sys

import click

from ._legacy import (
    _bootstrap_django,
    _get_or_create_cluster,
    _load_config,
    _resolve_compose_path,
    _write_service_config,
)


@click.command(name="provision")
@click.option("--dry-run", is_flag=True, help="Preview infrastructure changes")
@click.pass_context
def provision_cmd(ctx, dry_run):
    """Provision VPC, ALB, security groups, secrets, and Service Connect."""
    config = _load_config(ctx.obj.get("config_path"))
    _bootstrap_django(config)

    compose_path = _resolve_compose_path(config)
    project_name = config["project_name"]
    region = config.get("region", "us-west-2")

    click.echo(f"\nRemote Compose — provisioning {project_name} in {region}")
    if dry_run:
        click.echo("  (dry run — no changes will be made)\n")
    else:
        click.echo()

    cluster = _get_or_create_cluster(config)
    svc_config_path = _write_service_config(config)

    from remote_compose.services.deployment_pipeline.pipeline import PipelineBuilder
    from remote_compose.services.deployment_pipeline.context import PipelineContext

    context = PipelineContext(
        cluster=cluster,
        compose_file_path=compose_path,
        project_name=project_name,
        dry_run=dry_run,
        deployed_by="rc-cli",
        vpc_cidr=config.get("vpc_cidr", "10.0.0.0/16"),
        certificate_arn=config.get("certificate_arn"),
        domain=config.get("domain"),
        secrets_files=config.get("secrets", []),
        service_config_path=svc_config_path,
    )

    pipeline = PipelineBuilder.infrastructure_provisioning()

    step_num = [0]
    total_steps = len(pipeline.steps)

    def handler(event_type, **kwargs):
        step_name = kwargs.get("step", "")
        if event_type == "step_started":
            step_num[0] += 1
            click.echo(f"  [{step_num[0]}/{total_steps}] {step_name}...", nl=False)
        elif event_type == "step_completed":
            click.echo(" done")
        elif event_type == "step_skipped":
            click.echo(" skipped")
        elif event_type == "step_failed":
            click.echo(" FAILED")

    pipeline.attach_event_handler(handler)
    result = pipeline.execute(context)

    try:
        os.unlink(svc_config_path)
    except OSError:
        pass

    click.echo()
    if result.success:
        click.echo(f"  Provisioning complete ({result.duration_seconds:.0f}s)")
        if context.vpc_infrastructure:
            click.echo(f"  VPC: {context.vpc_infrastructure.vpc_id}")
        if context.load_balancer:
            click.echo(f"  ALB: {context.load_balancer.alb_dns_name}")
        if context.service_connect_namespace:
            click.echo(
                f"  Namespace: {context.service_connect_namespace.namespace_name}"
            )
        if context.secrets_arns:
            click.echo(f"  Secrets: {len(context.secrets_arns)} configured")
    else:
        click.echo(
            f"  Provisioning failed at '{result.failed_step}': {result.error}", err=True
        )
        sys.exit(1)
