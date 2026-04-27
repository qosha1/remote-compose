"""rc db — database backup, restore, push, dump-local, list.

backup/restore/list are v1 (use Django models + ECS exec). push is v2-only
(routes through Provider.exec). dump-local is local-only (docker exec).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._dispatchers import _db_push_v2
from ._legacy import (
    _bootstrap_django,
    _exec_interactive,
    _format_size,
    _get_backup_engine,
    _load_config,
    _resolve_ecs_exec_target,
    _set_aws_profile,
)


@click.group(name='db')
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

    import boto3
    s3_client = boto3.client('s3', region_name=region)
    presigned_url = s3_client.generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket, 'Key': s3_key},
        ExpiresIn=7200,
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

    import boto3
    s3_client = boto3.client('s3', region_name=region)
    presigned_url = s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': s3_key},
        ExpiresIn=7200,
    )

    engine = _get_backup_engine(config)
    download_step, cleanup = engine.get_restore_script(filename, local_file, is_targz)

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
    from remote_compose.dblocal import (
        DumpLocalError, default_dump_path, dump_local,
    )

    if output_path_str:
        output_path = Path(output_path_str)
    else:
        from ._dispatchers import _load_v2_if_present
        loaded = _load_v2_if_present(ctx.obj.get('config_path'), strict=False)
        project = loaded[2].project if loaded is not None else "rc-db-dump"
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
        for page in paginator.paginate(Bucket=bucket):
            objects.extend(page.get('Contents', []))
    except Exception as e:
        click.echo(f"Error listing backups: {e}", err=True)
        sys.exit(1)

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

    retention = backup_cfg.get('retention')
    if retention and len(objects) > retention:
        excess = len(objects) - retention
        click.echo(f"  Retention: {retention} (oldest {excess} could be pruned)")
