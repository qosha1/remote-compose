"""rc.yml v2 dispatchers + their internal helpers.

These wrap the v2 Provider pathway for commands that have both a v1 and
a v2 implementation (secrets push, db push, exec). They return True when
the v2 path handled the call; the caller falls back to the v1 pipeline
on False.

Living here (not cli.py) so commands extracted to cli_commands/* modules
can share the wrappers without circular imports.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import click


_RC_CONFIG_FILE = 'rc.yml'


def _load_v2_if_present(
    config_path: Optional[str], strict: bool = True,
) -> Optional[tuple[Path, dict, Any]]:
    """Resolve config_path → (path, raw_dict, RcConfigV2) when rc.yml is v2.

    Returns None when:
      - the file doesn't exist, or
      - the file is v1/legacy, or
      - parsing failed AND strict=False.

    With strict=True (default), parse failures echo the error and Exit(1).
    With strict=False, parse failures are swallowed (caller falls through).

    Use strict=True for commands that have a v1 fallback whose error
    UX is "tell the user the v2 parse went wrong"; strict=False for
    commands like rc exec where v1 may also exist and we want the user
    to silently fall through if rc.yml looks unparseable.
    """
    path = Path(config_path) if config_path else Path.cwd() / _RC_CONFIG_FILE
    if not path.exists():
        return None
    from remote_compose.cli_v2 import load_rc_yml
    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        if strict:
            click.echo(f"rc.yml parse failed: {exc}", err=True)
            raise click.exceptions.Exit(1)
        return None
    if version != 2 or v2 is None:
        return None
    return path, raw, v2


def _detect_empty_file_secrets(
    v2, region: str, aws_profile: Optional[str], file_secrets: list,
) -> list[str]:
    """Return SM secret names that exist but have empty / zero-key blobs.

    Used by `rc deploy` (rc-e5u.44.20) to catch the silent-fail-cascade
    where terraform created a secret resource (placeholder blob) but
    `rc secrets push` never populated it — every task on the new task
    def then fails with 'retrieved secret from Secrets Manager did not
    contain json key X'. Returns names that need a push; empty list when
    everything is populated. Missing secrets (NotFoundException) are NOT
    treated as empty — terraform hasn't applied yet and the deploy will
    create them.
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
                continue
            raise
        body = resp.get("SecretString") or ""
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
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
    loaded = _load_v2_if_present(config_path, strict=True)
    if loaded is None:
        return False
    path, raw, v2 = loaded

    from remote_compose.envfile import EnvFileError, parse as parse_env
    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    region = ecs_cfg.get("region")
    aws_profile = ecs_cfg.get("aws_profile")
    if not region:
        click.echo("rc.yml v2: provider_config.ecs.region is required.", err=True)
        raise click.exceptions.Exit(1)

    from remote_compose.cli_v2 import _expand_env_file_auto, _parse_compose_services
    compose_path = Path(v2.compose_file)
    if not compose_path.is_absolute():
        compose_path = (path.parent / compose_path).resolve()
    compose_services = _parse_compose_services(compose_path) if compose_path.exists() else {}
    expanded_secrets, _, _ = _expand_env_file_auto(
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
        env_path = Path(sec.path)
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

    # rc-e5u.38: detect orphan keys — keys present in the SM blob (just
    # uploaded) but NOT referenced by any task def's secrets[] entry.
    # The container would never see those keys until the next rc deploy
    # re-emits the task def. Warn loudly so the user knows to follow up.
    cluster = ecs_cfg.get("cluster") or f"{v2.project}-cluster"
    ecs = session.client("ecs")
    orphan_keys = _detect_orphan_secret_keys_v2(
        ecs, cluster, v2, file_secrets, project_dir,
    )
    if orphan_keys:
        click.echo(
            "\n  ! Orphan keys detected — these are in SM but no task "
            "def's secrets[] references them, so containers won't see "
            "them until the task defs are re-emitted:",
            err=True,
        )
        for sm_name, missing in sorted(orphan_keys.items()):
            click.echo(f"    {sm_name}: {sorted(missing)}", err=True)
        click.echo(
            "    → run `rc deploy --no-build` to re-emit the task defs "
            "with these keys included.",
            err=True,
        )

    if rollout and file_secrets:
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


def _detect_orphan_secret_keys_v2(
    ecs_client,
    cluster: str,
    v2,
    file_secrets,
    project_dir: Path,
) -> dict[str, set[str]]:
    """rc-e5u.38: return {sm_name: set_of_orphan_keys} for keys present in
    the JUST-pushed SM blobs but not referenced by any current task def.

    Returns {} silently on AWS errors (network, perms) — the orphan
    diagnostic is best-effort, not a blocker. Always returns an empty
    dict when v2.services is empty.
    """
    if not file_secrets or not v2.services:
        return {}
    from remote_compose.envfile import parse as parse_env, EnvFileError

    referenced_by_task_defs: dict[str, set[str]] = {}
    any_query_succeeded = False
    for svc_name in sorted(v2.services.keys()):
        try:
            desc = ecs_client.describe_services(
                cluster=cluster, services=[svc_name],
            )
            any_query_succeeded = True
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(desc, dict):
            continue
        services_resp = desc.get("services") or []
        if not isinstance(services_resp, list) or not services_resp:
            continue
        first_svc = services_resp[0]
        if not isinstance(first_svc, dict):
            continue
        td_arn = first_svc.get("taskDefinition")
        if not td_arn or not isinstance(td_arn, str):
            continue
        try:
            td = ecs_client.describe_task_definition(taskDefinition=td_arn)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(td, dict):
            continue
        td_inner = td.get("taskDefinition")
        if not isinstance(td_inner, dict):
            continue
        for cd in td_inner.get("containerDefinitions") or []:
            if not isinstance(cd, dict):
                continue
            for sec_entry in cd.get("secrets") or []:
                if not isinstance(sec_entry, dict):
                    continue
                value_from = sec_entry.get("valueFrom") or ""
                # Format: arn:...:<name>:<KEY>::
                # Extract the SM name AND the key after the colon.
                if ":" not in value_from:
                    continue
                # arn:aws:secretsmanager:region:acct:secret:<name>-suffix:<KEY>::
                parts = value_from.rsplit(":", 3)
                if len(parts) >= 3:
                    secret_arn = parts[0]
                    key = parts[1]
                    sm_simple_name = secret_arn.split(":")[-1]
                    referenced_by_task_defs.setdefault(
                        sm_simple_name, set()
                    ).add(key)

    # If every describe_services failed, we can't tell what's orphaned.
    # Skip the diagnostic rather than flagging every key as orphan (which
    # would be a false alarm).
    if not any_query_succeeded:
        return {}

    orphans: dict[str, set[str]] = {}
    for sec in file_secrets:
        env_path = Path(sec.path)
        if not env_path.is_absolute():
            env_path = (project_dir / env_path).resolve()
        try:
            body = parse_env(env_path)
        except (EnvFileError, OSError):
            continue
        sm_simple = f"{v2.project}/{sec.name}"
        # Match by simple-name suffix (the ARN includes a -<random> suffix).
        referenced = set()
        for full_name, keys in referenced_by_task_defs.items():
            # full_name looks like "<project>/<secret>-RandomSuffix" or just
            # "<project>/<secret>". Match by prefix.
            if full_name == sm_simple or full_name.startswith(sm_simple + "-"):
                referenced |= keys
        missing = set(body.keys()) - referenced
        if missing:
            orphans[sm_simple] = missing
    return orphans


def _db_push_v2(
    config_path: Optional[str], local_file: str, service: Optional[str], yes: bool,
) -> bool:
    """rc db push for v2 stacks: upload local dump → S3 → exec restore.

    Returns True when handled (rc.yml v2 detected), else False so the
    caller can decide what to do (currently: exit with error).
    """
    from datetime import datetime, timezone
    loaded = _load_v2_if_present(config_path, strict=True)
    if loaded is None:
        return False
    path, raw, v2 = loaded

    from remote_compose.cli_v2 import (
        build_deploy_context, resolve_provider,
    )

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

    local = Path(local_file)
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
    # rc-xmz: pg_restore with --no-owner --clean --if-exists exits 0 even
    # when individual table restores fail (FK ordering, missing role, etc.).
    # Errors only appear in stderr as 'pg_restore: error:' lines. Without
    # surfacing these, rc db push silently completes while the target DB
    # is missing tables. Sentinal repro lost workflows_pagecapture this way.
    pg_errors = _count_pg_restore_errors(result.stdout or "", result.stderr or "")
    pg_ignored = _pg_restore_ignored_count(
        result.stdout or "", result.stderr or "",
    )
    # rc-ln1: pg_restore exits 1 when individual operations fail BUT the
    # restore continued past them (e.g. extension already exists, role
    # missing for OWNER TO, FK ordering on DROP). pg_restore prints
    # 'errors ignored on restore: N' as the summary line in those cases
    # — the data IS restored, just with the listed objects skipped.
    # Treat that case as success-with-warnings instead of a hard failure.
    benign_warning_only = (
        result.exit_code == 1
        and pg_ignored is not None
        and pg_ignored == pg_errors
        and pg_ignored > 0
    )
    if pg_errors > 0:
        if benign_warning_only:
            click.echo(
                f"\n  rc db push: pg_restore reported {pg_errors} ignored "
                f"error(s) (extensions / roles / drop-order) but the restore "
                f"continued. Data IS in the target DB — review the "
                f"'pg_restore: error:' lines above to confirm none of them "
                f"matter. Treating as success.",
                err=True,
            )
        else:
            click.echo(
                f"\n  rc db push: WARNING — pg_restore reported {pg_errors} "
                f"error(s) during restore. The target DB is likely missing "
                f"tables/data. Look for 'pg_restore: error:' lines above; "
                f"common cause is FK ordering (use --jobs=1 or restore "
                f"schema-then-data separately).",
                err=True,
            )
    if result.exit_code != 0 and not benign_warning_only:
        click.echo(
            f"\n  rc db push: restore exited {result.exit_code}",
            err=True,
        )
        raise click.exceptions.Exit(result.exit_code)
    if pg_errors > 0 and not benign_warning_only:
        # Non-zero exit so callers / CI catch it.
        raise click.exceptions.Exit(2)
    click.echo("\n  rc db push: complete.")
    return True


def _pg_restore_ignored_count(stdout: str, stderr: str) -> Optional[int]:
    """rc-ln1: parse 'pg_restore: warning: errors ignored on restore: N'
    (or 'errors ignored on restore: N') from output. Returns N when
    found, else None. Without --exit-on-error, pg_restore continues
    past per-object failures and emits this summary; the restore IS
    complete, so this is the signal that exit-1 means warnings, not
    a fatal error."""
    import re
    pattern = re.compile(r"errors ignored on restore:\s*(\d+)", re.IGNORECASE)
    for stream in (stdout, stderr):
        for line in (stream or "").splitlines():
            m = pattern.search(line)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    return None
    return None


def _count_pg_restore_errors(stdout: str, stderr: str) -> int:
    """Count 'pg_restore: error:' lines across stdout+stderr.

    rc-xmz: pg_restore exits 0 on partial-success runs (FK ordering, missing
    object, role mismatch). Counting error lines is the only reliable signal
    that some objects didn't restore. Excludes 'warning' lines which are
    benign (already-handled by rc-ln1).
    """
    count = 0
    for stream in (stdout, stderr):
        for line in (stream or "").splitlines():
            if "pg_restore: error" in line.lower():
                count += 1
    return count


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
    # strict=False: silent fall-through if rc.yml is unparseable, matching
    # the legacy behavior where rc exec also has a v1 path.
    loaded = _load_v2_if_present(config_path, strict=False)
    if loaded is None:
        return False
    path, raw, v2 = loaded

    from remote_compose.cli_v2 import (
        build_deploy_context, resolve_provider,
    )

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


def _flatten_v2_to_legacy(v2: dict) -> dict:
    """Flatten a v2 rc.yml into the v1 flat dict shape that legacy helpers
    (backup/restore/list, exec, logs, status) expect.

    Only the ECS provider is supported here — backup tooling is ECS-specific
    and would be per-provider if/when other providers ship their own.
    """
    ecs = (v2.get("provider_config") or {}).get("ecs") or {}
    legacy: dict = dict(v2)
    legacy["project_name"] = v2.get("project", v2.get("project_name", ""))
    legacy["compose_file"] = v2.get("compose_file", "docker-compose.yml")
    if "cluster" in ecs:
        legacy["cluster"] = ecs["cluster"]
    if "region" in ecs:
        legacy["region"] = ecs["region"]
    if "aws_profile" in ecs:
        legacy["aws_profile"] = ecs["aws_profile"]
    return legacy
