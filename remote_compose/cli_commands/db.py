"""rc db — database backup, restore, push, dump-local, list.

backup/restore/list are v1 (use Django models + ECS exec). push is v2-only
(routes through Provider.exec). dump-local is local-only (docker exec).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml

from ._dispatchers import _db_backup_v2, _db_push_v2
from ._legacy import (
    _bootstrap_django,
    _exec_interactive,
    _format_size,
    _get_backup_engine,
    _load_config,
    _resolve_ecs_exec_target,
    _set_aws_profile,
)


@click.group(name="db")
def db_group():
    """Database backup and restore via S3."""
    pass


@db_group.command(name="backup")
@click.option(
    "--service",
    default=None,
    help="Service to exec into (default: backup.service from rc.yml)",
)
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
    # rc-56bq: v2 first. The v1 body below resolves its exec target through
    # rc's LOCAL Django ORM, which a terraform-managed v2 stack never
    # populates — it died "Service 'django' not found" on a service that was
    # declared in rc.yml and running in AWS. It also needs a PTY and never
    # verifies the upload. Same shape as `rc db push` above.
    if _db_backup_v2(ctx.obj.get("config_path"), service):
        return

    config = _load_config(ctx.obj.get("config_path"))
    _set_aws_profile(config)

    backup_cfg = config.get("backup", {})
    bucket = backup_cfg.get("bucket")
    if not bucket:
        click.echo("Error: 'backup.bucket' not set in rc.yml", err=True)
        click.echo("Add a backup section to rc.yml:\n")
        click.echo("  backup:")
        click.echo("    bucket: my-project-db-dumps")
        click.echo("    service: django")
        sys.exit(1)

    _bootstrap_django(config)

    service_name = service or backup_cfg.get("service", "django")
    project_name = config["project_name"]
    region = config.get("region", "us-west-2")

    cluster, svc, task_arn, container = _resolve_ecs_exec_target(config, service_name)

    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    s3_key = f"{project_name}/{project_name}-{timestamp}.dump"
    s3_uri = f"s3://{bucket}/{s3_key}"

    click.echo(f"\nBackup: {svc.name} → {s3_uri}\n")
    click.echo(
        "  This may take a while for large databases. Keep this terminal open.\n"
    )

    import boto3

    s3_client = boto3.client("s3", region_name=region)
    presigned_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": s3_key},
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
        "aws",
        "ecs",
        "execute-command",
        "--cluster",
        cluster_ref,
        "--task",
        task_arn,
        "--container",
        container,
        "--interactive",
        "--command",
        backup_cmd,
    ]
    if cluster.aws_region:
        aws_cmd.extend(["--region", cluster.aws_region])

    click.echo("  Connecting to container...\n")
    _exec_interactive(aws_cmd)


@db_group.command(name="restore")
@click.option(
    "--file",
    "backup_file",
    default=None,
    help="S3 key or filename to restore (from rc db list)",
)
@click.option("--service", default=None, help="Service to exec into")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
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
    config = _load_config(ctx.obj.get("config_path"))
    _set_aws_profile(config)

    backup_cfg = config.get("backup", {})
    bucket = backup_cfg.get("bucket")
    if not bucket:
        click.echo("Error: 'backup.bucket' not set in rc.yml", err=True)
        sys.exit(1)

    _bootstrap_django(config)

    service_name = service or backup_cfg.get("service", "django")
    region = config.get("region", "us-west-2")

    if backup_file:
        s3_key = backup_file if "/" in backup_file else backup_file
    else:
        import boto3

        s3 = boto3.client("s3", region_name=region)

        try:
            paginator = s3.get_paginator("list_objects_v2")
            objects = []
            for page in paginator.paginate(Bucket=bucket):
                objects.extend(page.get("Contents", []))
        except Exception as e:
            click.echo(f"Error listing backups: {e}", err=True)
            sys.exit(1)

        dump_files = [
            o
            for o in objects
            if o["Key"].endswith(".dump") or o["Key"].endswith(".tar.gz")
        ]
        dump_files = [o for o in dump_files if not o["Key"].endswith(".sh")]

        if not dump_files:
            click.echo(f"No backups found in s3://{bucket}/", err=True)
            sys.exit(1)

        dump_files.sort(key=lambda x: x["LastModified"], reverse=True)
        s3_key = dump_files[0]["Key"]

    s3_uri = f"s3://{bucket}/{s3_key}"
    filename = s3_key.split("/")[-1]
    local_file = f"/tmp/{filename}"
    is_targz = filename.endswith(".tar.gz") or filename.endswith(".tgz")

    click.echo(f"\n  Backup:  {s3_uri}")
    click.echo(f"  Target:  {service_name}")
    click.echo(f"  Format:  {'directory (tar.gz)' if is_targz else 'custom (.dump)'}")

    if not yes and not click.confirm(
        "\n  This will overwrite existing data. Continue?"
    ):
        click.echo("Aborted.")
        return

    cluster, svc, task_arn, container = _resolve_ecs_exec_target(config, service_name)

    click.echo(f"\nRestore: {s3_uri} → {svc.name}")
    click.echo("  This may take a while for large dumps. Keep this terminal open.\n")

    import boto3

    s3_client = boto3.client("s3", region_name=region)
    presigned_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=7200,
    )

    engine = _get_backup_engine(config)
    download_step, cleanup = engine.get_restore_script(filename, local_file, is_targz)

    restore_script = (
        "#!/bin/sh\n"
        f'export PRESIGNED_URL="$1"\n'
        'echo "=== Database Restore ==="\n'
        f"{download_step}"
        "# Background keepalive to prevent SSM idle timeout\n"
        '(while true; do echo "  [keepalive $(date +%H:%M:%S)]"; sleep 30; done) &\n'
        "KEEPALIVE=$!\n"
        "# Run restore in foreground with verbose output\n"
        "eval $RESTORE_CMD 2>&1\n"
        "RC=$?\n"
        "kill $KEEPALIVE 2>/dev/null\n"
        "wait $KEEPALIVE 2>/dev/null\n"
        'echo ""\n'
        "if [ $RC -eq 0 ] || [ $RC -eq 1 ]; then\n"
        '  echo "=== Restore Complete (warnings are normal) ==="\n'
        "else\n"
        '  echo "=== Restore FAILED (exit code $RC) ==="\n'
        "fi\n"
        f"{cleanup}\n"
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
        "aws",
        "ecs",
        "execute-command",
        "--cluster",
        cluster_ref,
        "--task",
        task_arn,
        "--container",
        container,
        "--interactive",
        "--command",
        restore_cmd,
    ]
    if cluster.aws_region:
        aws_cmd.extend(["--region", cluster.aws_region])

    click.echo("  Connecting to container...\n")
    _exec_interactive(aws_cmd)


@db_group.command(name="push")
@click.argument("local_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--service",
    default=None,
    help="Service to exec into (default: backup.service from rc.yml)",
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
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
    if _db_push_v2(ctx.obj.get("config_path"), local_file, service, yes):
        return
    click.echo(
        "rc db push currently requires rc.yml v2 (uses Provider.exec for "
        "execute-command). v1 not supported.",
        err=True,
    )
    raise click.exceptions.Exit(1)


@db_group.command(name="dump-local")
@click.option(
    "--container",
    "container_name",
    required=True,
    help="Local Docker container hosting postgres (e.g. sentinal_postgres).",
)
@click.option(
    "--to",
    "output_path_str",
    default=None,
    help="Local path to write the dump. Defaults to /tmp/rc-dumps/<project>-<timestamp>.dump.",
)
@click.option(
    "--user",
    "pg_user",
    default=None,
    help="Override POSTGRES_USER (auto-discovered from container env by default).",
)
@click.option(
    "--database",
    "pg_db",
    default=None,
    help="Override POSTGRES_DB (auto-discovered from container env by default).",
)
@click.option(
    "--port",
    "pg_port",
    type=int,
    default=None,
    help="Override POSTGRES_PORT (auto-discovered from container env by default).",
)
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
        DumpLocalError,
        default_dump_path,
        dump_local,
    )

    if output_path_str:
        output_path = Path(output_path_str)
    else:
        from ._dispatchers import _load_v2_if_present

        loaded = _load_v2_if_present(ctx.obj.get("config_path"), strict=False)
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


@db_group.command(name="list")
@click.pass_context
def db_list(ctx):
    """List available database backups in S3."""
    config = _load_config(ctx.obj.get("config_path"))
    _set_aws_profile(config)

    backup_cfg = config.get("backup", {})
    bucket = backup_cfg.get("bucket")
    if not bucket:
        click.echo("Error: 'backup.bucket' not set in rc.yml", err=True)
        sys.exit(1)

    region = config.get("region", "us-west-2")

    import boto3

    s3 = boto3.client("s3", region_name=region)

    try:
        paginator = s3.get_paginator("list_objects_v2")
        objects = []
        for page in paginator.paginate(Bucket=bucket):
            objects.extend(page.get("Contents", []))
    except Exception as e:
        click.echo(f"Error listing backups: {e}", err=True)
        sys.exit(1)

    objects = [
        o for o in objects if o["Key"].endswith(".dump") or o["Key"].endswith(".tar.gz")
    ]

    if not objects:
        click.echo(f"No backups found in s3://{bucket}/")
        return

    objects.sort(key=lambda x: x["LastModified"], reverse=True)

    click.echo(f"\nBackups in s3://{bucket}/\n")

    header = f"  {'FILE':<50} {'SIZE':>10} {'DATE':<20}"
    click.echo(header)
    click.echo(f"  {'-' * 80}")

    for obj in objects:
        key = obj["Key"]
        size = _format_size(obj["Size"])
        date = obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S")
        click.echo(f"  {key:<50} {size:>10} {date:<20}")

    click.echo(f"\n  Total: {len(objects)} backups")

    retention = backup_cfg.get("retention")
    if retention and len(objects) > retention:
        excess = len(objects) - retention
        click.echo(f"  Retention: {retention} (oldest {excess} could be pruned)")


@db_group.command(name="psql")
@click.option(
    "--service",
    default=None,
    help="Postgres service to exec into (default: backup.service from rc.yml)",
)
@click.option(
    "-c",
    "--command",
    "sql_command",
    default=None,
    help="Run a single SQL command and exit (psql -c). When omitted, "
    "opens an interactive psql shell.",
)
@click.option(
    "-d",
    "--database",
    "db_name",
    default=None,
    help="Database to connect to (default: $POSTGRES_DB from the "
    "service container env).",
)
@click.pass_context
def db_psql(ctx, service, sql_command, db_name):
    """Open a psql shell against the deployed postgres (rc-878).

    \b
    Wraps `aws ecs execute-command` + psql with sane defaults:
      - PAGER=cat + psql -P pager=off so output isn't mangled by less
      - auto-discovers the postgres task in the cluster
      - reads POSTGRES_USER / POSTGRES_DB / POSTGRES_PORT from the
        container's task-def env so users don't have to remember
        sentinal-style port quirks (5434 vs 5432)

    \b
    Examples:
      rc db psql                              # interactive shell
      rc db psql -c "SELECT count(*) FROM users_user"
      rc db psql -d backend -c "\\dt"
    """
    import os as _os
    import subprocess as _subprocess

    config_path = ctx.obj.get("config_path") or "rc.yml"
    rc_path = Path(config_path)
    if not rc_path.exists():
        raise click.ClickException(f"{rc_path} not found.")
    rc_raw = yaml.safe_load(rc_path.read_text()) or {}
    if rc_raw.get("version") != 2:
        raise click.ClickException("rc db psql currently supports v2 rc.yml only.")
    ecs_cfg = (rc_raw.get("provider_config") or {}).get("ecs") or {}
    cluster = ecs_cfg.get("cluster")
    region = ecs_cfg.get("region")
    aws_profile = ecs_cfg.get("aws_profile")
    if not cluster or not region:
        raise click.ClickException(
            "rc db psql: rc.yml must declare provider_config.ecs.cluster + "
            "provider_config.ecs.region."
        )

    service = service or (rc_raw.get("backup") or {}).get("service") or "postgres"

    if aws_profile:
        _os.environ.setdefault("AWS_PROFILE", aws_profile)
    import boto3

    sess = boto3.Session(profile_name=aws_profile, region_name=region)
    ecs = sess.client("ecs")

    # rc-ib01.2: under task_groups this service is a CONTAINER inside its
    # group's task, and the ECS service carries the group's name. `--container`
    # below still names the member, which is what the container is called.
    from ..config.v2_schema import container_named, group_for_service

    ecs_service = group_for_service(rc_raw, service)

    # Find a running task for the service.
    task_arns = (
        ecs.list_tasks(
            cluster=cluster,
            serviceName=ecs_service,
            desiredStatus="RUNNING",
        ).get("taskArns")
        or []
    )
    if not task_arns:
        grouped = (
            f" (container {service!r} runs in task group {ecs_service!r})"
            if ecs_service != service
            else ""
        )
        raise click.ClickException(
            f"rc db psql: no running task for service {ecs_service!r} in "
            f"cluster {cluster!r}{grouped}. Is the stack up?"
        )
    task_arn = task_arns[0]
    task_id = task_arn.rsplit("/", 1)[-1]

    # Read POSTGRES_PORT/USER/DB from the task def env so we don't have to
    # ask the user for them. Falls back to defaults if not set.
    # Read directly from the running task's task-def revision.
    task_desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    task_def_arn = task_desc["tasks"][0]["taskDefinitionArn"]
    td = ecs.describe_task_definition(taskDefinition=task_def_arn)
    # By NAME, not [0]: in a grouped task the first container is whichever
    # member rc emitted first (nginx, say), and reading POSTGRES_* off it would
    # silently fall back to 5432/postgres/postgres against the wrong container.
    container = container_named(
        td["taskDefinition"].get("containerDefinitions"), service
    )
    env_dict = {e["name"]: e["value"] for e in container.get("environment") or []}
    pg_port = env_dict.get("POSTGRES_PORT", "5432")
    pg_user = env_dict.get("POSTGRES_USER", "postgres")
    pg_db = db_name or env_dict.get("POSTGRES_DB", "postgres")

    # rc-878: PAGER=cat + psql -P pager=off so output captured via
    # ecs-execute-command is clean (no less paging artifacts).
    psql_inner = (
        f"PAGER=cat psql -h localhost -p {pg_port} "
        f"-U '{pg_user}' -d '{pg_db}' -P pager=off"
    )
    if sql_command:
        # Single command + exit. shlex.quote to survive any user input.
        import shlex as _shlex

        psql_inner += f" -c {_shlex.quote(sql_command)}"

    aws_cmd = [
        "aws",
        "ecs",
        "execute-command",
        "--cluster",
        cluster,
        "--task",
        task_id,
        "--container",
        service,
        "--interactive",
        "--command",
        psql_inner,
        "--region",
        region,
    ]
    if aws_profile:
        aws_cmd += ["--profile", aws_profile]

    if sql_command:
        # Non-interactive: capture + print stdout cleanly. Non-zero exit
        # propagates so CI catches errors.
        result = _subprocess.run(aws_cmd, capture_output=True, text=True)
        if result.stdout:
            click.echo(result.stdout, nl=False)
        if result.stderr and result.returncode != 0:
            click.echo(result.stderr, nl=False, err=True)
        if result.returncode != 0:
            raise click.exceptions.Exit(result.returncode)
        return
    # Interactive: replace the current process so the user gets a real TTY.
    _os.execvp(aws_cmd[0], aws_cmd)
