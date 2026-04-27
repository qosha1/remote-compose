"""rc fix — one-shot scaffolders for common ECS deploy gotchas."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml


@click.group(name='fix')
def fix_group():
    """One-shot scaffolders for common ECS deploy gotchas.

    \b
    Subcommands:
      rc fix nginx-conf   Emit an ECS-ready nginx.conf + Dockerfile
                          (rc-e5u.44.21).
    """


@fix_group.command(name='nginx-conf')
@click.option('--upstream', 'upstream_specs', multiple=True,
              help='Upstream service to proxy, in NAME:PORT form. Repeat '
                   'for multi-upstream configs. The first --upstream is '
                   'wired into the catch-all default_server block.')
@click.option('--django', 'django_names', multiple=True,
              help='Mark the named upstream(s) as Django so the generator '
                   "injects 'proxy_set_header Host localhost;' (works around "
                   "ALLOWED_HOSTS rejection of ALB DNS Host headers). "
                   "Use --django=NAME or just --django to mark every "
                   "upstream.")
@click.option('--out', 'out_dir', default='compose/ecs/nginx', show_default=True,
              type=click.Path(file_okay=False),
              help='Subdir under the project to write nginx.conf + '
                   'Dockerfile into. The default mirrors the convention '
                   "verified against rc-test-startsimpli.")
@click.option('--force', is_flag=True,
              help='Overwrite existing nginx.conf / Dockerfile in --out.')
@click.pass_context
def fix_nginx_conf_cmd(ctx, upstream_specs, django_names, out_dir, force):
    """Emit an ECS-ready nginx.conf + Dockerfile (rc-e5u.44.21).

    \b
    Generates a SIBLING nginx config (under compose/ecs/nginx/) that proxies
    one or more upstream compose services using the variable-based
    proxy_pass pattern that survives Cloud Map task replacements.
    Three pieces matter and must stay in sync:
      1. resolver <vpc_cidr_base+2> — the only DNS reachable from a Fargate
         task ENI. NOT 169.254.169.253.
      2. NO 'upstream { server X:Y; }' blocks (stock nginx caches the lookup
         at config-load time → dies on task replacement).
      3. 'set $u "<svc>.<project>.local:<port>"; proxy_pass http://$u;' —
         per-request resolution, FQDN form (nginx's resolver doesn't
         honour /etc/resolv.conf search domains).

    \b
    For Django upstreams add --django=NAME so the generator also injects
    'proxy_set_header Host localhost;' — Django's ALLOWED_HOSTS check
    rejects the ALB DNS Host header otherwise (returns 400).

    \b
    Examples:
      rc fix nginx-conf --upstream django:8000 --django=django
      rc fix nginx-conf --upstream web:3000 --upstream api:5000 --django=api
      rc fix nginx-conf  # reads upstreams from rc.yml services with port:
    """
    from remote_compose.fix_nginx_conf import (
        Upstream, parse_upstream_arg, upstreams_from_rc_v2, write_ecs_nginx,
    )

    config_path = ctx.obj.get('config_path') or 'rc.yml'
    rc_path = Path(config_path)
    if not rc_path.exists():
        click.echo(f"Error: {rc_path} not found.", err=True)
        sys.exit(1)

    raw = yaml.safe_load(rc_path.read_text()) or {}
    project = str(raw.get('project') or '')
    ecs_cfg = ((raw.get('provider_config') or {}).get('ecs') or {})
    vpc_cidr = ecs_cfg.get('vpc_cidr')

    django_set = {str(d) for d in (django_names or ())}

    upstreams: list[Upstream] = []
    if upstream_specs:
        for spec in upstream_specs:
            try:
                upstreams.append(parse_upstream_arg(spec, django_set))
            except ValueError as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)
    else:
        upstreams = upstreams_from_rc_v2(raw, django_services=django_set)
        if not upstreams:
            click.echo(
                "Error: no --upstream specs and rc.yml has no services "
                "with a numeric `port:` to derive upstreams from.",
                err=True,
            )
            sys.exit(1)

    project_dir = rc_path.parent.resolve()
    try:
        nginx_path, dockerfile_path = write_ecs_nginx(
            project_dir=project_dir,
            upstreams=upstreams,
            project=project,
            vpc_cidr=vpc_cidr,
            force=force,
            output_subdir=out_dir,
        )
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"\nrc fix nginx-conf")
    click.echo(f"  project:    {project or '<unset>'}")
    click.echo(f"  vpc_cidr:   {vpc_cidr or '10.0.0.0/16 (default)'}")
    click.echo(f"  upstreams:  " + ", ".join(
        f"{u.name}:{u.port}{' (django)' if u.django else ''}"
        for u in upstreams
    ))
    click.echo(f"  wrote:      {nginx_path.relative_to(project_dir)}")
    click.echo(f"              {dockerfile_path.relative_to(project_dir)}")
    click.echo(
        f"\n  Wire it into your compose ECS variant (build context = "
        f"project root):"
    )
    click.echo(f"\n    services:")
    click.echo(f"      nginx:")
    click.echo(f"        build:")
    click.echo(f"          context: .")
    click.echo(f"          dockerfile: {out_dir}/Dockerfile")
    click.echo(
        "\n  Then `rc deploy --services nginx` should produce a healthy "
        "ALB target on first attempt — no resolver/FQDN/Host iteration loop."
    )
