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


def _detect_empty_file_secrets(v2, region: str, aws_profile: Optional[str],
                                file_secrets: list) -> list[str]:
    """Return SM secret names that exist but have empty / zero-key blobs.

    Used by `rc deploy` (rc-e5u.44.20) to catch the silent-fail-cascade where
    terraform created a secret resource (placeholder blob) but `rc secrets
    push` never populated it — every task on the new task def then fails
    with 'retrieved secret from Secrets Manager did not contain json key X'.
    Returns names that need a push; empty list when everything is populated.
    Missing secrets (NotFoundException) are NOT treated as empty — terraform
    hasn't applied yet and the deploy will create them.
    """
    import json
    import boto3
    from botocore.exceptions import ClientError

    session = boto3.Session(region_name=region, profile_name=aws_profile)
    sm = session.client("secretsmanager")
    empty: list[str] = []
    for sec in file_secrets:
        sm_name = f"{v2.project}/{sec.name}"
        try:
            resp = sm.get_secret_value(SecretId=sm_name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ResourceNotFoundException", "InvalidRequestException"}:
                # terraform hasn't applied yet, OR the secret is in a
                # PendingDeletion state — neither qualifies as 'empty +
                # populatable'. Caller's deploy will create / recreate it.
                continue
            raise
        body = resp.get("SecretString") or ""
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            # Non-JSON SM blob (manually-set raw value) — out of scope here.
            continue
        if isinstance(parsed, dict) and not parsed:
            empty.append(sec.name)
    return empty


def _secrets_push_v2(config_path: Optional[str], rollout: bool = True) -> bool:
    """If rc.yml is v2, push file-sourced secrets and return True.

    Returns False for v1 so the caller falls back to the legacy pipeline.
    Uploads each file-sourced secret as a JSON blob {KEY: value, ...} to
    the SM secret the provider created (name = "<project>/<secret_name>").
    This matches the ECS JSON-key syntax the provider emits in task defs.
    """
    import json
    from pathlib import Path as _Path
    path = _Path(config_path) if config_path else _Path.cwd() / RC_CONFIG_FILE
    if not path.exists():
        return False

    from remote_compose.cli_v2 import load_rc_yml
    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        return False

    from remote_compose.envfile import EnvFileError, parse as parse_env
    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    region = ecs_cfg.get("region")
    aws_profile = ecs_cfg.get("aws_profile")
    if not region:
        click.echo("rc.yml v2: provider_config.ecs.region is required.", err=True)
        raise click.exceptions.Exit(1)

    # Expand env_file_auto -> per-file SecretRefs first so the upload step
    # sees the same shape regardless of how the user declared them. Mirrors
    # cli_v2.build_deploy_context so deploy and secrets-push stay in sync.
    from remote_compose.cli_v2 import _expand_env_file_auto, _parse_compose_services
    compose_path = _Path(v2.compose_file)
    if not compose_path.is_absolute():
        compose_path = (path.parent / compose_path).resolve()
    compose_services = _parse_compose_services(compose_path) if compose_path.exists() else {}
    expanded_secrets, _ = _expand_env_file_auto(
        list(v2.secrets or []), compose_services, compose_path,
    )
    file_secrets = [s for s in expanded_secrets if s.source == "file"]
    if not file_secrets:
        click.echo("No file-sourced secrets in rc.yml.")
        return True

    import boto3
    session = boto3.Session(region_name=region, profile_name=aws_profile)
    sm = session.client("secretsmanager")

    click.echo(f"\nRemote Compose v2 — pushing secrets for {v2.project} in {region}\n")

    project_dir = path.parent
    total_keys = 0
    for sec in file_secrets:
        env_path = _Path(sec.path)
        if not env_path.is_absolute():
            env_path = (project_dir / env_path).resolve()
        try:
            body = parse_env(env_path)
        except EnvFileError as exc:
            click.echo(f"  {sec.name}: {exc}", err=True)
            raise click.exceptions.Exit(1)
        if not body:
            click.echo(f"  {sec.name}: {env_path} has no entries, skipping")
            continue
        sm_name = f"{v2.project}/{sec.name}"
        click.echo(f"  {sec.name} → {sm_name} ({len(body)} keys)...", nl=False)
        sm.put_secret_value(SecretId=sm_name, SecretString=json.dumps(body))
        total_keys += len(body)
        click.echo(" done")

    click.echo(f"\n  Pushed {total_keys} keys across {len(file_secrets)} secret(s).")

    if rollout and file_secrets:
        cluster = ecs_cfg.get("cluster") or f"{v2.project}-cluster"
        ecs = session.client("ecs")
        services = sorted(v2.services.keys()) if v2.services else []
        if services:
            click.echo(f"\n  Forcing new deployment on {len(services)} service(s)...")
            for svc_name in services:
                try:
                    ecs.update_service(
                        cluster=cluster, service=svc_name, forceNewDeployment=True
                    )
                    click.echo(f"    {svc_name} ✓")
                except Exception as exc:
                    click.echo(f"    {svc_name}: rollout failed — {exc}", err=True)
    return True


def _db_push_v2(
    config_path: Optional[str], local_file: str, service: Optional[str], yes: bool
) -> bool:
    """rc db push for v2 stacks: upload local dump → S3 → exec restore.

    Returns True when handled (rc.yml v2 detected), else False so the
    caller can decide what to do (currently: exit with error).
    """
    from datetime import datetime, timezone
    from pathlib import Path as _Path
    path = _Path(config_path) if config_path else _Path.cwd() / RC_CONFIG_FILE
    if not path.exists():
        return False

    from remote_compose.cli_v2 import (
        build_deploy_context, load_rc_yml, resolve_provider,
    )
    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        return False

    backup_cfg = v2.backup
    if not backup_cfg or not backup_cfg.bucket:
        click.echo(
            "rc db push: rc.yml v2 must declare backup.bucket (S3 staging "
            "for the dump upload).",
            err=True,
        )
        raise click.exceptions.Exit(1)
    bucket = backup_cfg.bucket
    target_service = service or backup_cfg.service
    if not target_service:
        click.echo(
            "rc db push: backup.service not set in rc.yml and --service not "
            "passed. Specify which service container has psql/pg_restore.",
            err=True,
        )
        raise click.exceptions.Exit(1)
    if target_service not in v2.services:
        click.echo(
            f"rc db push: service {target_service!r} not in rc.yml.", err=True,
        )
        raise click.exceptions.Exit(1)

    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    region = ecs_cfg.get("region")
    aws_profile = ecs_cfg.get("aws_profile")

    local = _Path(local_file)
    fmt = _detect_dump_format(local.name)
    size_mb = local.stat().st_size / (1024 * 1024)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    s3_key = f"{v2.project}/pushed/{timestamp}-{local.name}"
    s3_uri = f"s3://{bucket}/{s3_key}"

    click.echo(f"\nrc db push v2 — {v2.project}")
    click.echo(f"  local:   {local} ({size_mb:.1f} MB)")
    click.echo(f"  upload:  {s3_uri}")
    click.echo(f"  target:  {target_service} container in us-west-1")
    click.echo(f"  format:  {fmt}")
    if not yes and not click.confirm("\n  This will overwrite existing data. Continue?"):
        click.echo("  Aborted.")
        return True

    import boto3
    session = boto3.Session(region_name=region, profile_name=aws_profile)
    s3 = session.client("s3")
    click.echo(f"\n  [1/3] Uploading {local.name} to {s3_uri}...")
    s3.upload_file(str(local), bucket, s3_key)
    click.echo(f"        upload complete")

    presigned = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=7200,
    )

    deploy_ctx = build_deploy_context(v2, raw, path)
    provider = resolve_provider(v2)

    restore_script = _build_restore_script(local.name, presigned, fmt)
    click.echo(f"\n  [2/3] Connecting to {target_service} container, "
               f"restoring (this may take a while for large dumps)...\n")
    result = provider.exec(
        deploy_ctx, target_service,
        ["sh", "-c", restore_script],
        timeout=3600,
    )
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    click.echo(f"\n  [3/3] Cleaning up s3://{bucket}/{s3_key}...")
    try:
        s3.delete_object(Bucket=bucket, Key=s3_key)
        click.echo(f"        deleted (kept locally: {local})")
    except Exception as exc:
        click.echo(
            f"        warning: failed to delete S3 object: {exc}",
            err=True,
        )
    if result.exit_code != 0:
        click.echo(
            f"\n  rc db push: restore exited {result.exit_code}",
            err=True,
        )
        raise click.exceptions.Exit(result.exit_code)
    click.echo("\n  rc db push: complete.")
    return True


def _detect_dump_format(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar+pg_restore"
    if name.endswith(".sql"):
        return "psql"
    if name.endswith(".dump") or name.endswith(".pgdump") or name.endswith(".bin"):
        return "pg_restore"
    raise click.exceptions.UsageError(
        f"rc db push: cannot detect dump format from filename {filename!r} — "
        f"expected one of .dump / .pgdump / .sql / .tar.gz"
    )


def _build_restore_script(filename: str, presigned_url: str, fmt: str) -> str:
    """Generate a /bin/sh script that downloads the dump and restores it.

    Runs inside the target ECS container. The restore CLI is chosen by
    fmt; common Postgres env vars (POSTGRES_HOST/USER/DB/PASSWORD) are
    expected to be in the container's env (provider already wires them
    via secrets).

    Bootstraps a download tool (curl preferred, wget fallback, apt-get
    install curl as a last resort) since stock postgres:17 doesn't ship
    curl by default.
    """
    pg_common = (
        "-h ${POSTGRES_HOST:-postgres} "
        "-p ${POSTGRES_PORT:-5432} "
        "-U ${POSTGRES_USER:-postgres} "
        "-d ${POSTGRES_DB:-postgres}"
    )
    bootstrap = (
        "if command -v curl >/dev/null 2>&1 && [ -f /etc/ssl/certs/ca-certificates.crt ]; then "
        "    DL='curl -fsSL -o'; "
        "elif command -v wget >/dev/null 2>&1 && [ -f /etc/ssl/certs/ca-certificates.crt ]; then "
        "    DL='wget -q -O'; "
        "else echo '[rc db push] bootstrapping curl + ca-certificates...'; "
        "    apt-get update >/dev/null 2>&1 && "
        "    apt-get install -y --no-install-recommends curl ca-certificates >/dev/null 2>&1 && "
        "    DL='curl -fsSL -o'; "
        "fi; "
        "[ -z \"$DL\" ] && { echo 'no download tool available'; exit 1; }"
    )
    if fmt == "tar+pg_restore":
        download = (
            "mkdir -p /tmp/_rcpush; "
            f'$DL /tmp/_rcpush/{filename} "{presigned_url}"; '
            f"tar -xzf /tmp/_rcpush/{filename} -C /tmp/_rcpush; "
            "DUMP_DIR=$(find /tmp/_rcpush -maxdepth 1 -type d "
            "! -path /tmp/_rcpush | head -1); "
            f"PGPASSWORD=$POSTGRES_PASSWORD pg_restore -Fd -v "
            f"{pg_common} --no-owner --clean --if-exists \"$DUMP_DIR\""
        )
    elif fmt == "pg_restore":
        download = (
            f'$DL /tmp/_rcpush.dump "{presigned_url}"; '
            f"PGPASSWORD=$POSTGRES_PASSWORD pg_restore -v "
            f"{pg_common} --no-owner --clean --if-exists /tmp/_rcpush.dump"
        )
    elif fmt == "psql":
        download = (
            f'$DL /tmp/_rcpush.sql "{presigned_url}"; '
            f"PGPASSWORD=$POSTGRES_PASSWORD psql {pg_common} -f /tmp/_rcpush.sql"
        )
    else:
        raise click.exceptions.UsageError(f"unknown format {fmt!r}")
    return f"set -e; {bootstrap}; {download}; rc=$?; rm -rf /tmp/_rcpush*; exit $rc"


def _exec_v2(config_path: Optional[str], service: str, command: list) -> bool:
    """Route 'rc exec' through Provider.exec for v2 stacks; return True
    when handled. False signals the caller to fall back to the legacy
    v1 path.
    """
    from pathlib import Path as _Path
    path = _Path(config_path) if config_path else _Path.cwd() / RC_CONFIG_FILE
    if not path.exists():
        return False

    from remote_compose.cli_v2 import (
        build_deploy_context, load_rc_yml, resolve_provider,
    )
    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception:
        return False
    if version != 2 or v2 is None:
        return False

    if service not in v2.services:
        click.echo(
            f"rc exec: service {service!r} not in rc.yml services. "
            f"Available: {', '.join(sorted(v2.services))}",
            err=True,
        )
        raise click.exceptions.Exit(1)

    ctx = build_deploy_context(v2, raw, path)
    provider = resolve_provider(v2)

    interactive = sys.stdin.isatty()
    result = provider.exec(ctx, service, command, interactive=interactive)
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    if result.exit_code != 0:
        raise click.exceptions.Exit(result.exit_code)
    return True


def _flatten_v2_to_legacy(v2: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a v2 rc.yml into the v1 flat dict shape that legacy helpers
    (backup/restore/list, exec, logs, status) expect.

    Only the ECS provider is supported here — backup tooling is ECS-specific
    and would be per-provider if/when other providers ship their own.
    """
    ecs = (v2.get("provider_config") or {}).get("ecs") or {}
    legacy: Dict[str, Any] = dict(v2)  # preserve backup, secrets, services, domain
    legacy["project_name"] = v2.get("project", v2.get("project_name", ""))
    legacy["compose_file"] = v2.get("compose_file", "docker-compose.yml")
    if "cluster" in ecs:
        legacy["cluster"] = ecs["cluster"]
    if "region" in ecs:
        legacy["region"] = ecs["region"]
    if "aws_profile" in ecs:
        legacy["aws_profile"] = ecs["aws_profile"]
    return legacy


def _load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load rc.yml from the current directory or specified path."""
    path = Path(config_path) if config_path else Path.cwd() / RC_CONFIG_FILE

    if not path.exists():
        click.echo(f"Error: {RC_CONFIG_FILE} not found in {path.parent}", err=True)
        click.echo("Run 'rc init' to create one.", err=True)
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    if int(config.get("version", 0)) == 2:
        config = _flatten_v2_to_legacy(config)

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
@click.pass_context
def up(ctx, from_compose, public_service, region, aws_profile,
       testing_defaults, ttl, dev_mode):
    """One-shot: scaffold rc.yml (if missing), deploy, push secrets, print ALB URL.

    The "I have a docker-compose.yml — get me a running stack" command. With
    --from-compose, rc generates a v2 rc.yml on the fly when none exists,
    then runs the deploy and secrets-push pipeline. Idempotent: rerun on an
    unchanged config does the no-op terraform apply and a forced rollout.
    """
    config_path = ctx.obj.get('config_path') or RC_CONFIG_FILE
    target = Path(config_path)

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
@click.pass_context
def destroy(ctx, infra, yes, all_ephemeral):
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


@cli.command(name='plan')
@click.pass_context
def plan_cmd(ctx):
    """Show terraform plan for the current rc.yml v2 config."""
    from remote_compose.cli_v2 import dispatch_if_v2
    if dispatch_if_v2(ctx.obj.get('config_path'), 'plan'):
        return
    click.echo("rc plan requires a rc.yml v2 config. Run `rc migrate` first.",
               err=True)
    raise click.exceptions.Exit(1)


@cli.command(name='doctor')
@click.option('--fix', is_flag=True,
              help='Attempt to install/upgrade missing deps via the platform package manager.')
def doctor_cmd(fix):
    """Check that terraform/docker/python/AWS are set up correctly."""
    from remote_compose import doctor
    report = doctor.run()
    click.echo(report.render_table())
    if not report.ok and not fix:
        click.echo("\n  Some hard requirements are missing. Re-run with --fix "
                   "to attempt repair, or `rc install`.", err=True)
        raise click.exceptions.Exit(1)
    if fix and not report.ok:
        click.echo("\n  Attempting fixes...\n")
        outcomes = doctor.apply_fixes(report)
        for name, ok, detail in outcomes:
            mark = "✓" if ok else "✗"
            click.echo(f"    {mark} {name}: {detail}")
        click.echo("\n  Re-running checks...\n")
        report = doctor.run()
        click.echo(report.render_table())
        if not report.ok:
            raise click.exceptions.Exit(1)


@cli.command(name='install')
@click.pass_context
def install_cmd(ctx):
    """Install/upgrade every prerequisite (alias for `rc doctor --fix`)."""
    ctx.invoke(doctor_cmd, fix=True)


@cli.command(name='migrate')
@click.option('--in', 'in_path', default='rc.yml', show_default=True,
              help='Path to rc.yml v1 input.')
@click.option('--out', 'out_path', default='rc.v2.yml', show_default=True,
              help='Path to write rc.yml v2 output.')
@click.option('--force', is_flag=True,
              help='Write output even if unmigratable fields are present.')
def migrate_cmd(in_path, out_path, force):
    """Convert a v1 rc.yml to v2 schema."""
    import yaml
    from remote_compose.config import v1_schema
    from remote_compose.config.migrate import migrate as _migrate

    raw = v1_schema.load(in_path)
    if not v1_schema.is_v1(raw):
        click.echo(f"{in_path} is already v2; nothing to migrate.")
        return

    result = _migrate(raw, strict=False)

    for w in result.warnings:
        click.echo(f"warning: {w}", err=True)
    for u in result.unmigratable:
        click.echo(f"unmigratable: {u}", err=True)

    if result.unmigratable and not force:
        click.echo(
            f"refusing to write {out_path}: {len(result.unmigratable)} "
            f"unmigratable field(s). Re-run with --force to write anyway.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    with open(out_path, 'w') as f:
        yaml.safe_dump(result.v2, f, sort_keys=False)
    click.echo(f"Wrote {out_path} (version 2).")


# =============================================================================
# rc lifecycle — run a named hook declared in rc.yml v2
# =============================================================================

@cli.command(name='lifecycle')
@click.argument('hook')
@click.argument('service', required=False, default=None)
@click.pass_context
def lifecycle_cmd(ctx, hook, service):
    """Run a named lifecycle hook declared on a service in rc.yml.

    \b
    Examples:
      rc lifecycle migrate                  # one service declares it
      rc lifecycle migrate django           # disambiguate explicitly
      rc lifecycle createsuperuser
    """
    from pathlib import Path as _Path
    config_path = ctx.obj.get('config_path')
    path = _Path(config_path) if config_path else _Path.cwd() / RC_CONFIG_FILE
    if not path.exists():
        click.echo(f"rc lifecycle: {path} not found.", err=True)
        raise click.exceptions.Exit(1)

    from remote_compose.cli_v2 import (
        build_deploy_context, load_rc_yml, resolve_provider,
    )
    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        click.echo(
            "rc lifecycle requires rc.yml v2 (declares services[*].lifecycle).",
            err=True,
        )
        raise click.exceptions.Exit(1)

    # Resolve which service declares this hook.
    declarers = [
        name for name, svc in v2.services.items() if hook in (svc.lifecycle or {})
    ]
    if service is not None:
        if service not in v2.services:
            click.echo(f"rc lifecycle: unknown service {service!r}.", err=True)
            raise click.exceptions.Exit(1)
        if service not in declarers:
            click.echo(
                f"rc lifecycle: service {service!r} does not declare hook {hook!r}.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        target = service
    else:
        if not declarers:
            click.echo(
                f"rc lifecycle: no service declares hook {hook!r}. "
                f"Add a `lifecycle.{hook}` block to a service in rc.yml.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        if len(declarers) > 1:
            click.echo(
                f"rc lifecycle: multiple services declare hook {hook!r}: "
                f"{', '.join(declarers)}. Disambiguate: rc lifecycle {hook} <service>.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        target = declarers[0]

    spec = v2.services[target].lifecycle[hook]
    deploy_ctx = build_deploy_context(v2, raw, path)
    provider = resolve_provider(v2)

    # run_once: probe first; skip if probe exits 0.
    if spec.run_once and spec.probe:
        probe_result = provider.exec(deploy_ctx, target, list(spec.probe))
        if probe_result.exit_code == 0:
            click.echo(
                f"rc lifecycle: {hook} on {target} already done (probe exit 0); skipping.",
            )
            return

    click.echo(f"rc lifecycle: running {hook} on {target}...")
    result = provider.exec(
        deploy_ctx, target, list(spec.command),
        interactive=spec.interactive,
    )
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, nl=False, err=True)
    if result.exit_code != 0:
        raise click.exceptions.Exit(result.exit_code)


# =============================================================================
# rc copilot import — migrate AWS Copilot apps to rc.yml v2
# =============================================================================

@cli.group(name='copilot')
def copilot_group():
    """AWS Copilot migration. (Copilot is end-of-support 2026-06-12.)"""
    pass


@copilot_group.command(name='import')
@click.option('--from', 'from_dir', default='./copilot', show_default=True,
              type=click.Path(exists=True, file_okay=False),
              help='Path to the source copilot/ directory.')
@click.option('--out', 'out_dir', default='.', show_default=True,
              type=click.Path(file_okay=False),
              help='Where to write rc.yml + docker-compose.yml + IMPORT_SUMMARY.md.')
@click.option('--env', 'env_name', default=None,
              help="Copilot environment to pin (production/staging/dev). "
                   "If unset, base manifest values are used and "
                   "${COPILOT_ENVIRONMENT_NAME} stays literal in secret ARNs.")
@click.option('--project', 'project_name', default=None,
              help='rc.yml v2 project field. Defaults to the parent dir name '
                   'of the copilot/ tree.')
@click.option('--force', is_flag=True,
              help='Overwrite existing rc.yml / docker-compose.yml in --out.')
def copilot_import(from_dir, out_dir, env_name, project_name, force):
    """Translate a copilot/ tree to rc.yml v2 + docker-compose.yml.

    \b
    Reads every copilot/<service>/manifest.yml + copilot/environments/*,
    runs the translators, and writes:
      <out>/rc.yml                  rc.yml v2
      <out>/docker-compose.yml      compose file with build/image + env
      <out>/IMPORT_SUMMARY.md       per-service translation report

    AWS Copilot reaches end-of-support on 2026-06-12. This command is the
    fast path off it.
    """
    import yaml as _yaml
    from pathlib import Path as _Path
    from remote_compose.copilot import discover, DiscoveryError
    from remote_compose.copilot.translate import compose_app

    src = _Path(from_dir).resolve()
    target = _Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    rc_path = target / 'rc.yml'
    compose_path = target / 'docker-compose.yml'
    summary_path = target / 'IMPORT_SUMMARY.md'

    for p in (rc_path, compose_path):
        if p.exists() and not force:
            click.echo(
                f"refusing to overwrite {p} — re-run with --force to replace.",
                err=True,
            )
            raise click.exceptions.Exit(1)

    try:
        app = discover(src)
    except DiscoveryError as exc:
        click.echo(f"rc copilot import: {exc}", err=True)
        raise click.exceptions.Exit(1)

    result = compose_app(app, project=project_name, env=env_name)

    rc_path.write_text(_yaml.safe_dump(result.rc_yml, sort_keys=False))
    compose_path.write_text(_yaml.safe_dump(result.docker_compose, sort_keys=False))
    summary_path.write_text(result.summary)

    click.echo(f"\nrc copilot import — {result.rc_yml['project']}")
    click.echo(f"  source:    {src}")
    click.echo(f"  env:       {env_name or '(base manifest values)'}")
    click.echo(f"  services:  {len(result.rc_yml['services'])}")
    if result.warnings:
        click.echo(f"  warnings:  {len(result.warnings)} (see IMPORT_SUMMARY.md)")
    click.echo(f"\n  wrote {rc_path}")
    click.echo(f"  wrote {compose_path}")
    click.echo(f"  wrote {summary_path}")
    click.echo(f"\n  Next: review the summary, then `rc plan` from {target}")


# =============================================================================
# rc compose import — scaffold a starter rc.yml from a docker-compose.yml
# =============================================================================

@cli.group(name='compose')
def compose_group():
    """docker-compose interop helpers."""
    pass


@compose_group.command(name='import')
@click.option('--from', 'compose_file', default='./docker-compose.yml',
              show_default=True,
              type=click.Path(exists=True, dir_okay=False),
              help='Path to the source docker-compose.yml.')
@click.option('--out', 'out_path', default='./rc.yml', show_default=True,
              type=click.Path(dir_okay=False),
              help='Where to write the scaffolded rc.yml.')
@click.option('--project', 'project_name', default=None,
              help='rc.yml v2 project field. Defaults to the parent dir name '
                   'of the compose file.')
@click.option('--force', is_flag=True,
              help='Overwrite an existing rc.yml at --out.')
def compose_import(compose_file, out_path, project_name, force):
    """Scaffold a starter rc.yml v2 from an existing docker-compose.yml.

    \b
    Reads docker-compose.yml and writes an rc.yml shell with project +
    provider + provider_config defaults, plus per-service overrides for
    things we can detect (public ports, db services with volume hints,
    worker-shaped names). env_file refs surface as commented stubs in the
    secrets: block.

    \b
    The auto-import path makes services[] OPTIONAL — compose drives the
    deploy set with cpu=256/memory=512 defaults. Add a service entry only
    to OVERRIDE those defaults.

    \b
    Examples:
      rc compose import
      rc compose import --from docker-compose.prod.yml --project myapp
    """
    from pathlib import Path as _Path
    from remote_compose.compose_import import scaffold_rc_yml

    src = _Path(compose_file).resolve()
    dst = _Path(out_path).resolve()

    if dst.exists() and not force:
        click.echo(
            f"refusing to overwrite {dst} — re-run with --force to replace.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    rc_yml = scaffold_rc_yml(src, project=project_name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rc_yml)

    click.echo(f"\nrc compose import")
    click.echo(f"  source:  {src}")
    click.echo(f"  wrote:   {dst}")
    click.echo(f"\n  Next: edit {dst.name} (provider region, secrets), "
               f"then `rc plan`.")


# =============================================================================
# rc audit — sweep an AWS account for resources matching a project
# =============================================================================

@cli.command(name='audit')
@click.option('--project', 'project_name', default=None,
              help='Project name to scan for. Defaults to rc.yml v2 project.')
@click.option('--region', 'region_name', default=None,
              help='AWS region to scan. Defaults to rc.yml v2 provider_config.ecs.region.')
@click.option('--profile', 'profile_name', default=None,
              help='AWS profile. Defaults to rc.yml v2 provider_config.ecs.aws_profile.')
@click.option('--delete', is_flag=True,
              help='Prompt to delete every leftover. Off by default — dry-run is the safer default.')
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
    from pathlib import Path as _Path

    # Resolve missing args from rc.yml v2 if present.
    if not project_name or not region_name or not profile_name:
        path = _Path(ctx.obj.get('config_path') or 'rc.yml')
        if path.exists():
            try:
                from remote_compose.cli_v2 import load_rc_yml
                version, _, v2 = load_rc_yml(path)
                if version == 2 and v2 is not None:
                    project_name = project_name or v2.project
                    ecs_cfg = (v2.provider_config or {}).get('ecs') or {}
                    region_name = region_name or ecs_cfg.get('region')
                    profile_name = profile_name or ecs_cfg.get('aws_profile')
            except Exception:
                pass

    if not project_name:
        click.echo("rc audit: --project required (or run from a dir with rc.yml v2).",
                   err=True)
        raise click.exceptions.Exit(1)
    if not region_name:
        click.echo("rc audit: --region required (or set provider_config.ecs.region in rc.yml).",
                   err=True)
        raise click.exceptions.Exit(1)

    session = boto3.Session(region_name=region_name, profile_name=profile_name)
    report = audit_project(session, project=project_name, region=region_name)
    click.echo(report.render())

    if not delete or report.is_clean:
        return

    if not click.confirm(f"\nDelete {len(report.findings)} resource(s)?"):
        click.echo("Aborted.")
        return
    click.echo("\n  --delete is dry-run today. Per-resource deletion will land "
               "in the next iteration; for now use the listed identifiers with "
               "the matching `aws <svc> delete-...` commands.", err=True)


# =============================================================================
# rc dev — hot-reload iteration (rc-e5u.45.9)
# =============================================================================

@cli.group(name='dev')
def dev_group():
    """Hot-reload iteration on a deployed dev-mode stack.

    \b
    Workflow:
      1. Add `dev_volumes:` entries to services in rc.yml.
      2. `rc up --dev` — deploys with EFS-backed bind mounts at the
         declared paths. The container will start empty until you push.
      3. `rc dev push <service>` — streams local source into the EFS
         mount via `aws ecs execute-command`. Django runserver et al.
         auto-reload on file change.
      4. `rc dev push --watch <service>` — keeps streaming on every
         local edit (debounced ~250ms).
    """


@dev_group.command(name='push')
@click.argument('service', required=False)
@click.option('--watch', 'watch', is_flag=True,
              help='Watch local sources and re-push on every change '
                   '(debounced ~250ms). Requires fswatch (macOS) or '
                   'inotifywait (Linux).')
@click.pass_context
def dev_push_cmd(ctx, service, watch):
    """Push local dev_volume source(s) to a running task via EFS.

    With no SERVICE arg, pushes EVERY service that declares dev_volumes.
    With --watch, runs forever, re-pushing on every local edit.
    """
    from pathlib import Path as _Path
    from remote_compose.dev_push import (
        DevPushError, push_all, watch_and_push,
    )

    config_path = ctx.obj.get('config_path') or RC_CONFIG_FILE
    rc_path = _Path(config_path)
    if not rc_path.exists():
        click.echo(f"Error: {rc_path} not found.", err=True)
        sys.exit(1)

    def _progress(msg: str) -> None:
        click.echo(msg)

    try:
        if watch:
            watch_and_push(rc_path, service, progress=_progress)
        else:
            results = push_all(rc_path, service, progress=_progress)
            total = sum(r["elapsed_s"] for r in results)
            click.echo(
                f"\n  pushed {len(results)} dev_volume(s) in {total:.1f}s."
            )
    except DevPushError as exc:
        click.echo(f"\n  rc dev push: {exc}", err=True)
        sys.exit(1)


# =============================================================================
# rc fix — one-shot scaffolders for the most common ECS gotchas
# =============================================================================


@cli.group(name='fix')
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
    from pathlib import Path as _Path
    from remote_compose.fix_nginx_conf import (
        Upstream, parse_upstream_arg, upstreams_from_rc_v2, write_ecs_nginx,
    )

    config_path = ctx.obj.get('config_path') or RC_CONFIG_FILE
    rc_path = _Path(config_path)
    if not rc_path.exists():
        click.echo(f"Error: {rc_path} not found.", err=True)
        sys.exit(1)

    raw = yaml.safe_load(rc_path.read_text()) or {}
    project = str(raw.get('project') or '')
    ecs_cfg = ((raw.get('provider_config') or {}).get('ecs') or {})
    vpc_cidr = ecs_cfg.get('vpc_cidr')

    # If --django was passed without an explicit value (just the bare flag,
    # not supported by click multiple), treat each --django=NAME as a name.
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
        # Fall back to rc.yml services with port:.
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


if __name__ == '__main__':
    cli()
