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
  rc-e5u.44.18 — nginx upstream-resolver detector (Cloud Map gotcha)
  rc-e5u.44.19 — same detector, now with vpc-derived resolver IP, FQDN, and
                 Django Host/ALLOWED_HOSTS hint (verified 2026-04-26)
  rc-e5u.44.23 — Django ALLOWED_HOSTS proactive heads-up (one warning per
                 Django-shaped service, fires even without an nginx front)
"""

from __future__ import annotations

import ipaddress
import os
import re
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
# Detector 5 — nginx upstream uses Cloud Map service name without resolver
#              (rc-e5u.44.18 — verified 2026-04-26 on rc-test-startsimpli)
# ---------------------------------------------------------------------------


# Match an upstream block: `upstream NAME { ...body... }`. Body capture is
# non-greedy and stops at the next `}`, which is correct because nginx's
# `upstream` directive cannot contain nested blocks. Multiline.
_UPSTREAM_BLOCK_RE = re.compile(
    r"upstream\s+([\w.-]+)\s*\{([^}]*)\}",
    re.MULTILINE | re.DOTALL,
)
# Within an upstream body, find each `server HOST:PORT[ options];`.
_SERVER_DIRECTIVE_RE = re.compile(
    r"\bserver\s+([\w.-]+):(\d+)\b",
)
# A `resolver` directive at the http {} or stream {} level signals the user
# already configured runtime DNS lookups. Suppress the warning when present.
_RESOLVER_DIRECTIVE_RE = re.compile(r"^\s*resolver\s+\S+", re.MULTILINE)
# Hostnames we explicitly do NOT flag — local-only refs that aren't ECS-affected.
_NEVER_FLAG_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _scan_upstream_servers(text: str) -> list[tuple[str, str, int]]:
    """Return ``(upstream_name, server_host, port)`` triples found in nginx
    config text. Matches both single-line (``upstream a { server x:y; }``)
    and multi-line upstream blocks. nginx upstream blocks can't nest, so
    the non-greedy ``[^}]*`` body capture is correct.
    """
    out: list[tuple[str, str, int]] = []
    for m in _UPSTREAM_BLOCK_RE.finditer(text):
        upstream_name = m.group(1)
        body = m.group(2)
        for sm in _SERVER_DIRECTIVE_RE.finditer(body):
            host = sm.group(1)
            try:
                port = int(sm.group(2))
            except ValueError:
                continue
            out.append((upstream_name, host, port))
    return out


from .defaults import VPC_CIDR_DEFAULT as _VPC_CIDR_DEFAULT


def _resolver_ip_for(vpc_cidr: Optional[str]) -> str:
    """VPC's internal DNS resolver = network base + 2 (AWS convention).

    For a Fargate awsvpc task the only reachable resolver IS this address —
    the EC2-host metadata IP 169.254.169.253 doesn't route from a task ENI,
    even though it's the obvious-looking "AWS internal" address. Verified
    2026-04-26 by reading /etc/resolv.conf inside a running ECS task.
    """
    cidr = vpc_cidr or _VPC_CIDR_DEFAULT
    try:
        network = ipaddress.ip_network(cidr, strict=False)
        return str(network.network_address + 2)
    except (ValueError, TypeError):
        # Malformed CIDR — fall back to the rc default's .2.
        return "10.0.0.2"


def _looks_like_django_service(svc_compose: dict, compose_path: Path) -> bool:
    """True iff this compose service's Dockerfile looks like Django.

    Thin wrapper around the framework registry (rc-e5u.47): a service is
    "Django-shaped" when ``detect_framework`` resolves to the ``django``
    preset. Kept as a public helper for back-compat with init_from_compose
    and external callers; new code should prefer ``detect_framework``
    directly so it gets equivalent treatment for Rails / Phoenix / etc.
    """
    from remote_compose.frameworks import detect_framework
    fw = detect_framework(svc_compose, compose_path)
    return bool(fw and fw.name == "django")


def detect_nginx_upstream_resolver(
    compose: dict,
    compose_path: Path,
    rc_v2_raw: Optional[dict] = None,
) -> list[str]:
    """Warn when nginx.conf has ``upstream { server <compose-svc>:<port>; }``
    without a ``resolver`` directive.

    Stock nginx resolves upstream hostnames ONCE at config-load time. Two
    failure modes on ECS Cloud Map:
      1. Lookup fails at startup -> nginx exits with `host not found`.
      2. Lookup succeeds at startup -> nginx caches the IP forever; when the
         backing task is replaced (rc deploy, autoscale, crash) the cached IP
         goes stale and proxy_pass returns 502.

    The fix uses the VPC's own DNS resolver + per-request resolution. The
    warning text is templated from rc.yml so a user copy-pasting it onto
    THIS stack gets a working snippet (right resolver IP, right FQDN, and
    a Host-header rewrite when the upstream looks like Django — verified
    2026-04-26 the hard way against rc-test-startsimpli).

    Only flag hosts that match a compose service name — those are the ones
    served by Cloud Map. External hostnames (api.example.com, S3 endpoints)
    do their own resolution and are out of scope.
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict) or not services:
        return warnings
    service_names = set(services.keys())
    seen: set[tuple[str, str, str]] = set()

    # Pull the vpc_cidr + project from rc.yml so the warning can hand the
    # user the EXACT snippet that will work on their stack.
    rc_raw = rc_v2_raw or {}
    project = str(rc_raw.get("project") or "")
    ecs_cfg = ((rc_raw.get("provider_config") or {}).get("ecs") or {})
    vpc_cidr = ecs_cfg.get("vpc_cidr")
    resolver_ip = _resolver_ip_for(vpc_cidr)
    namespace = f"{project}.local" if project else "<project>.local"

    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        ctx_path = _resolve_build_context(svc_compose, compose_path)
        if ctx_path is None or not ctx_path.exists() or not ctx_path.is_dir():
            continue
        # Reuse the same .conf scanning budget as detect_bad_hosts.
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
            try:
                if fpath.stat().st_size > _MAX_FILE_BYTES:
                    continue
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            # Suppress when user already has a resolver directive — they've
            # done the right thing and any `server X:port;` in upstreams is
            # likely intentional + paired with $var-based proxy_pass.
            if _RESOLVER_DIRECTIVE_RE.search(content):
                continue
            for upstream_name, host, port in _scan_upstream_servers(content):
                if host in _NEVER_FLAG_HOSTS:
                    continue
                if host not in service_names:
                    # External hostname — out of scope for this detector.
                    continue
                key = (svc_name, str(fpath), f"{upstream_name}:{host}:{port}")
                if key in seen:
                    continue
                seen.add(key)
                # FQDN form: nginx's resolver does NOT follow /etc/resolv.conf
                # search domains, so a bare `host` query returns NXDOMAIN.
                # Cloud Map registers each service under <svc>.<project>.local.
                fqdn = f"{host}.{namespace}:{port}"
                django_hint = ""
                upstream_compose = services.get(host)
                if isinstance(upstream_compose, dict) and \
                        _looks_like_django_service(upstream_compose, compose_path):
                    django_hint = (
                        f" Django gotcha: the upstream {host!r} is a Django "
                        f"service and its ALLOWED_HOSTS check rejects requests "
                        f"with the ALB DNS in the Host header (returns 400 "
                        f"SuspiciousOperation). In the same location block "
                        f"add 'proxy_set_header Host localhost;' (or set "
                        f"DJANGO_ALLOWED_HOSTS=* in the env)."
                    )
                warnings.append(
                    f"service {svc_name!r}: {fpath} declares "
                    f"'upstream {upstream_name} {{ server {host}:{port}; }}' "
                    f"without a 'resolver' directive — stock nginx caches the "
                    f"DNS lookup at startup, which breaks on Cloud Map task "
                    f"replacements (stale IP) and fails outright if {host!r} "
                    f"isn't resolvable at config-load time. Add: "
                    f"'resolver {resolver_ip} valid=10s ipv6=off;' at the "
                    f"http{{}} level (the VPC's .2 address — Fargate tasks "
                    f"can't reach 169.254.169.253) + 'set $u \"{fqdn}\"; "
                    f"proxy_pass http://$u;' in the location block (FQDN "
                    f"form because nginx's resolver doesn't follow the "
                    f"/etc/resolv.conf search domain).{django_hint}"
                )
    return warnings


# ---------------------------------------------------------------------------
# Detector 6 — Django ALLOWED_HOSTS proactive heads-up (rc-e5u.44.23)
# ---------------------------------------------------------------------------


def _service_has_django_allowed_hosts_override(
    svc_name: str,
    svc_compose: dict,
    rc_v2_raw: dict,
) -> bool:
    """Return True when the user has already wired DJANGO_ALLOWED_HOSTS.

    Suppression rule for the .44.23 detector: if the user has set
    DJANGO_ALLOWED_HOSTS in compose ``environment:`` (any value — even
    explicit overrides like ``mydomain.com`` count as "they thought about
    it") OR in rc.yml ``services.<svc>.env``, the warning is silenced.
    Detecting env-file contents is out of scope (they may legitimately
    not be readable at plan time).
    """
    env = svc_compose.get("environment")
    if isinstance(env, dict):
        if "DJANGO_ALLOWED_HOSTS" in env:
            return True
    elif isinstance(env, list):
        for entry in env:
            s = str(entry)
            if s.startswith("DJANGO_ALLOWED_HOSTS=") or s == "DJANGO_ALLOWED_HOSTS":
                return True
    rc_services = (rc_v2_raw or {}).get("services") or {}
    rc_svc = rc_services.get(svc_name) if isinstance(rc_services, dict) else None
    if isinstance(rc_svc, dict):
        rc_env = rc_svc.get("env") or {}
        if isinstance(rc_env, dict) and "DJANGO_ALLOWED_HOSTS" in rc_env:
            return True
    return False


def detect_django_allowed_hosts(
    compose: dict,
    compose_path: Path,
    rc_v2_raw: Optional[dict] = None,
) -> list[str]:
    """Warn for each Django-shaped service that will be deployed via ALB.

    Verified 2026-04-26 against rc-test-startsimpli: even after fixing
    nginx upstream-resolver (.44.19), Django still rejected requests with
    400 SuspiciousOperation because ALLOWED_HOSTS didn't include the ALB
    DNS name. The two known fixes are
      (a) ``DJANGO_ALLOWED_HOSTS=*`` in env, or
      (b) ``proxy_set_header Host localhost;`` upstream of Django (what
          ``rc fix nginx-conf`` emits for --django upstreams).
    Both are non-obvious — this detector surfaces the gotcha PROACTIVELY
    during ``rc plan`` even when there's no nginx-fronted setup, since
    the bare-Django-on-ECS case has the same problem.

    One warning per Django-shaped service (NOT one per upstream — the
    nginx detector .44.19 already enumerates upstreams). Reuses
    ``_looks_like_django_service`` so the heuristic stays in lockstep
    with the nginx detector's Django hint.
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    rc_raw = rc_v2_raw or {}
    seen: set[str] = set()
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        if svc_name in seen:
            continue
        if not _looks_like_django_service(svc_compose, compose_path):
            continue
        if _service_has_django_allowed_hosts_override(svc_name, svc_compose, rc_raw):
            # User has already set DJANGO_ALLOWED_HOSTS — they're aware.
            continue
        seen.add(svc_name)
        warnings.append(
            f"service {svc_name!r} has a Django-shaped Dockerfile "
            f"(manage.py / wsgi.py / django dep) + will be deployed via "
            f"ALB → Django's ALLOWED_HOSTS check rejects unknown Host "
            f"headers (returns 400 SuspiciousOperation). Either set "
            f"DJANGO_ALLOWED_HOSTS=* in env, or use 'rc fix nginx-conf' / "
            f"'proxy_set_header Host localhost;' upstream of django."
        )
    return warnings


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


_LOCALHOST_HOST_VALUES = {"localhost", "127.0.0.1", "host.docker.internal"}
# rc-7qq: KEYs that name another compose service. Pattern: <SVC>_HOST,
# DATABASE_HOST etc. We special-case the most common ones rather than
# guessing from arbitrary key names.
_HOST_KEY_PATTERN = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*_)?(?:HOST|HOSTNAME)$"
)


def detect_localhost_host_in_env_file(
    compose: dict, compose_path: Path,
) -> list[str]:
    """rc-7qq: warn when a compose service's env_file declares
    ``<SOMETHING>_HOST=localhost`` (or 127.0.0.1, host.docker.internal)
    while ANOTHER compose service exists with a name matching the prefix.

    Sentinal repro: .test/.postgres had POSTGRES_HOST=localhost. In
    docker-compose-on-host that resolves to the postgres container via
    host networking. In ECS each task has its own network namespace, so
    localhost is the django task itself — django then fails to connect
    on localhost:5434. The right value for ECS is ``postgres`` (the
    Cloud Map service-discovery FQDN).

    Heuristic: when we see ``<X>_HOST=localhost`` in an env_file AND a
    compose service named ``<x>`` (lowercased prefix) exists, warn.
    Avoids false positives on standalone uses of ``localhost`` for things
    like ``REDIS_HOST=localhost`` when there's no redis service declared.
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    compose_dir = compose_path.parent if compose_path else Path.cwd()
    service_names_lower = {str(n).lower() for n in services.keys()}
    seen: set[tuple[str, str, str]] = set()
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        env_files = svc_compose.get("env_file")
        if env_files is None:
            continue
        if isinstance(env_files, str):
            env_files = [env_files]
        for ref in env_files:
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = (compose_dir / ref_path).resolve()
            try:
                if not ref_path.exists() or ref_path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = ref_path.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if not _HOST_KEY_PATTERN.match(key):
                    continue
                if value not in _LOCALHOST_HOST_VALUES:
                    continue
                # Derive the prefix: POSTGRES_HOST → 'postgres'.
                prefix = key.rsplit("_", 1)[0].lower() if "_" in key else key.lower()
                if prefix == "host" or prefix == "hostname":
                    # Bare HOST=localhost — too generic to flag.
                    continue
                if prefix not in service_names_lower:
                    continue
                seen_key = (svc_name, str(ref_path), key)
                if seen_key in seen:
                    continue
                seen.add(seen_key)
                warnings.append(
                    f"service {svc_name!r}: {ref_path} declares "
                    f"{key}={value} but compose service {prefix!r} exists. "
                    f"In ECS, services are reachable by name via Cloud Map "
                    f"DNS — set {key}={prefix} so {svc_name} can connect to "
                    f"the {prefix} task. {value} would resolve to the "
                    f"{svc_name} task itself."
                )
    return warnings


# rc-562: capture every `resolver <IP> [opts]` directive so we can compare
# the configured IP against the IP that matches rc.yml.vpc_cidr today.
_RESOLVER_IP_RE = re.compile(
    r"^\s*resolver\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b",
    re.MULTILINE,
)


def detect_stale_nginx_resolver_ip(
    compose: dict,
    compose_path: Path,
    rc_v2_raw: Optional[dict] = None,
) -> list[str]:
    """rc-562: warn when an nginx config file has ``resolver <IP>`` baked
    in with an IP that doesn't match the VPC's current resolver.

    `rc fix nginx-conf` writes the resolver IP derived from
    rc.yml.provider_config.ecs.vpc_cidr at GENERATION time. If the user
    later changes vpc_cidr (e.g. moves to a new VPC) without re-running
    `rc fix nginx-conf`, the baked-in resolver IP belongs to the OLD VPC
    and Fargate tasks fail every Cloud Map lookup → 502s.

    We scan compose-build-context config files for resolver directives,
    extract the IP, and compare against the IP derived from current
    rc.yml.vpc_cidr (network base + 2). Mismatches → warn with the
    expected IP + the suggested re-run.
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict) or not services:
        return warnings
    rc_raw = rc_v2_raw or {}
    ecs_cfg = ((rc_raw.get("provider_config") or {}).get("ecs") or {})
    vpc_cidr = ecs_cfg.get("vpc_cidr")
    expected_ip = _resolver_ip_for(vpc_cidr)
    seen: set[tuple[str, str]] = set()
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        ctx_path = _resolve_build_context(svc_compose, compose_path)
        if ctx_path is None or not ctx_path.exists() or not ctx_path.is_dir():
            continue
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
            try:
                if fpath.stat().st_size > _MAX_FILE_BYTES:
                    continue
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            for m in _RESOLVER_IP_RE.finditer(content):
                found_ip = m.group(1)
                if found_ip == expected_ip:
                    continue
                key = (str(fpath), found_ip)
                if key in seen:
                    continue
                seen.add(key)
                warnings.append(
                    f"service {svc_name!r}: {fpath} has 'resolver "
                    f"{found_ip}' but rc.yml.vpc_cidr = "
                    f"{vpc_cidr or 'default'} → expected resolver IP "
                    f"is {expected_ip}. Cloud Map lookups will fail "
                    f"in this VPC. Re-run `rc fix nginx-conf` to "
                    f"regenerate the conf with the right IP."
                )
    return warnings


# rc-2v8: thresholds for the build-context size warning.
_LARGE_CONTEXT_WARN_BYTES = 1024 * 1024 * 1024  # 1 GiB
_LARGE_CONTEXT_BLOCK_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB
# Cap per-subtree work so a pathological tree doesn't make rc plan slow.
_MAX_ENTRIES_PER_SUBTREE = 100_000


def _human_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f}GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f}MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f}KB"
    return f"{n}B"


def _build_context_size(ctx_path: Path) -> tuple[int, list[tuple[str, int]]]:
    """Sum file sizes under ``ctx_path``. Return (total, sorted top-level entries).

    Walks each top-level entry via os.walk capped at
    ``_MAX_ENTRIES_PER_SUBTREE`` files to bound the worst case. Returns the
    top-level entry sizes sorted descending so callers can quote the
    heaviest dirs in a warning.
    """
    if not ctx_path.exists() or not ctx_path.is_dir():
        return 0, []
    sizes: dict[str, int] = {}
    try:
        entries = list(os.scandir(ctx_path))
    except OSError:
        return 0, []
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                sizes[entry.name] = entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total = 0
                count = 0
                for root, _dirs, files in os.walk(
                    entry.path, followlinks=False,
                ):
                    for f in files:
                        try:
                            total += os.lstat(os.path.join(root, f)).st_size
                        except OSError:
                            pass
                        count += 1
                        if count >= _MAX_ENTRIES_PER_SUBTREE:
                            break
                    if count >= _MAX_ENTRIES_PER_SUBTREE:
                        break
                sizes[entry.name] = total
        except OSError:
            continue
    total = sum(sizes.values())
    return total, sorted(sizes.items(), key=lambda kv: -kv[1])


def detect_large_build_context(
    compose: dict, compose_path: Path,
) -> list[str]:
    """rc-2v8: warn / error when a service's build context exceeds size
    thresholds.

    Sentinal repro: backend/ alone was 6.8GB (5.8GB of which was
    backend/backend/media — Django uploaded media). The first build
    hung 25+ min uploading the context to buildkit before adding
    .dockerignore exclusions. Detecting + naming the heaviest dirs at
    plan time saves that round-trip.

    Returns a list of WARN-prefixed strings (>1GB context) and ERROR-
    prefixed strings (>5GB). Callers decide how to render.
    """
    warnings: list[str] = []
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    seen_contexts: set[str] = set()
    for svc_name, svc_compose in services.items():
        if not isinstance(svc_compose, dict):
            continue
        ctx_path = _resolve_build_context(svc_compose, compose_path)
        if ctx_path is None:
            continue
        # Dedupe: many services sometimes share the same context.
        key = str(ctx_path.resolve()) if ctx_path.exists() else str(ctx_path)
        if key in seen_contexts:
            continue
        seen_contexts.add(key)
        total, top_dirs = _build_context_size(ctx_path)
        if total < _LARGE_CONTEXT_WARN_BYTES:
            continue
        # Format the top 3 heaviest entries for the user.
        top_summary = ", ".join(
            f"{name} ({_human_bytes(sz)})" for name, sz in top_dirs[:3]
        )
        severity = "ERROR" if total >= _LARGE_CONTEXT_BLOCK_BYTES else "WARN"
        warnings.append(
            f"{severity}: service {svc_name!r}: build context "
            f"{ctx_path} is {_human_bytes(total)}. Heaviest: "
            f"{top_summary}. Add to .dockerignore to slim the upload "
            f"to buildkit (the first build can hang for 30+ min on a "
            f"multi-GB context)."
        )
    return warnings


# rc-6jq: cap urls.py scanning so the detector stays cheap on plan.
_MAX_URLS_PY_FILES = 20
_MAX_URLS_PY_BYTES = 256 * 1024


def detect_unmatched_health_check_path(
    compose: dict,
    compose_path: Path,
    rc_v2_raw: Optional[dict] = None,
) -> list[str]:
    """rc-6jq: warn when rc.yml services.<svc>.health_check_path is set
    but the literal path doesn't appear anywhere in the service's
    Django build context (urls.py files in particular).

    Discovered during start-simpli redeploy: rc.yml had
    health_check_path: /api/health/ but the real endpoint was
    /api/v1/health/. ALB health checks 404'd → tasks drained → deploy
    stuck. Plan-time detection saves the round-trip.

    Heuristic only — false positives possible (e.g. routes constructed
    dynamically). Scans up to N urls.py files in the build context and
    looks for the configured health_check_path as a literal substring.
    Skips when health_check_path is the trivial '/' default.
    """
    warnings: list[str] = []
    rc_raw = rc_v2_raw or {}
    rc_services = (rc_raw.get("services") or {})
    if not isinstance(rc_services, dict):
        return warnings
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return warnings
    seen: set[tuple[str, str]] = set()
    for svc_name, svc_cfg in rc_services.items():
        if not isinstance(svc_cfg, dict):
            continue
        hc = svc_cfg.get("health_check_path")
        if not hc or hc in {"/", ""}:
            continue
        # Only check Django-shaped services — that's where this gotcha
        # actually shows up. Rails/Phoenix conventions differ enough
        # that the substring scan would miss legit cases.
        svc_compose = services.get(svc_name) or {}
        if not _looks_like_django_service(svc_compose, compose_path):
            continue
        ctx_path = _resolve_build_context(svc_compose, compose_path)
        if ctx_path is None or not ctx_path.exists() or not ctx_path.is_dir():
            continue
        # Scan urls.py files (capped). Look for the literal path.
        scanned = 0
        found = False
        sample_routes: list[str] = []
        for urls_py in ctx_path.rglob("urls.py"):
            if scanned >= _MAX_URLS_PY_FILES:
                break
            try:
                if urls_py.stat().st_size > _MAX_URLS_PY_BYTES:
                    continue
                content = urls_py.read_text(errors="replace")
            except OSError:
                continue
            scanned += 1
            # Django path() routes don't include the leading '/', so try
            # both forms. Also try without trailing slash for flexibility.
            stripped = hc.lstrip("/")
            candidates = (hc, hc.rstrip("/"), stripped, stripped.rstrip("/"))
            if any(c and c in content for c in candidates):
                found = True
                break
            # Collect a few sample routes for the warning.
            for m in re.finditer(r"""path\s*\(\s*['"]([^'"]+)['"]""", content):
                if len(sample_routes) < 5:
                    sample_routes.append(m.group(1))
        if not found and scanned > 0:
            key = (svc_name, hc)
            if key in seen:
                continue
            seen.add(key)
            samples = (
                f" Sample routes found: {sample_routes[:3]}."
                if sample_routes else ""
            )
            warnings.append(
                f"service {svc_name!r}: rc.yml health_check_path={hc!r} "
                f"not found in any of {scanned} urls.py file(s) under "
                f"{ctx_path}. ALB health checks may 404 → tasks drained "
                f"→ deploy stuck.{samples}"
            )
    return warnings


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
    out.extend(detect_nginx_upstream_resolver(compose, compose_path, rc_v2_raw))
    out.extend(detect_django_allowed_hosts(compose, compose_path, rc_v2_raw))
    out.extend(detect_localhost_host_in_env_file(compose, compose_path))
    out.extend(detect_stale_nginx_resolver_ip(compose, compose_path, rc_v2_raw))
    out.extend(detect_large_build_context(compose, compose_path))
    out.extend(detect_unmatched_health_check_path(compose, compose_path, rc_v2_raw))
    return out
