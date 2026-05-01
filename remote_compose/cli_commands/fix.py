"""rc fix — one-shot scaffolders for common ECS deploy gotchas."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml

from ..defaults import VPC_CIDR_DEFAULT


@click.group(name='fix')
def fix_group():
    """One-shot scaffolders for common ECS deploy gotchas.

    \b
    Subcommands:
      rc fix nginx-conf            Emit an ECS-ready nginx.conf + Dockerfile
                                   (rc-e5u.44.21).
      rc fix bake-bind-mount-source <service>
                                   Append COPY <host> <container> to a
                                   service's Dockerfile so /app exists in
                                   the built image (rc-bys).
      rc fix django-tls            Append CSRF_TRUSTED_ORIGINS reader +
                                   SECURE_PROXY_SSL_HEADER +
                                   USE_X_FORWARDED_HOST to the active
                                   Django settings module so admin login
                                   works behind the ALB (rc-j08).
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
    click.echo(f"  vpc_cidr:   {vpc_cidr or f'{VPC_CIDR_DEFAULT} (default)'}")
    click.echo(f"  upstreams:  " + ", ".join(
        f"{u.name}:{u.port}{' (django)' if u.django else ''}"
        for u in upstreams
    ))
    click.echo(f"  wrote:      {nginx_path.relative_to(project_dir)}")
    click.echo(f"              {dockerfile_path.relative_to(project_dir)}")
    # rc-2kp: ensure next rc up doesn't reuse a stale buildx layer cache
    # that predates the nginx-conf write.
    from remote_compose.no_cache_state import mark_no_cache
    mark_no_cache(project_dir, reason="rc fix nginx-conf")
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


@fix_group.command(name='bake-bind-mount-source')
@click.argument('service')
@click.option('--force', is_flag=True,
              help='Append the COPY line even if a similar one already exists.')
@click.pass_context
def fix_bake_bind_mount_source_cmd(ctx, service, force):
    """Patch a service's Dockerfile so its bind-mount source dirs are baked in.

    \b
    Local docker-compose stacks commonly use:
        services:
          django:
            build: { context: ., dockerfile: compose/local/django/Dockerfile }
            volumes:
              - ./backend:/app

    The bind mount overrides whatever's in /app at runtime — so the local
    Dockerfile typically does NOT `COPY ./backend /app`. ECS has no bind
    mounts; the running container's /app is empty; manage.py is missing;
    the start script crashes.

    This subcommand parses the service's compose volumes, finds the
    HOST_DIR:CONTAINER_DIR pairs (skipping system mounts like /tmp/.X11-unix
    and absolute system paths), and appends a `COPY <host> <container>`
    line to the matching Dockerfile so the same image works for ECS.

    \b
    Local docker-compose still bind-mounts at runtime (overrides the COPY)
    so hot-reload keeps working for local dev.

    \b
    Examples:
      rc fix bake-bind-mount-source django
      rc fix bake-bind-mount-source celeryworker --force
    """
    from remote_compose.fix_bake_bind import bake_bind_mount_source

    config_path = ctx.obj.get('config_path') or 'rc.yml'
    rc_path = Path(config_path)
    if not rc_path.exists():
        raise click.ClickException(f"{rc_path} not found.")

    rc_raw = yaml.safe_load(rc_path.read_text()) or {}
    compose_field = rc_raw.get("compose_file") or "docker-compose.yml"
    compose_path = Path(compose_field)
    if not compose_path.is_absolute():
        compose_path = (rc_path.parent / compose_path).resolve()
    if not compose_path.exists():
        raise click.ClickException(
            f"compose file {compose_path} not found (rc.yml.compose_file = "
            f"{compose_field!r})."
        )

    try:
        result = bake_bind_mount_source(
            compose_path, service, force=force,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc))

    if result.skipped_reason:
        click.echo(f"  rc fix bake-bind-mount-source {service}: "
                   f"{result.skipped_reason}")
        return

    # rc-2kp: edits to the Dockerfile may not invalidate the buildx layer
    # cache reliably (the registry cache can contain layers from older
    # Dockerfile revisions that buildx still reuses). Drop the no-cache
    # sentinel so the next `rc up` rebuilds without --cache-from for this
    # service's image.
    from remote_compose.no_cache_state import mark_no_cache
    mark_no_cache(
        rc_path.parent.resolve(),
        reason=f"rc fix bake-bind-mount-source {service}",
    )

    click.echo(f"\nrc fix bake-bind-mount-source {service}")
    click.echo(f"  dockerfile:  {result.dockerfile_path}")
    click.echo(f"  added COPY:")
    for host, container in result.copies_added:
        click.echo(f"    COPY {host} {container}")
    if result.skipped_dockerignored:
        click.echo(f"\n  ⚠ skipped (excluded by .dockerignore — "
                   f"adding COPY would break docker build):")
        for host, container in result.skipped_dockerignored:
            click.echo(f"    COPY {host} {container}")
        click.echo(f"  → remove the dockerignore entry for these paths if "
                   f"you need them baked into the ECS image.")
    click.echo(
        "\n  Local docker-compose still bind-mounts these paths at runtime "
        "(the bind mount overrides the COPY), so local hot-reload keeps "
        "working. ECS deploys (no bind mounts) now have the source baked in."
    )


@fix_group.command(name='django-tls')
@click.option('--settings', 'settings_module', default=None,
              help='Django settings module to patch (dotted path or '
                   'relative path). Auto-detected when omitted from the '
                   'usual locations: backend/config/settings/local.py, '
                   'config/settings.py, etc.')
@click.option('--secure-cookies', is_flag=True,
              help='Also append SESSION_COOKIE_SECURE + CSRF_COOKIE_SECURE. '
                   'Skip when any path in the stack is reachable over '
                   'plain HTTP (e.g. service-to-service health checks).')
@click.option('--force', is_flag=True,
              help='Re-append the block even when the rc-j08 marker is '
                   'already present.')
@click.pass_context
def fix_django_tls_cmd(ctx, settings_module, secure_cookies, force):
    """Patch a Django settings module so it consumes the env vars rc up --domain injects.

    \b
    Background: `rc up --domain X` (rc-32x) sets DJANGO_ALLOWED_HOSTS +
    CSRF_TRUSTED_ORIGINS in the django container env. But Django's
    settings.py has to actually READ those env vars for them to take
    effect — most Django apps' local.py / production.py hardcodes
    these settings. The result: admin POST login returns 403 'Origin
    checking failed' even though the env var is set on the task def.
    \b
    This subcommand appends an env-reading block to the active Django
    settings module. Three universally-correct settings:

    \b
      CSRF_TRUSTED_ORIGINS       — reads the env var rc up --domain sets
      SECURE_PROXY_SSL_HEADER    — trusts ALB X-Forwarded-Proto so
                                   request.is_secure() returns True
      USE_X_FORWARDED_HOST       — build_absolute_uri() returns public
                                   hostname (not internal task IP)

    \b
    --secure-cookies adds SESSION_COOKIE_SECURE + CSRF_COOKIE_SECURE
    when the stack is HTTPS-only.

    \b
    Idempotent via the rc-j08 marker — re-runs are no-ops unless
    --force.
    """
    from remote_compose.fix_django_tls import fix_django_tls

    config_path = ctx.obj.get('config_path') or 'rc.yml'
    rc_path = Path(config_path)
    if not rc_path.exists():
        raise click.ClickException(f"{rc_path} not found.")
    project_dir = rc_path.parent.resolve()

    try:
        result = fix_django_tls(
            project_dir,
            settings_module=settings_module,
            secure_cookies=secure_cookies,
            force=force,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc))

    if result.skipped_reason:
        click.echo(
            f"  rc fix django-tls: {result.skipped_reason}\n"
            f"  ({result.settings_path})"
        )
        return

    # rc-2kp: settings.py is part of the build context (COPY ./backend /app
    # picks it up). Same cache-staleness risk as fix_bake_bind: the
    # registry layer cache may not invalidate reliably across
    # source-content changes. Force --no-cache on next rc up.
    from remote_compose.no_cache_state import mark_no_cache
    mark_no_cache(project_dir, reason="rc fix django-tls")

    click.echo(f"\nrc fix django-tls")
    click.echo(f"  settings:  {result.settings_path}")
    click.echo(f"  appended:")
    click.echo(f"    CSRF_TRUSTED_ORIGINS = env('CSRF_TRUSTED_ORIGINS')")
    click.echo(f"    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')")
    click.echo(f"    USE_X_FORWARDED_HOST = True")
    if secure_cookies:
        click.echo(f"    SESSION_COOKIE_SECURE = True")
        click.echo(f"    CSRF_COOKIE_SECURE = True")
    click.echo(
        "\n  Now `rc up --domain X` will produce a stack where /admin "
        "POST login works first time."
    )
