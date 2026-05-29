"""rc list — inventory of ephemeral stacks (rc-e5u.44.16)."""

from __future__ import annotations

from typing import Any, Optional

import click


def _format_relative_time(iso_ts: str, now: Optional[Any] = None) -> str:
    """Render an ISO timestamp as a short relative offset (e.g. '2h 14m').

    Past timestamps render '<delta> ago'; future timestamps render
    'in <delta>'. Granularity: days/hours/minutes only — seconds aren't
    useful at the deploy lifecycle scale.
    """
    from datetime import datetime, timezone
    from remote_compose.ephemeral import from_iso_utc

    try:
        target = from_iso_utc(iso_ts)
    except Exception:
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
        if minutes and not days:
            parts.append(f"{minutes}m")
        body = " ".join(parts) or f"{secs}s"
    return f"{prefix}{body}{(' ' + suffix) if suffix else ''}".strip()


@click.command(name="list")
@click.option(
    "--ephemeral",
    "ephemeral_only",
    is_flag=True,
    help="List ephemeral stacks from the local registry (created via "
    "rc deploy --ttl / rc up --ttl).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-parseable JSON instead of a table.",
)
def list_cmd(ephemeral_only, as_json):
    """List rc-managed stacks (today: ephemeral only — see --ephemeral).

    Reads ~/.config/remote-compose/ephemeral.json and prints one row per
    stack: project | region | profile | created | ttl-remaining | rc.yml.
    Pairs with `rc reap` (destroys past-due) and
    `rc destroy --all-ephemeral` (destroys every entry on confirmation).
    """
    if not ephemeral_only:
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
            except Exception:
                ttl_seconds = None
            out.append(
                {
                    "project": r.project,
                    "region": r.region,
                    "aws_profile": r.aws_profile,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "ttl_remaining_seconds": ttl_seconds,
                    "expired": r.is_expired(now),
                    "rc_yml_path": r.rc_yml_path,
                    "terraform_dir": r.terraform_dir,
                }
            )
        click.echo(_json.dumps(out, indent=2))
        return

    if not records:
        click.echo(f"  No ephemeral stacks in registry ({DEFAULT_REGISTRY_PATH}).")
        return

    rows: list[tuple[str, str, str, str, str, str]] = []
    for r in records:
        ttl = _format_relative_time(r.expires_at)
        if r.is_expired():
            ttl = f"EXPIRED ({ttl})"
        rows.append(
            (
                r.project,
                r.region,
                r.aws_profile or "-",
                _format_relative_time(r.created_at),
                ttl,
                r.rc_yml_path,
            )
        )

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
