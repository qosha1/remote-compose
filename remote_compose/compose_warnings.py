"""Compose-file detectors emitted during ``rc plan``.

Each detector is a pure function that takes parsed compose data (and, where
necessary, the rc.yml v2 config + filesystem paths) and returns a list of
human-readable warning strings. The dispatcher in ``cli_v2.py`` runs the
suite during plan and surfaces results in ``render_plan`` output.

Detectors deliberately emit prose so the text of a single warning is
self-contained — the user reading ``rc plan`` output should not need to
chase a code or look anything up to act on it.

Beads:
  rc-e5u.44.6 — bind-mount detector
  rc-e5u.44.7 — external named volume detector
  rc-e5u.44.8 — host.docker.internal config detector
  rc-e5u.44.9 — multi-port ALB detector
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_compose(compose_path: Path) -> dict:
    """Return the parsed compose document or an empty dict if unreadable."""
    if not compose_path.exists():
        return {}
    try:
        with compose_path.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_volume_entries(svc_compose: dict) -> Iterable[Any]:
    """Yield raw entries from a compose service's volumes:[] (or nothing)."""
    vols = svc_compose.get("volumes") or []
    if isinstance(vols, list):
        for entry in vols:
            yield entry


def _classify_volume_entry(entry: Any) -> tuple[str, Optional[str], Optional[str]]:
    """Return ``(kind, source, target)`` for one compose volumes[] entry.

    kind is one of:
      - "bind"  -> a host path mounted in (./foo:/app or /abs:/c or long-form bind)
      - "named" -> a named volume reference (myvol:/data) or long-form volume
      - "anon"  -> anonymous volume (/data with no source)
      - "other" -> tmpfs or unknown / unparseable
    """
    if isinstance(entry, dict):
        # Long syntax: { type: bind|volume|tmpfs, source: ..., target: ... }
        vtype = entry.get("type")
        source = entry.get("source")
        target = entry.get("target")
        if vtype == "bind":
            return "bind", str(source) if source is not None else None, \
                str(target) if target is not None else None
        if vtype == "volume":
            return "named", str(source) if source is not None else None, \
                str(target) if target is not None else None
        if vtype == "tmpfs":
            return "other", None, str(target) if target is not None else None
        return "other", None, None

    s = str(entry)
    # Strip optional :ro / :rw / :z suffix.
    parts = s.split(":")
    if len(parts) == 1:
        return "anon", None, parts[0]
    # parts[0] is source, parts[1] is target. Mode/option in parts[2:] ignored.
    source = parts[0]
    target = parts[1]
    # bind mounts start with '.', '/', or '~' (or contain a path separator).
    if source.startswith(("./", "../", "/", "~")) or source.startswith("."):
        return "bind", source, target
    return "named", source, target


# ---------------------------------------------------------------------------
# Detector 1 — bind-mount volumes (rc-e5u.44.6)
# ---------------------------------------------------------------------------


def detect_bind_mounts(compose: dict) -> list[str]:
    """Warn when compose services declare bind-mount volumes.

    A bind mount maps a host path into the container ('./src:/app',
    '/abs:/data'). On ECS Fargate there is no host filesystem to mount
    from, so the runtime image must already contain the source. Silently
    dropping the mount risks shipping a stale or empty image.
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    seen: set[tuple[str, str, str]] = set()
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        for entry in _iter_volume_entries(svc_compose):
            kind, source, target = _classify_volume_entry(entry)
            if kind != "bind":
                continue
            host = source or "?"
            cont = target or "?"
            key = (svc_name, host, cont)
            if key in seen:
                continue
            seen.add(key)
            warnings.append(
                f"service {svc_name!r}: bind mount '{host}:{cont}' will not "
                f"be present in ECS — image must already contain this content."
            )
    return warnings


# ---------------------------------------------------------------------------
# Detector 2 — external named volumes silently dropped (rc-e5u.44.7)
# ---------------------------------------------------------------------------


def detect_external_volumes(compose: dict, rc_v2_raw: dict) -> list[str]:
    """Warn when a compose external named volume is mounted by a service
    but the service has no rc.yml volumes entry covering it.

    The rc.yml ``services.<svc>.volumes`` array is what the ECS provider
    uses to provision an EFS access point; volumes declared *only* in
    compose's top-level ``volumes:`` block (with ``external: true``) are
    silently dropped, which means data that the user expects to persist
    will be ephemeral.
    """
    warnings: list[str] = []
    top_volumes = compose.get("volumes") or {}
    if not isinstance(top_volumes, dict):
        return warnings
    external_names: set[str] = set()
    for vname, vcfg in top_volumes.items():
        if isinstance(vcfg, dict) and vcfg.get("external"):
            external_names.add(str(vname))
    if not external_names:
        return warnings

    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    rc_services = (rc_v2_raw or {}).get("services") or {}

    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        # rc.yml-declared volumes for this service (any non-empty list
        # signals the user wired their own persistent storage).
        rc_svc = rc_services.get(svc_name) if isinstance(rc_services, dict) else None
        rc_vols = []
        if isinstance(rc_svc, dict):
            rc_vols = rc_svc.get("volumes") or []
        rc_vols_have_entries = isinstance(rc_vols, list) and len(rc_vols) > 0
        for entry in _iter_volume_entries(svc_compose):
            kind, source, _target = _classify_volume_entry(entry)
            if kind != "named" or not source:
                continue
            if source not in external_names:
                continue
            if rc_vols_have_entries:
                # User wired persistent storage in rc.yml — assume they
                # know about this volume and suppress the warning.
                continue
            warnings.append(
                f"service {svc_name!r}: named volume {source!r} is declared "
                f"external in compose and not present in rc.yml services."
                f"{svc_name}.volumes — data will NOT persist across task restarts."
            )
    return warnings


# ---------------------------------------------------------------------------
# Detector 3 — host.docker.internal in build-context configs (rc-e5u.44.8)
# ---------------------------------------------------------------------------


_BAD_HOST = "host.docker.internal"
_CONFIG_GLOBS = ("**/nginx.conf", "**/*.conf")
_MAX_FILES_PER_CONTEXT = 32
_MAX_FILE_BYTES = 256 * 1024  # 256 KiB — config files are small


def _resolve_build_context(svc_compose: dict, compose_path: Path) -> Optional[Path]:
    build = svc_compose.get("build")
    if build is None:
        return None
    compose_dir = compose_path.parent
    if isinstance(build, str):
        return (compose_dir / build).resolve()
    if isinstance(build, dict):
        ctx = build.get("context", ".")
        return (compose_dir / ctx).resolve()
    return None


def detect_bad_hosts(compose: dict, compose_path: Path) -> list[str]:
    """Warn when a service's build-context config files reference
    ``host.docker.internal`` (unreachable inside a Fargate task).

    Heuristic: scan ``<context>/**/nginx.conf`` and ``<context>/**/*.conf``
    capped at a small file count and small byte budget per file. We
    deliberately do NOT flag ``localhost`` or ``127.0.0.1`` (those have
    legitimate intra-container uses).
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    seen: set[tuple[str, str]] = set()
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        ctx_path = _resolve_build_context(svc_compose, compose_path)
        if ctx_path is None or not ctx_path.exists() or not ctx_path.is_dir():
            continue
        scanned = 0
        # Use a set to dedup across overlapping globs (nginx.conf matches both).
        candidates: list[Path] = []
        for pattern in _CONFIG_GLOBS:
            for p in ctx_path.glob(pattern):
                if p in candidates:
                    continue
                candidates.append(p)
                if len(candidates) >= _MAX_FILES_PER_CONTEXT:
                    break
            if len(candidates) >= _MAX_FILES_PER_CONTEXT:
                break
        for fpath in candidates:
            if scanned >= _MAX_FILES_PER_CONTEXT:
                break
            try:
                if fpath.stat().st_size > _MAX_FILE_BYTES:
                    continue
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            scanned += 1
            if _BAD_HOST not in content:
                continue
            key = (svc_name, str(fpath))
            if key in seen:
                continue
            seen.add(key)
            warnings.append(
                f"service {svc_name!r}: {fpath} references "
                f"{_BAD_HOST!r} which is unreachable in ECS — "
                f"those upstreams will return 502."
            )
    return warnings


# ---------------------------------------------------------------------------
# Detector 4 — multi-port services (rc-e5u.44.9)
# ---------------------------------------------------------------------------


def _compose_container_ports(svc_compose: dict) -> list[int]:
    """Return sorted unique container-side ports for a compose service."""
    out: list[int] = []
    for entry in svc_compose.get("ports") or []:
        if isinstance(entry, dict):
            target = entry.get("target")
            if target is not None:
                out.append(int(target))
            continue
        s = str(entry)
        if s.count(":") == 2:
            s = s.split(":", 1)[1]
        if ":" in s:
            _h, c = s.split(":", 1)
        else:
            c = s
        c = c.split("/", 1)[0].strip()
        if c.isdigit():
            out.append(int(c))
    return sorted(set(out))


def detect_multi_port_alb(compose: dict, rc_v2_raw: dict) -> list[str]:
    """Warn when a service declares 2+ container ports — only the primary
    is wired into the ALB by the ECS provider today.

    Suppressed when the service is explicitly ``public: false`` in rc.yml
    (in which case the user knows the ports are intra-VPC only and no ALB
    is involved).
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    rc_services = (rc_v2_raw or {}).get("services") or {}
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        ports = _compose_container_ports(svc_compose)
        if len(ports) < 2:
            continue
        rc_svc = rc_services.get(svc_name) if isinstance(rc_services, dict) else None
        if isinstance(rc_svc, dict) and rc_svc.get("public") is False:
            # User explicitly opted out of ALB exposure.
            continue
        primary = ports[0]
        rest = ports[1:]
        port_list = ", ".join(str(p) for p in ports)
        rest_list = ", ".join(str(p) for p in rest)
        warnings.append(
            f"service {svc_name!r}: exposes ports [{port_list}]; only "
            f"{primary} is reachable via the ALB. Ports [{rest_list}] are "
            f"only reachable intra-VPC. To expose additional ports, see "
            f"rc-e5u.44.13 (extra_listeners) or use compose.exclude to "
            f"drop the secondary container."
        )
    return warnings


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def collect_compose_warnings(compose_path: Path, rc_v2_raw: dict) -> list[str]:
    """Run every compose-warning detector and return a flat list.

    Designed to be cheap (parses compose once, scans only obvious config
    file globs) so it can run on every ``rc plan`` invocation without a
    perceptible delay.
    """
    compose = _load_compose(compose_path)
    if not compose:
        return []
    out: list[str] = []
    out.extend(detect_bind_mounts(compose))
    out.extend(detect_external_volumes(compose, rc_v2_raw))
    out.extend(detect_bad_hosts(compose, compose_path))
    out.extend(detect_multi_port_alb(compose, rc_v2_raw))
    return out
