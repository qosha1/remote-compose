"""rc fix bake-bind-mount-source — append COPY directives to a Dockerfile
so a compose service's bind-mounted source dirs are baked into the image.

Local docker-compose pattern:
    services:
      django:
        build: { context: ., dockerfile: compose/local/django/Dockerfile }
        volumes:
          - ./backend:/app

The bind mount overrides /app at runtime, so the local Dockerfile typically
does NOT COPY ./backend itself. ECS has no bind mounts → /app is empty →
manage.py missing → start script crashes.

This module parses the service's compose volumes, picks the HOST:CONTAINER
pairs that look like source-dir mappings (relative host paths, NOT system
mounts like /tmp/.X11-unix), and appends `COPY <host> <container>` to the
matching Dockerfile. Local dev is unaffected because the bind mount
overrides the COPY at runtime.

rc-bys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class BakeResult:
    dockerfile_path: Optional[Path] = None
    copies_added: list[tuple[str, str]] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    # rc-4mf: COPYs we declined to append because the host source dir is
    # excluded by the build context's .dockerignore. Appending these
    # would produce a structurally-broken Dockerfile (docker build fails
    # with '<host>: not found'). The CLI surfaces this list so the user
    # can either remove the ignore entry or accept the skip.
    skipped_dockerignored: list[tuple[str, str]] = field(default_factory=list)


# Container paths that are obviously NOT user source dirs — system mounts,
# data volumes, etc. We never bake these.
_SKIP_CONTAINER_PATHS: frozenset[str] = frozenset({
    "/tmp/.X11-unix",
    "/var/run/docker.sock",
    "/dev/shm",
    "/var/lib/postgresql/data",
    "/data",
    "/var/lib/mysql",
    "/var/lib/redis",
    "/etc/localtime",
})


def _split_volume_entry(entry) -> Optional[tuple[str, str]]:
    """Parse a compose volumes[] entry into (host, container) or None.

    Accepts the short string form 'host:container[:opts]' and the long
    dict form {type: bind, source: X, target: Y}. Returns None for named
    volumes (no host path), tmpfs mounts, and unparseable entries.
    """
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) < 2:
            return None
        host, container = parts[0], parts[1]
        return host, container
    if isinstance(entry, dict):
        if entry.get("type") not in (None, "bind"):
            return None
        source = entry.get("source")
        target = entry.get("target")
        if source is None or target is None:
            return None
        return str(source), str(target)
    return None


def _is_source_bind_mount(host: str, container: str) -> bool:
    """Heuristic: looks like a SOURCE bind mount we should bake.

    Includes:
      - relative host paths starting with ./ or just a dir name
      - host paths that don't begin with / (relative)
      - container paths that aren't system/data dirs

    Excludes:
      - named volumes (no slash in host)
      - absolute system host paths (/tmp/.X11-unix, /var/run/...)
      - container paths in _SKIP_CONTAINER_PATHS
    """
    if container in _SKIP_CONTAINER_PATHS:
        return False
    if container.startswith("/var/lib/") or container.startswith("/var/run/"):
        return False
    # Named volumes: no path separator in host part.
    if "/" not in host and not host.startswith("."):
        return False
    # Absolute system paths on the host are usually system mounts.
    if host.startswith("/tmp/") or host.startswith("/var/run/"):
        return False
    if host.startswith("/dev/"):
        return False
    return True


def _resolve_build_context(compose_path: Path, svc_compose: dict) -> Optional[Path]:
    """Resolve the absolute path of the service's build context. Mirrors
    compose semantics: build: <str> means context = that dir; build: dict
    uses build.context (default '.'). Returns None for image-only services.
    """
    build = svc_compose.get("build")
    if build is None:
        return None
    compose_dir = compose_path.parent
    if isinstance(build, str):
        return (compose_dir / build).resolve()
    if not isinstance(build, dict):
        return None
    ctx = build.get("context", ".")
    return (compose_dir / ctx).resolve()


def _load_dockerignore(ctx_path: Path) -> set[str]:
    """Parse .dockerignore at ``ctx_path``/`.dockerignore`. Returns the set
    of literal patterns we treat as exclusion roots. Mirrors the parser
    in compose_warnings._load_dockerignore — exact paths and simple
    top-level dir names; glob patterns ignored.
    """
    skip: set[str] = set()
    df = ctx_path / ".dockerignore"
    if not df.is_file():
        return skip
    try:
        text = df.read_text(errors="replace")
    except OSError:
        return skip
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        clean = line.rstrip("/").rstrip("*").rstrip("/")
        if not clean or "*" in clean:
            continue
        skip.add(clean)
    return skip


def _is_dockerignored(host: str, ignored: set[str]) -> bool:
    """True when the host source path (compose-style: './test-fixtures' or
    'backend/media') is excluded by .dockerignore.

    Match strategy: normalize ./prefix and trailing slash, then check
    whether any path prefix is an ignored entry. ./test-fixtures matches
    'test-fixtures'. ./backend/media matches 'backend/media' (or 'backend').
    A bind mount like ./backend with only 'backend/media' ignored is NOT
    a match — docker copies the parent and excludes the child internally.
    """
    if not ignored:
        return False
    norm = host.lstrip("./").rstrip("/")
    if not norm:
        return False
    parts = norm.split("/")
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        if prefix in ignored:
            return True
    return False


def _resolve_dockerfile(compose_path: Path, service_name: str, svc_compose: dict) -> Optional[Path]:
    """Resolve the Dockerfile path for a service from its compose build stanza."""
    build = svc_compose.get("build")
    if build is None:
        return None
    compose_dir = compose_path.parent
    if isinstance(build, str):
        # `build: ./path/to/dir` → Dockerfile in that dir.
        ctx = (compose_dir / build).resolve()
        return ctx / "Dockerfile"
    if not isinstance(build, dict):
        return None
    ctx = build.get("context", ".")
    ctx_path = (compose_dir / ctx).resolve()
    df = build.get("dockerfile") or "Dockerfile"
    df_path = Path(df)
    if df_path.is_absolute():
        return df_path
    return (ctx_path / df_path).resolve()


def bake_bind_mount_source(
    compose_path: Path,
    service_name: str,
    force: bool = False,
) -> BakeResult:
    """Append COPY directives to ``service_name``'s Dockerfile for each
    source bind mount.

    Raises ValueError when:
      - compose has no service of this name
      - service has no `build:` stanza (only `image:` services don't have a
        Dockerfile to modify)

    Returns a :class:`BakeResult` with skipped_reason set when the service
    has no source bind mounts (image-only deploy already correct), or when
    every COPY is already present and ``force`` is False.
    """
    if not compose_path.exists():
        raise ValueError(f"compose file not found: {compose_path}")
    compose = yaml.safe_load(compose_path.read_text()) or {}
    services = compose.get("services") or {}
    if service_name not in services:
        raise ValueError(
            f"service {service_name!r} not in compose. Available: "
            f"{sorted(services)!r}"
        )
    svc_compose = services[service_name] or {}
    if not isinstance(svc_compose, dict):
        raise ValueError(f"service {service_name!r}: invalid compose entry")

    dockerfile_path = _resolve_dockerfile(compose_path, service_name, svc_compose)
    if dockerfile_path is None:
        raise ValueError(
            f"service {service_name!r}: no `build:` stanza in compose — "
            f"image-only services don't have a Dockerfile to bake into."
        )
    if not dockerfile_path.exists():
        raise ValueError(
            f"service {service_name!r}: Dockerfile not found at "
            f"{dockerfile_path}"
        )

    volumes = svc_compose.get("volumes") or []
    candidate_copies: list[tuple[str, str]] = []
    for entry in volumes:
        parsed = _split_volume_entry(entry)
        if parsed is None:
            continue
        host, container = parsed
        if not _is_source_bind_mount(host, container):
            continue
        candidate_copies.append((host, container))

    result = BakeResult(dockerfile_path=dockerfile_path)
    if not candidate_copies:
        result.skipped_reason = (
            "service has no source bind mounts to bake (no relative-path "
            "volumes)."
        )
        return result

    # rc-4mf: filter out COPYs whose host source is .dockerignored. Without
    # this, docker build dies with 'failed to compute cache key: ...
    # "<host>": not found'. We surface the skipped pairs so the caller can
    # warn.
    ctx_path = _resolve_build_context(compose_path, svc_compose)
    ignored = _load_dockerignore(ctx_path) if ctx_path else set()
    copies: list[tuple[str, str]] = []
    for host, container in candidate_copies:
        if _is_dockerignored(host, ignored):
            result.skipped_dockerignored.append((host, container))
        else:
            copies.append((host, container))

    if not copies:
        result.skipped_reason = (
            "every source bind mount is excluded by .dockerignore — nothing "
            "to bake. Remove the dockerignore entries for paths you want "
            "baked into the image, or accept that ECS won't have these dirs."
        )
        return result

    existing = dockerfile_path.read_text()
    new_copies: list[tuple[str, str]] = []
    for host, container in copies:
        # Match `COPY <host> <container>` — fuzzy on whitespace.
        signature = f"COPY {host} {container}".strip()
        if not force and signature in existing:
            continue
        new_copies.append((host, container))

    if not new_copies:
        result.skipped_reason = (
            "every COPY already present in the Dockerfile (use --force to "
            "append duplicates)."
        )
        return result

    appended = ["", "# rc-bys: bake bind-mount source dirs into the image so",
                "# ECS deploys have the source baked in. Local docker-compose",
                "# still overrides these paths via bind mounts at runtime, so",
                "# hot-reload keeps working for local dev."]
    for host, container in new_copies:
        appended.append(f"COPY {host} {container}")
    appended.append("")
    new_content = existing.rstrip() + "\n" + "\n".join(appended)
    dockerfile_path.write_text(new_content)
    result.copies_added = new_copies
    return result
