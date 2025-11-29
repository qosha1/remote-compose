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

RC_TEMPLATE = """\
# rc.yml — Remote Compose configuration
# Docs: https://github.com/yourusername/django-remote-compose

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


def _load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load rc.yml from the current directory or specified path."""
    path = Path(config_path) if config_path else Path.cwd() / RC_CONFIG_FILE

    if not path.exists():
        click.echo(f"Error: {RC_CONFIG_FILE} not found in {path.parent}", err=True)
        click.echo("Run 'rc init' to create one.", err=True)
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f)

    required = ['cluster', 'region', 'compose_file', 'project_name']
    for key in required:
        if key not in config:
            click.echo(f"Error: '{key}' is required in {RC_CONFIG_FILE}", err=True)
            sys.exit(1)

    return config


def _bootstrap_django(config: Dict[str, Any]):
    """Set up Django with a persistent per-project SQLite database."""
    import django
    from django.conf import settings

    # Set AWS profile before any boto3 calls
    aws_profile = config.get('aws_profile')
    if aws_profile and 'AWS_PROFILE' not in os.environ:
        os.environ['AWS_PROFILE'] = aws_profile

    if settings.configured:
        return

    project_name = config['project_name']
    db_dir = Path.home() / '.remote-compose' / project_name
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / 'state.sqlite3'

    # Generate and persist a unique SECRET_KEY per project
    secret_key_file = os.path.join(str(db_dir), 'secret_key')
    if os.path.exists(secret_key_file):
        with open(secret_key_file) as f:
            secret_key = f.read().strip()
    else:
        import secrets
        secret_key = secrets.token_urlsafe(50)
        with open(secret_key_file, 'w') as f:
            f.write(secret_key)
        os.chmod(secret_key_file, 0o600)

    # Pick up encryption key from environment
    encryption_key = os.environ.get('REMOTE_COMPOSE_ENCRYPTION_KEY') or os.environ.get('ENCRYPTION_KEY')
    if not encryption_key:
        from cryptography.fernet import Fernet
        encryption_key = Fernet.generate_key().decode()

    settings.configure(
        DEBUG=False,
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': str(db_path),
            }
        },
        INSTALLED_APPS=[
            'django.contrib.contenttypes',
            'django.contrib.auth',
            'remote_compose',
        ],
        DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
        USE_TZ=True,
        REMOTE_COMPOSE={
            'ENCRYPTION_KEY': encryption_key,
            'DEPLOYMENT_TIMEOUT': 600,
            'AWS_DEFAULT_REGION': config.get('region', 'us-west-2'),
        },
        CACHES={
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            }
        },
        SECRET_KEY=secret_key,
    )

    django.setup()

    # Only run migrations when the schema version changes
    import remote_compose
    schema_version = getattr(remote_compose, '__version__', '0.0.0')
    version_file = os.path.join(str(db_dir), 'schema_version')

    needs_migrate = True
    if os.path.exists(version_file):
        with open(version_file) as f:
            stored_version = f.read().strip()
        if stored_version == schema_version:
            needs_migrate = False

    if needs_migrate:
        from django.core.management import call_command
        call_command('migrate', '--run-syncdb', verbosity=0)
        with open(version_file, 'w') as f:
            f.write(schema_version)


def _get_or_create_cluster(config: Dict[str, Any]):
    """Get the cluster from the local DB, or import/create from AWS."""
    from remote_compose.models import ECSCluster
    from remote_compose.services import ECSService

    name = config['cluster']
    region = config.get('region', 'us-west-2')

    try:
        return ECSCluster.objects.get(name=name)
    except ECSCluster.DoesNotExist:
        pass

    ecs_service = ECSService()

    # Try to import from AWS
    try:
        cluster = ecs_service.import_cluster(cluster_name_or_arn=name, region=region)
        ecs_service.sync_cluster_networking(cluster)
        return cluster
    except Exception:
        pass

    # Create new
    cluster = ecs_service.create_cluster(
        name=name,
        region=region,
        capacity_providers=['FARGATE', 'FARGATE_SPOT'],
    )
    cluster.launch_type = ECSCluster.LaunchType.FARGATE
    cluster.save()
    ecs_service.sync_cluster_networking(cluster)
    return cluster


def _write_service_config(config: Dict[str, Any]) -> str:
    """Write a temporary service config YAML from the rc.yml services block."""
    import tempfile

    services_block = config.get('services', {})
    service_config = {'services': {}}

    for svc_name, svc_opts in services_block.items():
        service_config['services'][svc_name] = {
            'cpu': str(svc_opts.get('cpu', '256')),
            'memory': str(svc_opts.get('memory', '512')),
            'desired_count': svc_opts.get('desired_count', 1),
            'type': svc_opts.get('type', 'application'),
        }
        for optional in ('health_check_path', 'public', 'port', 'default_target', 'ephemeral_storage'):
            if optional in svc_opts:
                service_config['services'][svc_name][optional] = svc_opts[optional]

    fd, path = tempfile.mkstemp(suffix='.yml', prefix='rc_service_config_')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(service_config, f, default_flow_style=False)

    return path


def _resolve_compose_path(config: Dict[str, Any]) -> Path:
    """Resolve the compose file path relative to CWD."""
    path = Path.cwd() / config['compose_file']
    if not path.exists():
        click.echo(f"Error: Compose file not found: {path}", err=True)
        sys.exit(1)
    return path


def _step_counter():
    """Create a step counter for clean progress output."""
    state = {'current': 0, 'total': 0}

    def set_total(total):
        state['total'] = total

    def step(msg):
        state['current'] += 1
        click.echo(f"  [{state['current']}/{state['total']}] {msg}...", nl=False)

    def done(detail=''):
        suffix = f" ({detail})" if detail else ''
        click.echo(f" done{suffix}")

    def fail(detail=''):
        suffix = f" ({detail})" if detail else ''
        click.echo(f" FAILED{suffix}")

    return set_total, step, done, fail


# =============================================================================
# Helpers — ECS exec target resolution
# =============================================================================

def _resolve_ecs_exec_target(config, service_name, container_name=None):
    """Resolve an ECS service to a running task for execute-command.

    Returns (cluster, svc, task_arn, container).
    """
    import shutil

    if not shutil.which('session-manager-plugin'):
        click.echo(
            "Error: session-manager-plugin is not installed.\n"
            "Install it: https://docs.aws.amazon.com/systems-manager/latest/userguide/"
            "session-manager-working-with-install-plugin.html",
            err=True,
        )
        sys.exit(1)

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
    svc = services.filter(name=service_name).first() or \
          services.filter(name=f"{project_name}-{service_name}").first()

    if not svc:
        available = [s.name for s in services]
        click.echo(f"Error: Service '{service_name}' not found.", err=True)
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

    if not container_name:
        try:
            tasks = ecs_svc.describe_tasks(cluster, [task_arn])
            if tasks and tasks[0].get('containers'):
                container_name = tasks[0]['containers'][0]['name']
            else:
                click.echo("Error: Could not determine container name.", err=True)
                sys.exit(1)
        except Exception as e:
            click.echo(f"Error describing task: {e}", err=True)
            sys.exit(1)

    return cluster, svc, task_arn, container_name


def _set_aws_profile(config):
    """Set AWS_PROFILE from rc.yml if configured."""
    aws_profile = config.get('aws_profile')
    if aws_profile and 'AWS_PROFILE' not in os.environ:
        os.environ['AWS_PROFILE'] = aws_profile


def _exec_interactive(aws_cmd):
    """Run an interactive aws ecs execute-command.

    Reopens stdin from /dev/tty if needed (e.g., when invoked via pipe)
    so the SSM session gets a real terminal.
    """
    import subprocess

    if sys.stdin.isatty():
        result = subprocess.run(aws_cmd)
    else:
        try:
            tty = open('/dev/tty', 'r')
            result = subprocess.run(aws_cmd, stdin=tty)
            tty.close()
        except OSError:
            result = subprocess.run(aws_cmd)

    sys.exit(result.returncode)


def _format_size(size_bytes):
    """Format bytes into human-readable size."""
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# =============================================================================
# Database backup engine abstraction
# =============================================================================

class DatabaseBackupEngine:
    """Abstract interface for database backup/restore shell scripts."""

    def get_dump_script(self, s3_uri):
        """Return a shell script string that dumps the database to /tmp/backup.dump."""
        raise NotImplementedError

    def get_restore_script(self, filename, local_file, is_targz):
        """Return (download_step, cleanup) shell script fragments for restore."""
        raise NotImplementedError


class PostgresBackupEngine(DatabaseBackupEngine):
    """PostgreSQL backup/restore via pg_dump and pg_restore."""

    def get_dump_script(self, s3_uri):
        return (
            '#!/bin/sh\n'
            'export PRESIGNED_URL="$1"\n'
            'echo "=== Database Backup ==="\n'
            '# Background keepalive to prevent SSM idle timeout\n'
            '(while true; do echo "  [keepalive $(date +%H:%M:%S)]"; sleep 30; done) &\n'
            'KEEPALIVE=$!\n'
            'echo "[1/3] Dumping database..."\n'
            'PGPASSWORD=$POSTGRES_PASSWORD pg_dump -Fc -v \\\n'
            '  -h ${POSTGRES_HOST:-postgres} \\\n'
            '  -p ${POSTGRES_PORT:-5432} \\\n'
            '  -U ${POSTGRES_USER:-postgres} \\\n'
            '  ${POSTGRES_DB:-postgres} \\\n'
            '  > /tmp/backup.dump 2>&1\n'
            'RC=$?\n'
            'if [ $RC -ne 0 ]; then\n'
            '  kill $KEEPALIVE 2>/dev/null\n'
            '  echo "pg_dump failed (exit $RC)"\n'
            '  rm -f /tmp/backup.dump\n'
            '  exit 1\n'
            'fi\n'
            'DUMP_SIZE=$(ls -lh /tmp/backup.dump | awk "{print \\$5}")\n'
            'echo "[2/3] Dump complete: $DUMP_SIZE"\n'
            'echo "[3/3] Uploading to S3..."\n'
            'curl -sS -X PUT -T /tmp/backup.dump "$PRESIGNED_URL"\n'
            'echo ""\n'
            'kill $KEEPALIVE 2>/dev/null\n'
            'wait $KEEPALIVE 2>/dev/null\n'
            'rm -f /tmp/backup.dump\n'
            f'echo "=== Backup Complete: {s3_uri} ==="\n'
        )

    def get_restore_script(self, filename, local_file, is_targz):
        pg_common_opts = (
            '-h ${POSTGRES_HOST:-postgres} '
            '-p ${POSTGRES_PORT:-5432} '
            '-U ${POSTGRES_USER:-postgres} '
            '-d ${POSTGRES_DB:-postgres} '
            '--no-owner --clean --if-exists'
        )

        if is_targz:
            download_step = (
                'echo "[1/3] Downloading and extracting archive..."\n'
                'mkdir -p /tmp/dump_restore\n'
                'curl -sS "$PRESIGNED_URL" | tar xz -C /tmp/dump_restore\n'
                'DUMP_DIR=$(find /tmp/dump_restore -maxdepth 1 -type d ! -path /tmp/dump_restore | head -1)\n'
                '[ -z "$DUMP_DIR" ] && DUMP_DIR=/tmp/dump_restore\n'
                'echo "[2/3] Extracted to $DUMP_DIR"\n'
                'echo "[3/3] Restoring database..."\n'
                f'RESTORE_CMD="PGPASSWORD=$POSTGRES_PASSWORD pg_restore -Fd -v {pg_common_opts} $DUMP_DIR"\n'
            )
            cleanup = 'rm -rf /tmp/dump_restore /tmp/restore.log'
        else:
            download_step = (
                f'echo "[1/3] Downloading {filename}..."\n'
                f'curl -sS -o {local_file} "$PRESIGNED_URL"\n'
                'echo "[2/3] Downloaded"\n'
                'echo "[3/3] Restoring database..."\n'
                f'RESTORE_CMD="PGPASSWORD=$POSTGRES_PASSWORD pg_restore -v {pg_common_opts} {local_file}"\n'
            )
            cleanup = f'rm -f {local_file} /tmp/restore.log'

        return download_step, cleanup


def _get_backup_engine(config):
    """Get the backup engine based on rc.yml config."""
    engine = config.get('backup', {}).get('engine', 'postgresql')
    engines = {'postgresql': PostgresBackupEngine()}
    if engine not in engines:
        click.echo(
            f"Error: Unsupported backup engine: {engine}. "
            f"Supported: {', '.join(engines)}",
            err=True,
        )
        sys.exit(1)
    return engines[engine]


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
def init():
    """Generate an rc.yml template in the current directory."""
    target = Path.cwd() / RC_CONFIG_FILE
    if target.exists():
        click.echo(f"{RC_CONFIG_FILE} already exists in {Path.cwd()}")
        if not click.confirm("Overwrite?", default=False):
            return

    target.write_text(RC_TEMPLATE)
    click.echo(f"Created {RC_CONFIG_FILE}")
    click.echo("Edit it with your cluster, region, and service configuration.")


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
@click.option('--tag', default='latest', help='Image tag (default: latest)')
@click.option('--code-only', is_flag=True, help='Deploy only code services (skip infrastructure)')
@click.option('--services', 'selected_services', default=None, help='Comma-separated services to deploy')
@click.pass_context
def deploy(ctx, no_build, dry_run, tag, code_only, selected_services):
    """Build images, push to ECR, and deploy all services."""
    if code_only and selected_services:
        click.echo("Error: --code-only and --services are mutually exclusive", err=True)
        sys.exit(1)

    services_list = None
    if selected_services:
        services_list = [s.strip() for s in selected_services.split(',') if s.strip()]

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
# rc status
# =============================================================================

@cli.command()
@click.pass_context
def status(ctx):
    """Show service status table."""
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
    """Execute a command in a running ECS task.

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
@click.pass_context
def secrets_push(ctx):
    """Push secrets from env files defined in rc.yml."""
    config = _load_config(ctx.obj.get('config_path'))
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
@click.pass_context
def destroy(ctx, infra):
    """Tear down all services (prompts for confirmation)."""
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
        _teardown_infrastructure(cluster)

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


def _teardown_infrastructure(cluster):
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
        for secret in secrets:
            try:
                sm.delete_secret(
                    SecretId=secret.secret_arn,
                    ForceDeleteWithoutRecovery=True,
                )
                secret.delete()
            except Exception:
                pass
        click.echo(" removed")


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


if __name__ == '__main__':
    cli()
