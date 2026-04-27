"""
rc — Simple Remote Compose CLI.

Drop an rc.yml in your project directory and deploy to ECS with:
    rc provision   # one-time infrastructure setup
    rc deploy      # build, push, deploy
    rc status      # check service health
    rc destroy     # tear it all down
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional

import click
import yaml


RC_CONFIG_FILE = 'rc.yml'

# Helpers used by still-in-cli commands. As commands move to cli_commands/*
# their imports follow them; this top-level re-export keeps the remaining
# v1+v2 mixed commands working without churn.
from .cli_commands._dispatchers import (  # noqa: E402
    _build_restore_script,
    _db_push_v2,
    _detect_dump_format,
    _detect_empty_file_secrets,
    _exec_v2,
    _flatten_v2_to_legacy,
    _secrets_push_v2,
)
from .cli_commands._legacy import (  # noqa: E402
    DatabaseBackupEngine,
    PostgresBackupEngine,
    _bootstrap_django,
    _exec_interactive,
    _format_size,
    _get_backup_engine,
    _get_or_create_cluster,
    _load_config,
    _resolve_compose_path,
    _resolve_ecs_exec_target,
    _set_aws_profile,
    _step_counter,
    _write_service_config,
)

RC_TEMPLATE_V2 = """\
# rc.yml — Remote Compose configuration (v2 schema)

version: 2
project: my-project
compose_file: docker-compose.yml

provider: ecs

provider_config:
  ecs:
    region: us-west-2
    cluster: my-project-cluster
    # aws_profile: default
    vpc_cidr: 10.42.0.0/16
    default_launch_type: FARGATE

terraform:
  output_dir: ./terraform/${provider}
  backend:
    type: local
    # For shared state across machines, use s3:
    # type: s3
    # bucket: my-project-tf-state
    # key: ecs.tfstate
    # region: us-west-2

# domain: api.example.com               # custom domain — auto-provisions ACM cert + HTTPS
# certificate_arn: arn:aws:acm:...      # or supply an existing cert

# Compose-driven deploy set. Every compose service deploys with defaults
# unless overridden under `services:` below or filtered here.
# compose:
#   exclude:
#     - dev-only-sidecar

services:
  web:
    cpu: 512
    memory: 1024
    type: application
    public: true
    port: 80
    health_check_path: /health/
    default_target: true
  # worker:
  #   cpu: 1024
  #   memory: 2048
  #   type: worker
  # postgres:
  #   cpu: 512
  #   memory: 1024
  #   type: infrastructure

# Env files become Secrets Manager JSON blobs; the task def gets one
# secrets[] entry per KEY using arn:KEY:: selectors.
# secrets:
#   - name: app
#     source: file
#     path: .envs/.production/.app

# Database backup — rc db backup / rc db restore / rc db list
# backup:
#   bucket: my-project-db-dumps
#   service: postgres
#   retention_days: 14
"""


RC_TEMPLATE_V1 = """\
# rc.yml — Remote Compose configuration (legacy v1 schema)
# For new projects prefer the v2 schema (omit --v1).

cluster: my-cluster
region: us-west-2
compose_file: docker-compose.production.yml
project_name: my-project

vpc_cidr: 10.0.0.0/16
# domain: api.example.com  # custom domain — auto-provisions ACM cert + HTTPS
# certificate_arn: arn:aws:acm:us-east-1:XXXX:certificate/XXXX  # or use existing cert

# Env files to push as Secrets Manager secrets
# secrets:
#   - .envs/.production/.django
#   - .envs/.production/.postgres

services:
  web:
    cpu: 512
    memory: 1024
    type: application
    health_check_path: /health/
  # worker:
  #   cpu: 1024
  #   memory: 2048
  #   type: worker
  # nginx:
  #   cpu: 256
  #   memory: 512
  #   type: proxy
  #   public: true
  #   port: 80
  #   health_check_path: /health
  #   default_target: true

# Database backup — rc db backup / rc db restore / rc db list
# backup:
#   bucket: my-project-db-dumps  # S3 bucket for backups
#   service: postgres            # service to exec into (needs pg_dump + aws CLI)
#   retention: 30                # keep last N backups
"""


# Helpers extracted to cli_commands/_dispatchers.py and cli_commands/_legacy.py


# =============================================================================
# CLI Group
# =============================================================================

@click.group()
@click.option('-c', '--config', 'config_path', default=None, help='Path to rc.yml')
@click.pass_context
def cli(ctx, config_path):
    """rc — Simple Remote Compose CLI for ECS deployments."""
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config_path


# =============================================================================
# rc init
# =============================================================================

@cli.command()
@click.option('--v1', 'use_v1', is_flag=True,
              help='Emit the legacy v1 schema (top-level cluster/region/compose_file). Default is v2.')
@click.option('--from-compose', 'from_compose', type=click.Path(exists=True, dir_okay=False),
              default=None,
              help='Read a docker-compose.yml and scaffold a v2 rc.yml from it.')
@click.option('-o', '--output', 'output_path', type=click.Path(dir_okay=False),
              default=None,
              help=f'Write to this path instead of ./{RC_CONFIG_FILE}.')
@click.option('--public-service', 'public_service', default=None,
              help='Override the auto-detected ALB-fronted service (used with --from-compose).')
@click.option('--region', default='us-west-2',
              help='AWS region in the generated rc.yml (used with --from-compose).')
@click.option('--aws-profile', 'aws_profile', default=None,
              help='aws_profile in the generated rc.yml (used with --from-compose).')
@click.option('--testing-defaults/--no-testing-defaults', 'testing_defaults',
              default=None,
              help='Inject DJANGO_ALLOWED_HOSTS=* / CSRF_TRUSTED_ORIGINS=* / '
                   'DJANGO_DEBUG=False on Django services (used with '
                   '--from-compose). Default: auto-enabled when project '
                   'starts with rc-test-, off otherwise. UNSAFE for '
                   'production stacks. See rc-e5u.46.4.')
def init(use_v1, from_compose, output_path, public_service, region, aws_profile,
         testing_defaults):
    """Generate an rc.yml template in the current directory.

    With --from-compose, read an existing docker-compose.yml and scaffold
    a v2 rc.yml with per-service entries inferred from images / commands /
    ports. Edit the result before deploying.
    """
    target = Path(output_path) if output_path else Path.cwd() / RC_CONFIG_FILE
    if target.exists():
        click.echo(f"{target} already exists")
        if not click.confirm("Overwrite?", default=False):
            return

    if from_compose:
        if use_v1:
            raise click.UsageError("--from-compose only generates v2 schema; drop --v1")
        from remote_compose.init_from_compose import generate_v2_rc_yml
        try:
            text = generate_v2_rc_yml(
                Path(from_compose),
                output_path=target,
                public_service=public_service,
                region=region,
                aws_profile=aws_profile,
                testing_defaults=testing_defaults,
            )
        except Exception as exc:
            raise click.ClickException(f"failed to scaffold from {from_compose}: {exc}")
        target.write_text(text)
        click.echo(f"Created {target} from {from_compose}")
        click.echo("Review the generated file, then run `rc plan`.")
        return

    template = RC_TEMPLATE_V1 if use_v1 else RC_TEMPLATE_V2
    target.write_text(template)
    click.echo(f"Created {target}")
    if use_v1:
        click.echo("Legacy v1 schema. Edit cluster/region/services and run `rc deploy`.")
    else:
        click.echo("v2 schema. Edit project/region/services and run `rc plan`.")


# =============================================================================
# rc provision
# =============================================================================

@cli.command()
@click.option('--dry-run', is_flag=True, help='Preview infrastructure changes')
@click.pass_context
def provision(ctx, dry_run):
    """Provision VPC, ALB, security groups, secrets, and Service Connect."""
    config = _load_config(ctx.obj.get('config_path'))
    _bootstrap_django(config)

    compose_path = _resolve_compose_path(config)
    project_name = config['project_name']
    region = config.get('region', 'us-west-2')

    click.echo(f"\nRemote Compose — provisioning {project_name} in {region}")
    if dry_run:
        click.echo("  (dry run — no changes will be made)\n")
    else:
        click.echo()

    cluster = _get_or_create_cluster(config)

    # Write service config from rc.yml
    svc_config_path = _write_service_config(config)

    from remote_compose.services.deployment_pipeline.pipeline import PipelineBuilder
    from remote_compose.services.deployment_pipeline.context import PipelineContext

    context = PipelineContext(
        cluster=cluster,
        compose_file_path=compose_path,
        project_name=project_name,
        dry_run=dry_run,
        deployed_by='rc-cli',
        vpc_cidr=config.get('vpc_cidr', '10.0.0.0/16'),
        certificate_arn=config.get('certificate_arn'),
        domain=config.get('domain'),
        secrets_files=config.get('secrets', []),
        service_config_path=svc_config_path,
    )

    pipeline = PipelineBuilder.infrastructure_provisioning()

    step_num = [0]
    total_steps = len(pipeline.steps)

    def handler(event_type, **kwargs):
        step_name = kwargs.get('step', '')
        if event_type == 'step_started':
            step_num[0] += 1
            click.echo(f"  [{step_num[0]}/{total_steps}] {step_name}...", nl=False)
        elif event_type == 'step_completed':
            click.echo(" done")
        elif event_type == 'step_skipped':
            click.echo(" skipped")
        elif event_type == 'step_failed':
            click.echo(" FAILED")

    pipeline.attach_event_handler(handler)
    result = pipeline.execute(context)

    # Cleanup temp file
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
            click.echo(f"  Namespace: {context.service_connect_namespace.namespace_name}")
        if context.secrets_arns:
            click.echo(f"  Secrets: {len(context.secrets_arns)} configured")
    else:
        click.echo(f"  Provisioning failed at '{result.failed_step}': {result.error}", err=True)
        sys.exit(1)


# =============================================================================
# rc deploy
# =============================================================================

@cli.command()
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
def deploy(ctx, no_build, dry_run, tag, code_only, selected_services, ttl, dev_mode, reconcile):
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
        # Discover stale services via provider.status, populate
        # services_list with their names. Empty list → nothing to do.
        from remote_compose.cli_v2 import (
            build_deploy_context, load_rc_yml, resolve_provider,
        )
        from pathlib import Path as _Path
        rc_path = _Path(ctx.obj.get('config_path') or RC_CONFIG_FILE)
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
    if dispatch_if_v2(
        ctx.obj.get('config_path'), 'deploy',
        ttl=ttl, services=services_list, tag=tag, dev=dev_mode,
    ):
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

    # services_list already computed above for the v2 dispatcher; reused here.

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

    # Load infrastructure created during provisioning
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
        # Print service status table
        _print_service_table(context)

        if context.load_balancer:
            click.echo(f"\n  ALB: {context.load_balancer.alb_dns_name}")

        click.echo(f"  Duration: {result.duration_seconds:.0f}s")
    else:
        click.echo(f"  Deployment failed at '{result.failed_step}': {result.error}", err=True)
        sys.exit(1)


# =============================================================================
# rc up — one-shot: scaffold (if missing) + deploy + push secrets
# =============================================================================

@cli.command()
@click.option('--from-compose', 'from_compose',
              type=click.Path(exists=True, dir_okay=False), default=None,
              help='If rc.yml is missing, scaffold one from this docker-compose.yml first.')
@click.option('--public-service', 'public_service', default=None,
              help='Override the auto-detected ALB-fronted service when scaffolding.')
@click.option('--region', default='us-west-2',
              help='AWS region used when scaffolding rc.yml (ignored if rc.yml exists).')
@click.option('--aws-profile', 'aws_profile', default=None,
              help='aws_profile used when scaffolding rc.yml (ignored if rc.yml exists).')
@click.option('--testing-defaults/--no-testing-defaults', 'testing_defaults',
              default=None,
              help='Inject DJANGO_ALLOWED_HOSTS=* / CSRF_TRUSTED_ORIGINS=* '
                   'on Django services when scaffolding rc.yml (ignored if '
                   'rc.yml exists). Default auto-on for rc-test-* projects. '
                   'See rc-e5u.46.4.')
@click.option('--ttl', 'ttl', default=None,
              help='Mark this stack ephemeral with the given TTL '
                   '(e.g. 30m, 4h, 2h30m). Tags resources Ephemeral=true + '
                   'ExpiresAt=<iso>; `rc reap` later destroys past-due stacks.')
@click.option('--dev', 'dev_mode', is_flag=True,
              help='Dev-mode deploy: provision EFS-backed bind mounts for '
                   'every services[*].dev_volumes entry so `rc dev push` can '
                   'stream local source into the running task for sub-second '
                   'iteration. See rc-e5u.45.8.')
@click.option('--domain', 'domain', default=None,
              help='Wire the ALB to a custom FQDN. Scaffolds '
                   'services.<public>.domain + provider_config.ecs.'
                   'route53_zone (drop-leftmost-label heuristic) so '
                   'terraform creates ACM cert + Route 53 A records. '
                   'Verifies the zone exists in the configured aws_profile '
                   'before deploying. See rc-e5u.46.7.')
@click.option('--alias', 'aliases', multiple=True,
              help='Add an additional FQDN as a SAN on the ACM cert + an '
                   'extra A record. Repeat --alias N times for N aliases. '
                   'Requires --domain.')
@click.option('--route53-zone', 'route53_zone', default=None,
              help='Override the Route 53 hosted-zone name (defaults to '
                   '--domain with the leftmost label dropped). Use when '
                   'your zone is something other than parent-of-FQDN.')
@click.pass_context
def up(ctx, from_compose, public_service, region, aws_profile,
       testing_defaults, ttl, dev_mode, domain, aliases, route53_zone):
    """One-shot: scaffold rc.yml (if missing), deploy, push secrets, print ALB URL.

    The "I have a docker-compose.yml — get me a running stack" command. With
    --from-compose, rc generates a v2 rc.yml on the fly when none exists,
    then runs the deploy and secrets-push pipeline. Idempotent: rerun on an
    unchanged config does the no-op terraform apply and a forced rollout.
    """
    config_path = ctx.obj.get('config_path') or RC_CONFIG_FILE
    target = Path(config_path)

    # --- Pre-flight: --alias requires --domain ---
    if aliases and not domain:
        raise click.ClickException(
            "--alias requires --domain. Use --domain <FQDN> + "
            "--alias <other-FQDN> together."
        )

    # --- Step 1: scaffold rc.yml if missing ---
    if not target.exists():
        if not from_compose:
            raise click.ClickException(
                f"{target} not found. Pass --from-compose <docker-compose.yml> to "
                f"scaffold inline, or run `rc init --from-compose <path>` first."
            )
        from remote_compose.init_from_compose import generate_v2_rc_yml
        click.echo(f"Scaffolding {target} from {from_compose}...")
        text = generate_v2_rc_yml(
            Path(from_compose),
            output_path=target,
            public_service=public_service,
            region=region,
            aws_profile=aws_profile,
            testing_defaults=testing_defaults,
        )
        target.write_text(text)
        click.echo(f"  written ({len(text)} bytes).\n")

    # --- Step 1.25: --domain wiring (rc-e5u.46.7) ---
    if domain:
        from remote_compose.init_from_compose import _patch_rc_yml_domain
        # Pre-flight: verify the configured Route 53 hosted zone exists in
        # the user's account/profile. Cheaper to fail here than after the
        # 5-min terraform apply that would error on aws_route53_record.
        from remote_compose.init_from_compose import _zone_from_domain_drop_leftmost
        zone_check = route53_zone or _zone_from_domain_drop_leftmost(domain)
        try:
            import boto3
            session = boto3.Session(profile_name=aws_profile, region_name=region)
            r53 = session.client("route53")
            zones = r53.list_hosted_zones().get("HostedZones") or []
            zone_names = {z.get("Name", "").rstrip(".") for z in zones}
            if zone_check.rstrip(".") not in zone_names:
                raise click.ClickException(
                    f"--domain pre-flight: Route 53 hosted zone "
                    f"'{zone_check}' not found in profile {aws_profile!r}. "
                    f"Existing zones: {sorted(zone_names) or '(none)'}. "
                    f"Override with --route53-zone <zone-you-own>."
                )
        except click.ClickException:
            raise
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"  WARN: could not verify Route 53 zone {zone_check!r} "
                f"({type(exc).__name__}: {exc}); proceeding anyway.",
                err=True,
            )
        try:
            wired = _patch_rc_yml_domain(
                target, domain, list(aliases or []), route53_zone=route53_zone,
            )
        except ValueError as exc:
            raise click.ClickException(f"--domain: {exc}")
        click.echo(
            f"  Domain wired (rc-e5u.46.7): "
            f"services.{wired['public_service']}.domain = {wired['domain']}; "
            f"route53_zone = {wired['route53_zone']}"
        )
        if wired["aliases"]:
            click.echo(f"    aliases: {', '.join(wired['aliases'])}")
        click.echo("")

    # --- Step 1.5: auto-fix nginx for ECS when compose trips .44.18 + Django ---
    # rc-e5u.46.2: when the user's nginx.conf has 'upstream { server X:Y; }'
    # without a resolver directive AND one of the upstreams looks like Django,
    # silently chain `rc fix nginx-conf` so the deploy below builds the
    # ECS-aware image instead of the stale-DNS local one. Without this the
    # user has to read the .44.18 warning, hand-run rc fix, wire the
    # dockerfile override (.46.1), and re-run rc up — five steps for what
    # is a deterministic fix.
    compose_path_for_autofix: Optional[Path] = None
    if from_compose:
        compose_path_for_autofix = Path(from_compose).resolve()
    elif target.exists():
        try:
            rc_raw_existing = yaml.safe_load(target.read_text()) or {}
        except yaml.YAMLError:
            rc_raw_existing = {}
        compose_field = rc_raw_existing.get("compose_file") if isinstance(
            rc_raw_existing, dict) else None
        if compose_field:
            cp = Path(compose_field)
            if not cp.is_absolute():
                cp = (target.parent / cp).resolve()
            if cp.exists():
                compose_path_for_autofix = cp
    if compose_path_for_autofix is not None:
        try:
            from remote_compose.init_from_compose import auto_fix_nginx_if_needed
            result = auto_fix_nginx_if_needed(target, compose_path_for_autofix)
        except Exception as exc:
            click.echo(f"  WARN: nginx auto-fix skipped: {exc}", err=True)
            result = None
        if result:
            project_dir = target.parent.resolve()
            try:
                nginx_rel = result["nginx_path"].relative_to(project_dir)
                df_rel = result["dockerfile_path"].relative_to(project_dir)
            except ValueError:
                nginx_rel = result["nginx_path"]
                df_rel = result["dockerfile_path"]
            ups = ", ".join(
                f"{u.name}:{u.port}{' (django)' if u.django else ''}"
                for u in result["upstreams"]
            )
            click.echo(
                f"  auto-fixed nginx config for ECS — see "
                f"./{result['output_subdir']}/ "
                f"(rc-e5u.46.2; rc-e5u.44.18 detector fired)."
            )
            click.echo(f"    upstreams:  {ups}")
            click.echo(f"    wrote:      {nginx_rel}")
            click.echo(f"                {df_rel}")
            click.echo(
                f"    rc.yml:     services.{result['nginx_service']}."
                f"dockerfile = {result['dockerfile_rel']}\n"
            )

    # --- Step 2: deploy via the v2 dispatcher ---
    # rc-3q9: defer auto_on_deploy lifecycle hooks until after Step 3
    # secrets-push + force-roll have completed. Otherwise the hooks land
    # on tasks still running with placeholder env vars (e.g. Django's
    # migrate hook fails connecting to Postgres → exit 254 noise).
    # ttl=None is fine; dispatcher only acts when truthy.
    from remote_compose.cli_v2 import (
        dispatch_if_v2, run_auto_on_deploy_hooks_for_path,
    )
    if not dispatch_if_v2(
        str(target), 'deploy',
        ttl=ttl, dev=dev_mode, defer_lifecycle_hooks=True,
    ):
        raise click.ClickException(
            f"{target} is not a v2 rc.yml. `rc up` only supports v2 — "
            f"migrate with `rc migrate` or use `rc deploy` for v1."
        )

    # --- Step 3: push file-sourced secrets and force a rollout ---
    # First-deploy task definitions reference SM secrets that don't exist
    # yet; push fills them in and force-rolls so the next task boots green.
    try:
        _secrets_push_v2(str(target), rollout=True)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"\n  WARN: secrets push failed: {exc}", err=True)
        click.echo("  Run `rc secrets push` after fixing the issue above.")

    # --- Step 4: now that real env vars are live, run any auto_on_deploy
    # lifecycle hooks (rc-3q9). Helper waits for ECS deployment stability
    # before exec-ing so the hook lands on a fresh task definition.
    try:
        run_auto_on_deploy_hooks_for_path(str(target))
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"\n  WARN: auto_on_deploy hooks failed: {exc!s}. "
            f"Rerun with `rc lifecycle <hook> <service>` for full output.",
            err=True,
        )

    if ttl:
        click.echo(
            f"\n  Stack is up (TTL {ttl}). `rc reap` tears it down once expired, "
            f"or `rc destroy --yes` to teardown now."
        )
    else:
        click.echo(
            "\n  Stack is up. Use `rc status` to inspect and `rc destroy` to tear down."
        )


# =============================================================================
# rc status
# =============================================================================

@cli.command()
@click.pass_context
def status(ctx):
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
        click.echo(f"Error: Cluster '{cluster_name}' not found. Run 'rc provision' first.", err=True)
        sys.exit(1)

    click.echo(f"\nRemote Compose — {project_name} ({cluster.aws_region})\n")

    services = ECSServiceModel.objects.filter(cluster=cluster)

    if not services.exists():
        click.echo("  No services deployed yet.")
        return

    # Try to get live status from AWS
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

    # Show ALB if available
    try:
        if cluster.load_balancer:
            click.echo(f"\n  ALB: {cluster.load_balancer.alb_dns_name}")
    except Exception:
        pass


# =============================================================================
# rc restart
# =============================================================================

@cli.command()
@click.argument('service', required=False)
@click.pass_context
def restart(ctx, service):
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
        # Try both the bare name and project-prefixed name
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


# =============================================================================
# rc exec
# =============================================================================

@cli.command(
    context_settings=dict(
        ignore_unknown_options=True,
    ),
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
    import shutil
    import subprocess

    if not command:
        click.echo("Error: No command specified. Use -- before the command.", err=True)
        click.echo("Example: rc exec django -- python manage.py shell", err=True)
        sys.exit(1)

    # Check for session-manager-plugin
    if not shutil.which('session-manager-plugin'):
        click.echo(
            "Error: session-manager-plugin is not installed.\n"
            "Install it: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html",
            err=True,
        )
        sys.exit(1)

    # v2 path: route through Provider.exec which knows about v2 stacks.
    # Returns True when handled (rc.yml v2 detected), else falls through.
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

    # Resolve service name (bare name or project-prefixed)
    services = ECSServiceModel.objects.filter(cluster=cluster)
    svc = services.filter(name=service).first() or \
          services.filter(name=f"{project_name}-{service}").first()

    if not svc:
        available = [s.name for s in services]
        click.echo(f"Error: Service '{service}' not found.", err=True)
        if available:
            click.echo(f"Available services: {', '.join(available)}", err=True)
        sys.exit(1)

    # Find a running task
    try:
        task_arns = ecs_svc.list_tasks(cluster, service_name=svc.name)
    except Exception as e:
        click.echo(f"Error listing tasks: {e}", err=True)
        sys.exit(1)

    if not task_arns:
        click.echo(f"Error: No running tasks for service '{svc.name}'.", err=True)
        sys.exit(1)

    task_arn = task_arns[0]

    # Get container name from task description if not specified
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


# =============================================================================
# rc logs
# =============================================================================

@cli.command()
@click.argument('service')
@click.option('-n', '--lines', default=50, help='Number of log lines (default: 50)')
@click.pass_context
def logs(ctx, service, lines):
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

    # Find recent deployments for this project
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


# =============================================================================
# rc secrets push
# =============================================================================

@cli.group(name='secrets')
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
        click.echo(f"Error: Cluster '{config['cluster']}' not found. Run 'rc provision' first.", err=True)
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


cli.add_command(secrets_group)


# =============================================================================
# rc db — database backup/restore
# =============================================================================

@cli.group(name='db')
def db_group():
    """Database backup and restore via S3."""
    pass


@db_group.command(name='backup')
@click.option('--service', default=None, help='Service to exec into (default: backup.service from rc.yml)')
@click.pass_context
def db_backup(ctx, service):
    """Dump the database and upload to S3.

    \b
    Execs into the target container (default: django), runs pg_dump,
    and uploads to S3. Keeps the terminal connected until complete.

    \b
    Examples:
      rc db backup
      rc db backup --service django
    """
    config = _load_config(ctx.obj.get('config_path'))
    _set_aws_profile(config)

    backup_cfg = config.get('backup', {})
    bucket = backup_cfg.get('bucket')
    if not bucket:
        click.echo("Error: 'backup.bucket' not set in rc.yml", err=True)
        click.echo("Add a backup section to rc.yml:\n")
        click.echo("  backup:")
        click.echo("    bucket: my-project-db-dumps")
        click.echo("    service: django")
        sys.exit(1)

    _bootstrap_django(config)

    service_name = service or backup_cfg.get('service', 'django')
    project_name = config['project_name']
    region = config.get('region', 'us-west-2')

    cluster, svc, task_arn, container = _resolve_ecs_exec_target(config, service_name)

    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S')
    s3_key = f"{project_name}/{project_name}-{timestamp}.dump"
    s3_uri = f"s3://{bucket}/{s3_key}"

    click.echo(f"\nBackup: {svc.name} → {s3_uri}\n")
    click.echo(f"  This may take a while for large databases. Keep this terminal open.\n")

    # Generate presigned PUT URL so the container can upload via curl
    # instead of aws s3 cp (which can OOM on large files in Fargate).
    import boto3
    s3_client = boto3.client('s3', region_name=region)
    presigned_url = s3_client.generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket, 'Key': s3_key},
        ExpiresIn=7200,  # 2 hours
    )

    engine = _get_backup_engine(config)
    backup_script = engine.get_dump_script(s3_uri)

    import base64
    script_b64 = base64.b64encode(backup_script.encode()).decode()

    backup_cmd = (
        f"sh -c 'echo {script_b64} | base64 -d > /tmp/_backup.sh && "
        f"chmod +x /tmp/_backup.sh && "
        f'sh /tmp/_backup.sh "$0" && rm -f /tmp/_backup.sh'
        f"' '{presigned_url}'"
    )

    cluster_ref = cluster.aws_cluster_arn or cluster.aws_cluster_name
    aws_cmd = [
        'aws', 'ecs', 'execute-command',
        '--cluster', cluster_ref,
        '--task', task_arn,
        '--container', container,
        '--interactive',
        '--command', backup_cmd,
    ]
    if cluster.aws_region:
        aws_cmd.extend(['--region', cluster.aws_region])

    click.echo("  Connecting to container...\n")
    _exec_interactive(aws_cmd)


@db_group.command(name='restore')
@click.option('--file', 'backup_file', default=None, help='S3 key or filename to restore (from rc db list)')
@click.option('--service', default=None, help='Service to exec into')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def db_restore(ctx, backup_file, service, yes):
    """Restore the database from an S3 backup.

    \b
    Without --file, restores the most recent backup. Execs into
    the target container and runs the restore in the foreground.
    Keep the terminal open until complete.

    \b
    Examples:
      rc db restore
      rc db restore --file db-dump.tar.gz
      rc db restore --service django
    """
    config = _load_config(ctx.obj.get('config_path'))
    _set_aws_profile(config)

    backup_cfg = config.get('backup', {})
    bucket = backup_cfg.get('bucket')
    if not bucket:
        click.echo("Error: 'backup.bucket' not set in rc.yml", err=True)
        sys.exit(1)

    _bootstrap_django(config)

    service_name = service or backup_cfg.get('service', 'django')
    project_name = config['project_name']
    region = config.get('region', 'us-west-2')

    # Determine which backup to restore
    if backup_file:
        s3_key = backup_file if '/' in backup_file else backup_file
    else:
        import boto3
        s3 = boto3.client('s3', region_name=region)

        try:
            paginator = s3.get_paginator('list_objects_v2')
            objects = []
            for page in paginator.paginate(Bucket=bucket):
                objects.extend(page.get('Contents', []))
        except Exception as e:
            click.echo(f"Error listing backups: {e}", err=True)
            sys.exit(1)

        dump_files = [
            o for o in objects
            if o['Key'].endswith('.dump') or o['Key'].endswith('.tar.gz')
        ]
        # Exclude scripts and status files
        dump_files = [o for o in dump_files if not o['Key'].endswith('.sh')]

        if not dump_files:
            click.echo(f"No backups found in s3://{bucket}/", err=True)
            sys.exit(1)

        dump_files.sort(key=lambda x: x['LastModified'], reverse=True)
        s3_key = dump_files[0]['Key']

    s3_uri = f"s3://{bucket}/{s3_key}"
    filename = s3_key.split('/')[-1]
    local_file = f"/tmp/{filename}"
    is_targz = filename.endswith('.tar.gz') or filename.endswith('.tgz')

    click.echo(f"\n  Backup:  {s3_uri}")
    click.echo(f"  Target:  {service_name}")
    click.echo(f"  Format:  {'directory (tar.gz)' if is_targz else 'custom (.dump)'}")

    if not yes and not click.confirm(f"\n  This will overwrite existing data. Continue?"):
        click.echo("Aborted.")
        return

    cluster, svc, task_arn, container = _resolve_ecs_exec_target(config, service_name)

    click.echo(f"\nRestore: {s3_uri} → {svc.name}")
    click.echo(f"  This may take a while for large dumps. Keep this terminal open.\n")

    # Generate a presigned S3 URL locally so the container can download
    # via curl instead of aws s3 cp (which OOMs on large files in Fargate).
    import boto3
    s3_client = boto3.client('s3', region_name=region)
    presigned_url = s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': s3_key},
        ExpiresIn=7200,  # 2 hours
    )

    engine = _get_backup_engine(config)
    download_step, cleanup = engine.get_restore_script(filename, local_file, is_targz)

    # Build the full script. Strategy: run a background keepalive loop that
    # prints every 30s to prevent SSM idle timeout, then run the restore in
    # the foreground with -v (verbose). Between the per-table output
    # and the keepalive, the session stays alive.
    restore_script = (
        '#!/bin/sh\n'
        f'export PRESIGNED_URL="$1"\n'
        'echo "=== Database Restore ==="\n'
        f'{download_step}'
        '# Background keepalive to prevent SSM idle timeout\n'
        '(while true; do echo "  [keepalive $(date +%H:%M:%S)]"; sleep 30; done) &\n'
        'KEEPALIVE=$!\n'
        '# Run restore in foreground with verbose output\n'
        'eval $RESTORE_CMD 2>&1\n'
        'RC=$?\n'
        'kill $KEEPALIVE 2>/dev/null\n'
        'wait $KEEPALIVE 2>/dev/null\n'
        'echo ""\n'
        'if [ $RC -eq 0 ] || [ $RC -eq 1 ]; then\n'
        '  echo "=== Restore Complete (warnings are normal) ==="\n'
        'else\n'
        '  echo "=== Restore FAILED (exit code $RC) ==="\n'
        'fi\n'
        f'{cleanup}\n'
    )

    import base64
    script_b64 = base64.b64encode(restore_script.encode()).decode()

    # Decode and execute the script inside the container, passing
    # the presigned URL as $1 to avoid any escaping issues.
    restore_cmd = (
        f"sh -c 'echo {script_b64} | base64 -d > /tmp/_restore.sh && "
        f"chmod +x /tmp/_restore.sh && "
        f'sh /tmp/_restore.sh "$0" && rm -f /tmp/_restore.sh'
        f"' '{presigned_url}'"
    )

    cluster_ref = cluster.aws_cluster_arn or cluster.aws_cluster_name
    aws_cmd = [
        'aws', 'ecs', 'execute-command',
        '--cluster', cluster_ref,
        '--task', task_arn,
        '--container', container,
        '--interactive',
        '--command', restore_cmd,
    ]
    if cluster.aws_region:
        aws_cmd.extend(['--region', cluster.aws_region])

    click.echo("  Connecting to container...\n")
    _exec_interactive(aws_cmd)


@db_group.command(name='push')
@click.argument('local_file', type=click.Path(exists=True, dir_okay=False))
@click.option('--service', default=None, help='Service to exec into (default: backup.service from rc.yml)')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def db_push(ctx, local_file, service, yes):
    """Upload a LOCAL dump file and restore it into the deployed database.

    \b
    Useful for seeding test stacks with real data dumped from a local
    Docker volume:

      docker exec sentinal_postgres pg_dump -Fc -U postgres sentinal \\
          > /tmp/sentinal.dump
      rc db push /tmp/sentinal.dump

    \b
    Format auto-detected by extension:
      *.dump        -> pg_restore custom format
      *.tar.gz      -> tar xz | pg_restore directory
      *.sql         -> psql

    \b
    Examples:
      rc db push /tmp/sentinal.dump
      rc db push --service postgres backup.tar.gz
    """
    if _db_push_v2(ctx.obj.get('config_path'), local_file, service, yes):
        return
    click.echo(
        "rc db push currently requires rc.yml v2 (uses Provider.exec for "
        "execute-command). v1 not supported.",
        err=True,
    )
    raise click.exceptions.Exit(1)


@db_group.command(name='dump-local')
@click.option('--container', 'container_name', required=True,
              help='Local Docker container hosting postgres (e.g. sentinal_postgres).')
@click.option('--to', 'output_path_str', default=None,
              help='Local path to write the dump. Defaults to /tmp/rc-dumps/<project>-<timestamp>.dump.')
@click.option('--user', 'pg_user', default=None,
              help='Override POSTGRES_USER (auto-discovered from container env by default).')
@click.option('--database', 'pg_db', default=None,
              help='Override POSTGRES_DB (auto-discovered from container env by default).')
@click.option('--port', 'pg_port', type=int, default=None,
              help='Override POSTGRES_PORT (auto-discovered from container env by default).')
@click.pass_context
def db_dump_local(ctx, container_name, output_path_str, pg_user, pg_db, pg_port):
    """Dump a local Docker postgres container to a file for `rc db push`.

    \b
    Discovers POSTGRES_USER / POSTGRES_DB / POSTGRES_PORT from the
    container's own env vars so you don't have to remember per-project
    port quirks (e.g. sentinal_postgres listens on 5434 inside the
    container, not 5432).

    \b
    Examples:
      rc db dump-local --container sentinal_postgres
      rc db dump-local --container my_pg --to /tmp/x.dump
      rc db push /tmp/x.dump        # pair with rc db push for full seed flow
    """
    from pathlib import Path as _Path
    from remote_compose.dblocal import (
        DumpLocalError, default_dump_path, dump_local,
    )

    if output_path_str:
        output_path = _Path(output_path_str)
    else:
        # Derive project name for the default file name from rc.yml v2 if present.
        project = "rc-db-dump"
        try:
            from remote_compose.cli_v2 import load_rc_yml
            rc_path = _Path(ctx.obj.get('config_path') or 'rc.yml')
            if rc_path.exists():
                version, _, v2 = load_rc_yml(rc_path)
                if version == 2 and v2 is not None:
                    project = v2.project
        except Exception:
            pass
        output_path = default_dump_path(project)

    click.echo(f"\nrc db dump-local — {container_name}")
    click.echo(f"  output: {output_path}")
    try:
        result = dump_local(
            container=container_name,
            output_path=output_path,
            user=pg_user,
            database=pg_db,
            port=pg_port,
        )
    except DumpLocalError as exc:
        click.echo(f"\n  FAILED: {exc}", err=True)
        raise click.exceptions.Exit(1)

    mb = result.size_bytes / (1024 * 1024)
    click.echo(f"  user:   {result.user}")
    click.echo(f"  db:     {result.database}")
    click.echo(f"  port:   {result.port}")
    click.echo(f"  size:   {mb:.1f} MB")
    click.echo(f"\n  Next: rc db push {result.path}")


@db_group.command(name='list')
@click.pass_context
def db_list(ctx):
    """List available database backups in S3."""
    config = _load_config(ctx.obj.get('config_path'))
    _set_aws_profile(config)

    backup_cfg = config.get('backup', {})
    bucket = backup_cfg.get('bucket')
    if not bucket:
        click.echo("Error: 'backup.bucket' not set in rc.yml", err=True)
        sys.exit(1)

    project_name = config['project_name']
    region = config.get('region', 'us-west-2')

    import boto3
    s3 = boto3.client('s3', region_name=region)

    try:
        paginator = s3.get_paginator('list_objects_v2')
        objects = []
        # List all objects in the bucket (backups may be at root or under project prefix)
        for page in paginator.paginate(Bucket=bucket):
            objects.extend(page.get('Contents', []))
    except Exception as e:
        click.echo(f"Error listing backups: {e}", err=True)
        sys.exit(1)

    # Filter to backup files
    objects = [
        o for o in objects
        if o['Key'].endswith('.dump') or o['Key'].endswith('.tar.gz')
    ]

    if not objects:
        click.echo(f"No backups found in s3://{bucket}/")
        return

    objects.sort(key=lambda x: x['LastModified'], reverse=True)

    click.echo(f"\nBackups in s3://{bucket}/\n")

    header = f"  {'FILE':<50} {'SIZE':>10} {'DATE':<20}"
    click.echo(header)
    click.echo(f"  {'-' * 80}")

    for obj in objects:
        key = obj['Key']
        size = _format_size(obj['Size'])
        date = obj['LastModified'].strftime('%Y-%m-%d %H:%M:%S')
        click.echo(f"  {key:<50} {size:>10} {date:<20}")

    click.echo(f"\n  Total: {len(objects)} backups")

    # Show retention info if configured
    retention = backup_cfg.get('retention')
    if retention and len(objects) > retention:
        excess = len(objects) - retention
        click.echo(f"  Retention: {retention} (oldest {excess} could be pruned)")


cli.add_command(db_group)


# =============================================================================
# rc destroy
# =============================================================================

@cli.command()
@click.option('--infra', is_flag=True, help='Also destroy VPC, ALB, etc.')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompt')
@click.option(
    '--all-ephemeral', 'all_ephemeral', is_flag=True,
    help='Destroy every stack in the ephemeral registry (deployed via '
         'rc deploy --ttl / rc up --ttl), regardless of TTL expiry. '
         'Single confirmation prompt covers all stacks.',
)
@click.option(
    '--force-delete-secrets', 'force_delete_secrets', is_flag=True,
    help='Bypass AWS Secrets Manager 30-day recovery window when '
         'deleting per-secret blobs as part of teardown. Default '
         '(off) preserves the recovery window so a mistaken destroy '
         'is reversible — but the secret name stays reserved for 30 '
         'days, blocking re-create with the same name. Set this when '
         'you intend to immediately re-deploy with the same project '
         'name. (remote-compose-myw)',
)
@click.pass_context
def destroy(ctx, infra, yes, all_ephemeral, force_delete_secrets):
    """Tear down all services (prompts for confirmation)."""
    if all_ephemeral:
        # Reuse the reap pipeline — same registry, same provider.destroy
        # plumbing, same per-stack failure isolation. Diff vs `rc reap --all`:
        # rc destroy --all-ephemeral is the right verb when you're saying "I
        # want this gone NOW", regardless of whether you set a TTL.
        from remote_compose.ephemeral import (
            DEFAULT_REGISTRY_PATH, list_records,
        )
        targets = list_records()
        if not targets:
            click.echo(
                f"  No ephemeral stacks in registry "
                f"({DEFAULT_REGISTRY_PATH})."
            )
            return
        click.echo(
            f"\nrc destroy --all-ephemeral — {len(targets)} stack(s) "
            f"in registry:"
        )
        for r in targets:
            prof = f" profile={r.aws_profile}" if r.aws_profile else ""
            click.echo(
                f"  - {r.project} (region={r.region}{prof}) "
                f"expires_at={r.expires_at}"
            )
        _destroy_ephemeral_targets(targets, yes=yes,
                                    command_name="destroy --all-ephemeral")
        return

    from remote_compose.cli_v2 import dispatch_if_v2, load_rc_yml
    if dispatch_if_v2(ctx.obj.get('config_path'), 'destroy', yes=yes):
        # rc-e5u.46.6 followup: a single-stack `rc destroy` should also
        # unregister the project from the ephemeral registry if it was
        # ever recorded there (`rc deploy --ttl` / `rc up --ttl`).
        # Otherwise stale entries pile up and `rc list --ephemeral`
        # reports phantom stacks. Best-effort: registry mutations are
        # purely local-disk JSON; failure here doesn't matter to the
        # user since AWS resources are already destroyed.
        try:
            from remote_compose.ephemeral import remove_stack
            cfg_path = ctx.obj.get('config_path') or RC_CONFIG_FILE
            _, raw, v2 = load_rc_yml(cfg_path)
            if v2 is not None:
                ecs_cfg = ((raw.get('provider_config') or {}).get('ecs')
                           or {}) if isinstance(raw, dict) else {}
                region = ecs_cfg.get('region')
                if region:
                    remove_stack(project=v2.project, region=region)
        except Exception:
            pass
        return

    config = _load_config(ctx.obj.get('config_path'))
    _bootstrap_django(config)

    project_name = config['project_name']

    from remote_compose.models import ECSCluster

    try:
        cluster = ECSCluster.objects.get(name=config['cluster'])
    except ECSCluster.DoesNotExist:
        click.echo(f"Cluster '{config['cluster']}' not found. Nothing to destroy.")
        return

    scope = "all services and infrastructure" if infra else "all services"
    if not click.confirm(
        f"\nThis will destroy {scope} for {project_name} in {cluster.aws_region}.\n"
        f"Are you sure?"
    ):
        click.echo("Aborted.")
        return

    click.echo(f"\nRemote Compose — destroying {project_name}\n")

    # Tear down services
    _teardown_services(cluster, project_name)

    # Tear down infrastructure if requested
    if infra:
        _teardown_infrastructure(
            cluster, force_delete_secrets=force_delete_secrets,
        )

    click.echo("\n  Teardown complete.")


def _teardown_services(cluster, project_name):
    """Tear down ECS services for the cluster."""
    from remote_compose.models import ECSService as ECSServiceModel, TargetGroupConfig
    from remote_compose.services.ecs_service import ECSService

    ecs_logic = ECSService()
    services = ECSServiceModel.objects.filter(cluster=cluster)

    if not services.exists():
        click.echo("  No services to tear down.")
        return

    click.echo(f"  Tearing down {services.count()} services...")

    for svc in services:
        click.echo(f"    {svc.name}...", nl=False)
        try:
            if svc.aws_service_arn:
                try:
                    ecs_logic.update_service(svc, desired_count=0)
                except Exception:
                    pass
                try:
                    ecs_logic.delete_service(svc, force=True)
                except Exception:
                    pass
            svc.delete()
            click.echo(" removed")
        except Exception as e:
            click.echo(f" FAILED ({e})")

    # Clean up target groups
    tgs = TargetGroupConfig.objects.filter(cluster=cluster)
    if tgs.exists():
        click.echo(f"  Removing {tgs.count()} target groups...")
        from remote_compose.services.aws_client_factory import get_aws_client_factory

        factory = get_aws_client_factory()
        elbv2 = factory.get_client(
            'elbv2',
            region=cluster.aws_region,
            credential=cluster.aws_credential,
        )

        for tg in tgs:
            try:
                elbv2.delete_target_group(TargetGroupArn=tg.target_group_arn)
                tg.delete()
            except Exception:
                pass


def _teardown_infrastructure(cluster, force_delete_secrets: bool = False):
    """Tear down VPC, ALB, security groups, and other infrastructure."""
    click.echo("  Tearing down infrastructure...")

    # ALB
    try:
        lb = cluster.load_balancer
        if lb:
            click.echo(f"    ALB ({lb.alb_dns_name})...", nl=False)
            from remote_compose.services.aws_client_factory import get_aws_client_factory
            factory = get_aws_client_factory()
            elbv2 = factory.get_client(
                'elbv2',
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )
            for arn in [lb.http_listener_arn, lb.https_listener_arn]:
                if arn:
                    try:
                        elbv2.delete_listener(ListenerArn=arn)
                    except Exception:
                        pass
            try:
                elbv2.delete_load_balancer(LoadBalancerArn=lb.alb_arn)
            except Exception:
                pass
            lb.delete()
            click.echo(" removed")
    except Exception:
        pass

    # Service Connect namespace
    try:
        ns = cluster.service_connect_namespace
        if ns:
            click.echo(f"    Namespace ({ns.namespace_name})...", nl=False)
            from remote_compose.services.aws_client_factory import get_aws_client_factory
            factory = get_aws_client_factory()
            sd = factory.get_client(
                'servicediscovery',
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )
            try:
                sd.delete_namespace(Id=ns.namespace_id)
            except Exception:
                pass
            ns.delete()
            click.echo(" removed")
    except Exception:
        pass

    # Security groups
    from remote_compose.models import SecurityGroupConfig
    sgs = SecurityGroupConfig.objects.filter(cluster=cluster)
    if sgs.exists():
        click.echo(f"    {sgs.count()} security groups...", nl=False)
        from remote_compose.services.aws_client_factory import get_aws_client_factory
        factory = get_aws_client_factory()
        ec2 = factory.get_client(
            'ec2',
            region=cluster.aws_region,
            credential=cluster.aws_credential,
        )
        for sg in sgs:
            try:
                ec2.delete_security_group(GroupId=sg.security_group_id)
                sg.delete()
            except Exception:
                pass
        click.echo(" removed")

    # VPC
    try:
        vpc = cluster.vpc_infrastructure
        if vpc and vpc.is_managed:
            click.echo(f"    VPC ({vpc.vpc_id})...", nl=False)
            from remote_compose.services.vpc_service import VPCService
            vpc_service = VPCService()
            try:
                vpc_service.teardown_vpc(vpc)
            except Exception:
                pass
            click.echo(" removed")
    except Exception:
        pass

    # Secrets
    from remote_compose.models import SecretConfig
    secrets = SecretConfig.objects.filter(cluster=cluster)
    if secrets.exists():
        click.echo(f"    {secrets.count()} secrets...", nl=False)
        from remote_compose.services.aws_client_factory import get_aws_client_factory
        factory = get_aws_client_factory()
        sm = factory.get_client(
            'secretsmanager',
            region=cluster.aws_region,
            credential=cluster.aws_credential,
        )
        # remote-compose-myw: default to AWS's 30-day recovery window
        # so a mistaken `rc destroy` is reversible. Force-delete is
        # opt-in via --force-delete-secrets when the user knows they
        # want to immediately re-deploy with the same project name
        # (otherwise the reserved name blocks re-create for 30d).
        for secret in secrets:
            try:
                if force_delete_secrets:
                    sm.delete_secret(
                        SecretId=secret.secret_arn,
                        ForceDeleteWithoutRecovery=True,
                    )
                else:
                    sm.delete_secret(SecretId=secret.secret_arn)
                secret.delete()
            except Exception:
                pass
        if force_delete_secrets:
            click.echo(" removed (force)")
        else:
            click.echo(" scheduled for deletion (30d recovery window)")


# =============================================================================
# rc reap — destroy ephemeral stacks past their TTL
# =============================================================================


def _destroy_ephemeral_targets(targets, yes: bool, command_name: str) -> None:
    """Sequentially destroy each ephemeral stack via provider.destroy.

    Shared by ``rc reap`` and ``rc destroy --all-ephemeral`` (rc-e5u.44.15)
    so both code paths handle missing rc.yml, v1 entries, and provider
    failures identically. A failure on one stack does NOT stop the rest;
    succeeded stacks are removed from the local registry; the function
    exits the process non-zero if any failures occurred.
    """
    from remote_compose.ephemeral import remove_stack
    from remote_compose.cli_v2 import (
        build_deploy_context, load_rc_yml, resolve_provider,
    )

    if not yes:
        if not click.confirm(
            f"\n  Destroy these {len(targets)} stack(s)?", default=False,
        ):
            click.echo("  aborted.")
            return

    failures: list[tuple[str, str]] = []
    succeeded = 0
    for r in targets:
        click.echo(f"\n  Destroying {r.project} ({r.region})...")
        rc_path = Path(r.rc_yml_path)
        tf_dir = Path(r.terraform_dir) if r.terraform_dir else None

        # rc-e5u.46.8: rc.yml-missing fallback. The registry record always
        # carries terraform_dir; terraform state is local + self-contained,
        # so 'terraform destroy' from the emitted module reaches the same
        # AWS resources without going through the provider abstraction.
        # Without this, a stale registry entry (rc.yml deleted between
        # deploy + reap) leaves orphan AWS resources and a permanently-
        # dirty registry.
        if not rc_path.exists():
            if tf_dir and tf_dir.exists():
                click.echo(
                    f"    rc.yml at {rc_path} missing — falling back to "
                    f"terraform destroy in {tf_dir}."
                )
                from remote_compose.terraform.runner import (
                    TerraformError, TerraformRunner,
                )
                try:
                    runner = TerraformRunner(tf_dir)
                    runner.init()
                    runner.destroy()
                except TerraformError as exc:
                    click.echo(
                        f"    FAILED: terraform destroy in {tf_dir}: {exc}",
                        err=True,
                    )
                    failures.append((r.project, f"tf destroy: {exc}"))
                    continue
                except Exception as exc:
                    click.echo(
                        f"    FAILED: terraform destroy: {exc}", err=True,
                    )
                    failures.append((r.project, str(exc)))
                    continue
                remove_stack(project=r.project, region=r.region)
                succeeded += 1
                click.echo("    done (via terraform_dir fallback).")
                continue
            click.echo(
                f"    WARN: rc.yml not found at {rc_path} AND no usable "
                f"terraform_dir on this record. Leaving registry entry "
                f"in place — clean up manually.",
                err=True,
            )
            failures.append((r.project, "rc.yml + terraform_dir both missing"))
            continue
        try:
            version, raw, v2 = load_rc_yml(rc_path)
        except Exception as exc:
            click.echo(f"    FAILED: rc.yml parse: {exc}", err=True)
            failures.append((r.project, f"rc.yml parse: {exc}"))
            continue
        if version != 2 or v2 is None:
            click.echo(
                f"    FAILED: rc.yml at {rc_path} is not v2 "
                f"(only v2 stacks can be ephemeral).", err=True,
            )
            failures.append((r.project, "not v2"))
            continue
        try:
            ctx = build_deploy_context(v2, raw, rc_path)
            provider = resolve_provider(v2)
            provider.destroy(ctx)
        except Exception as exc:
            click.echo(f"    FAILED: provider.destroy: {exc}", err=True)
            failures.append((r.project, str(exc)))
            continue
        remove_stack(project=r.project, region=r.region)
        succeeded += 1
        click.echo("    done.")

    click.echo(
        f"\n  {command_name} complete: {succeeded} destroyed, "
        f"{len(failures)} failed."
    )
    if failures:
        for proj, why in failures:
            click.echo(f"    {proj}: {why}", err=True)
        sys.exit(1)


@cli.command()
@click.option('--dry-run', is_flag=True, help='List past-due stacks without destroying.')
@click.option(
    '--all', 'reap_all', is_flag=True,
    help='Destroy every ephemeral stack regardless of TTL.',
)
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompt.')
def reap(dry_run, reap_all, yes):
    """Destroy ephemeral stacks past their TTL.

    Reads the local registry (~/.config/remote-compose/ephemeral.json)
    written by `rc deploy --ttl ...`, finds entries whose expires_at is
    in the past, and runs `provider.destroy(ctx)` for each. A failure
    on one stack does not stop the rest. Successfully destroyed stacks
    are removed from the registry.
    """
    from remote_compose.ephemeral import (
        DEFAULT_REGISTRY_PATH, list_records, find_expired,
    )

    if reap_all:
        targets = list_records()
        scope = "all ephemeral"
    else:
        targets = find_expired()
        scope = "past-due"

    if not targets:
        click.echo(
            f"  No {scope} stacks in registry "
            f"({DEFAULT_REGISTRY_PATH})."
        )
        return

    click.echo(f"\nrc reap — {len(targets)} {scope} stack(s):")
    for r in targets:
        prof = f" profile={r.aws_profile}" if r.aws_profile else ""
        click.echo(
            f"  - {r.project} (region={r.region}{prof}) "
            f"expires_at={r.expires_at}"
        )

    if dry_run:
        click.echo("\n  --dry-run: nothing destroyed.")
        return

    _destroy_ephemeral_targets(targets, yes=yes, command_name="Reap")


# =============================================================================
# rc list — inventory of ephemeral stacks (rc-e5u.44.16)
# =============================================================================


def _format_relative_time(iso_ts: str, now: Optional[Any] = None) -> str:
    """Render an ISO timestamp as a short relative offset (e.g. '2h 14m').

    Past timestamps render '<delta> ago'; future timestamps render
    'in <delta>'. Used for both 'created' and 'ttl-remaining' columns
    in `rc list --ephemeral`. Granularity: days/hours/minutes only —
    seconds aren't useful at the deploy lifecycle scale.
    """
    from datetime import datetime, timezone
    from remote_compose.ephemeral import from_iso_utc
    try:
        target = from_iso_utc(iso_ts)
    except Exception:  # noqa: BLE001
        return "(invalid)"
    when = now or datetime.now(timezone.utc)
    delta = target - when
    secs = int(delta.total_seconds())
    suffix = "ago" if secs < 0 else ""
    prefix = "in " if secs >= 0 else ""
    secs = abs(secs)
    if secs < 60:
        body = f"{secs}s"
    else:
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes and not days:  # don't bother with mins past a day
            parts.append(f"{minutes}m")
        body = " ".join(parts) or f"{secs}s"
    return f"{prefix}{body}{(' ' + suffix) if suffix else ''}".strip()


@cli.command(name="list")
@click.option(
    '--ephemeral', 'ephemeral_only', is_flag=True,
    help='List ephemeral stacks from the local registry (created via '
         'rc deploy --ttl / rc up --ttl).',
)
@click.option('--json', 'as_json', is_flag=True,
              help='Emit machine-parseable JSON instead of a table.')
def list_cmd(ephemeral_only, as_json):
    """List rc-managed stacks (today: ephemeral only — see --ephemeral).

    Reads ~/.config/remote-compose/ephemeral.json and prints one row per
    stack: project | region | profile | created | ttl-remaining | rc.yml.
    Pairs with `rc reap` (destroys past-due) and
    `rc destroy --all-ephemeral` (destroys every entry on confirmation).
    """
    if not ephemeral_only:
        # Until we have non-ephemeral inventory (e.g., scan-aws-by-tag),
        # default to the same behavior as --ephemeral so the command does
        # something useful without the flag.
        ephemeral_only = True

    from remote_compose.ephemeral import DEFAULT_REGISTRY_PATH, list_records
    records = list_records()

    if as_json:
        import json as _json
        from datetime import datetime, timezone
        from remote_compose.ephemeral import from_iso_utc
        now = datetime.now(timezone.utc)
        out = []
        for r in records:
            try:
                expires_dt = from_iso_utc(r.expires_at)
                ttl_seconds = int((expires_dt - now).total_seconds())
            except Exception:  # noqa: BLE001
                ttl_seconds = None
            out.append({
                "project": r.project,
                "region": r.region,
                "aws_profile": r.aws_profile,
                "created_at": r.created_at,
                "expires_at": r.expires_at,
                "ttl_remaining_seconds": ttl_seconds,
                "expired": r.is_expired(now),
                "rc_yml_path": r.rc_yml_path,
                "terraform_dir": r.terraform_dir,
            })
        click.echo(_json.dumps(out, indent=2))
        return

    if not records:
        click.echo(
            f"  No ephemeral stacks in registry "
            f"({DEFAULT_REGISTRY_PATH})."
        )
        return

    rows: list[tuple[str, str, str, str, str, str]] = []
    for r in records:
        ttl = _format_relative_time(r.expires_at)
        if r.is_expired():
            ttl = f"EXPIRED ({ttl})"
        rows.append((
            r.project,
            r.region,
            r.aws_profile or "-",
            _format_relative_time(r.created_at),
            ttl,
            r.rc_yml_path,
        ))

    headers = ("PROJECT", "REGION", "PROFILE", "CREATED", "TTL", "RC.YML")
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    click.echo(fmt.format(*headers))
    click.echo("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        click.echo(fmt.format(*row))


# =============================================================================
# Helpers
# =============================================================================

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


# rc plan + rc migrate + rc lifecycle moved to cli_commands/plan.py
# + cli_commands/migrate.py + cli_commands/lifecycle.py
# (registered at the bottom of this file via cli.add_command)


# rc copilot + rc compose moved to cli_commands/copilot.py + cli_commands/compose.py
# (registered at the bottom of this file via cli.add_command)


# rc audit moved to cli_commands/audit.py
# (registered at the bottom of this file via cli.add_command)


# rc dev + rc fix moved to cli_commands/dev.py + cli_commands/fix.py
# (registered at the bottom of this file via cli.add_command)


# =============================================================================
# Register commands extracted into cli_commands/* modules
# =============================================================================
# As we split this file, command modules export click commands/groups that get
# registered here. cli.py stays the entry point; the modules own the bodies.

from .cli_commands.audit import audit_cmd as _audit_cmd
from .cli_commands.compose import compose_group as _compose_group
from .cli_commands.copilot import copilot_group as _copilot_group
from .cli_commands.dev import dev_group as _dev_group
from .cli_commands.doctor import doctor_cmd as _doctor_cmd
from .cli_commands.doctor import install_cmd as _install_cmd
from .cli_commands.fix import fix_group as _fix_group
from .cli_commands.lifecycle import lifecycle_cmd as _lifecycle_cmd
from .cli_commands.migrate import migrate_cmd as _migrate_cmd
from .cli_commands.plan import plan_cmd as _plan_cmd

cli.add_command(_audit_cmd)
cli.add_command(_compose_group)
cli.add_command(_copilot_group)
cli.add_command(_dev_group)
cli.add_command(_doctor_cmd)
cli.add_command(_fix_group)
cli.add_command(_install_cmd)
cli.add_command(_lifecycle_cmd)
cli.add_command(_migrate_cmd)
cli.add_command(_plan_cmd)


if __name__ == '__main__':
    cli()
