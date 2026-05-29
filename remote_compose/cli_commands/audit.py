"""rc audit — sweep an AWS account for resources matching a project.

Reverse of rc destroy. Reports orphan resources tagged Project=<name>
or named with the project prefix. Useful as a post-destroy verifier
or account-hygiene check.
"""

from __future__ import annotations


import click


@click.command(name="audit")
@click.option(
    "--project",
    "project_name",
    default=None,
    help="Project name to scan for. Defaults to rc.yml v2 project.",
)
@click.option(
    "--region",
    "region_name",
    default=None,
    help="AWS region to scan. Defaults to rc.yml v2 provider_config.ecs.region.",
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help="AWS profile. Defaults to rc.yml v2 provider_config.ecs.aws_profile.",
)
@click.option(
    "--delete",
    is_flag=True,
    help="Prompt to delete every leftover. Off by default — dry-run is the safer default.",
)
@click.pass_context
def audit_cmd(ctx, project_name, region_name, profile_name, delete):
    """Find AWS resources matching this project — orphans, leftovers, etc.

    \b
    Reverse of `rc destroy`. Sweeps every resource class terraform owns
    and reports anything tagged Project=<name> or named with the project
    prefix. Useful as a post-destroy verifier or account-hygiene check.

    \b
    Examples:
      rc audit
      rc audit --project rc-test-foo --region us-west-1
      rc audit --delete       # interactive cleanup
    """
    import boto3
    from remote_compose.audit import audit_project

    if not project_name or not region_name or not profile_name:
        from ._dispatchers import _load_v2_if_present

        loaded = _load_v2_if_present(ctx.obj.get("config_path"), strict=False)
        if loaded is not None:
            _, _, v2 = loaded
            project_name = project_name or v2.project
            ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
            region_name = region_name or ecs_cfg.get("region")
            profile_name = profile_name or ecs_cfg.get("aws_profile")

    if not project_name:
        click.echo(
            "rc audit: --project required (or run from a dir with rc.yml v2).", err=True
        )
        raise click.exceptions.Exit(1)
    if not region_name:
        click.echo(
            "rc audit: --region required (or set provider_config.ecs.region in rc.yml).",
            err=True,
        )
        raise click.exceptions.Exit(1)

    session = boto3.Session(region_name=region_name, profile_name=profile_name)
    report = audit_project(session, project=project_name, region=region_name)
    click.echo(report.render())

    if not delete or report.is_clean:
        return

    if not click.confirm(f"\nDelete {len(report.findings)} resource(s)?"):
        click.echo("Aborted.")
        return
    click.echo(
        "\n  --delete is dry-run today. Per-resource deletion will land "
        "in the next iteration; for now use the listed identifiers with "
        "the matching `aws <svc> delete-...` commands.",
        err=True,
    )
