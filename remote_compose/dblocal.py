"""rc db dump-local — wrap `docker exec pg_dump` for the local→remote seed flow.

Pairs with `rc db push` (remote_compose.cli._db_push_v2). Discovers the
postgres user/db/port from the container's own env so users don't have
to remember per-project port quirks (sentinal listens on 5434, not the
default 5432).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class DumpLocalError(RuntimeError):
    """Raised when the local pg_dump pipeline can't complete."""


@dataclass
class DumpResult:
    path: Path
    size_bytes: int
    user: str
    database: str
    port: int


def inspect_container_env(container: str) -> dict[str, str]:
    """Read the env vars of a running Docker container.

    Implementation: `docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' <c>`
    returns the env in KEY=value form, one per line. Cheaper than
    docker exec env and works on stopped containers too.
    """
    docker = shutil.which("docker") or "docker"
    cmd = [
        docker, "inspect", "-f",
        "{{range .Config.Env}}{{println .}}{{end}}",
        container,
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise DumpLocalError(
            f"docker inspect failed for container {container!r}: "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    out: dict[str, str] = {}
    for raw in proc.stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value
    return out


def dump_local(
    container: str,
    output_path: Path,
    *,
    user: Optional[str] = None,
    database: Optional[str] = None,
    port: Optional[int] = None,
    timeout: int = 1800,
) -> DumpResult:
    """pg_dump the database in `container` to `output_path` (custom format).

    user/database/port default to whatever the container env declares
    (POSTGRES_USER / POSTGRES_DB / POSTGRES_PORT). Explicit kwargs win.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    env = inspect_container_env(container)
    pg_user = user or env.get("POSTGRES_USER")
    if not pg_user:
        raise DumpLocalError(
            f"container {container!r} has no POSTGRES_USER env var; pass --user "
            f"explicitly or use a container that declares one."
        )
    pg_db = database or env.get("POSTGRES_DB")
    if not pg_db:
        raise DumpLocalError(
            f"container {container!r} has no POSTGRES_DB env var; pass --database "
            f"explicitly or use a container that declares one."
        )
    pg_port = port if port is not None else int(env.get("POSTGRES_PORT") or 5432)

    docker = shutil.which("docker") or "docker"
    cmd = [
        docker, "exec", container,
        "pg_dump", "-Fc",
        "-h", "127.0.0.1",
        "-p", str(pg_port),
        "-U", pg_user,
        pg_db,
    ]

    with output_path.open("wb") as out_fh:
        proc = subprocess.run(
            cmd, stdout=out_fh, stderr=subprocess.PIPE, timeout=timeout,
        )
    if proc.returncode != 0:
        raise DumpLocalError(
            f"pg_dump failed for container {container!r}: "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    size = output_path.stat().st_size
    return DumpResult(
        path=output_path, size_bytes=size,
        user=pg_user, database=pg_db, port=pg_port,
    )


def default_dump_path(project: str) -> Path:
    """/tmp/rc-dumps/<project>-<ISO-timestamp>.dump"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", project) or "dump"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("/tmp/rc-dumps") / f"{safe}-{stamp}.dump"
