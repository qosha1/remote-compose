"""rc.yml v1 helpers — Django bootstrap, ECS exec resolution, backup engines.

Used by the still-imperative v1 commands (deploy, provision, status,
restart, logs, exec_cmd v1 fallback, db backup/restore, list_stacks,
reap, init). Lifted out of cli.py so v2-aware command modules can use
the same helpers without circular imports.

This module will shrink as v1 commands are deprecated (rc-e5u.28 cuts
start-simpli to v2) and finally be deleted as part of remote-compose-sdb.5.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click
import yaml

from ._dispatchers import _flatten_v2_to_legacy


_RC_CONFIG_FILE = 'rc.yml'


def _load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load rc.yml from the current directory or specified path."""
    path = Path(config_path) if config_path else Path.cwd() / _RC_CONFIG_FILE

    if not path.exists():
        click.echo(f"Error: {_RC_CONFIG_FILE} not found in {path.parent}", err=True)
        click.echo("Run 'rc init' to create one.", err=True)
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    if int(config.get("version", 0)) == 2:
        config = _flatten_v2_to_legacy(config)

    required = ['cluster', 'region', 'compose_file', 'project_name']
    for key in required:
        if key not in config:
            click.echo(f"Error: '{key}' is required in {_RC_CONFIG_FILE}", err=True)
            sys.exit(1)

    return config


def _bootstrap_django(config: Dict[str, Any]):
    """Set up Django with a persistent per-project SQLite database."""
    import django
    from django.conf import settings

    aws_profile = config.get('aws_profile')
    if aws_profile and 'AWS_PROFILE' not in os.environ:
        os.environ['AWS_PROFILE'] = aws_profile

    if settings.configured:
        return

    project_name = config['project_name']
    db_dir = Path.home() / '.remote-compose' / project_name
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / 'state.sqlite3'

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

    encryption_key = (
        os.environ.get('REMOTE_COMPOSE_ENCRYPTION_KEY')
        or os.environ.get('ENCRYPTION_KEY')
    )
    if not encryption_key:
        encryption_key_file = os.path.join(str(db_dir), 'encryption_key')
        if os.path.exists(encryption_key_file):
            with open(encryption_key_file) as f:
                encryption_key = f.read().strip()
        else:
            from cryptography.fernet import Fernet
            encryption_key = Fernet.generate_key().decode()
            with open(encryption_key_file, 'w') as f:
                f.write(encryption_key)
            os.chmod(encryption_key_file, 0o600)
            click.echo(
                f"\n  Generated new credential-encryption key at "
                f"{encryption_key_file} (mode 0600).",
                err=True,
            )
            click.echo(
                f"  Back this file up alongside the SQLite db at "
                f"{db_path} — without it, every credential stored by "
                f"rc becomes unrecoverable.\n",
                err=True,
            )

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

    try:
        cluster = ecs_service.import_cluster(cluster_name_or_arn=name, region=region)
        ecs_service.sync_cluster_networking(cluster)
        return cluster
    except Exception:
        pass

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
