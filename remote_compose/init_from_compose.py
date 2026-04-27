"""Scaffold a v2 rc.yml from an existing docker-compose.*.yml.

Used by ``rc init --from-compose <path>``. Pure functions (input -> output)
so the heuristics can be unit-tested without filesystem I/O.

The scaffolder makes opinionated choices that the user can edit afterward:
- Per-service ``type`` is inferred from image / command / ports
- Per-type ``cpu`` / ``memory`` defaults
- One service marked ``public: true`` (the proxy / lone port-80 service)
- Browser-driven dev sidecars (LinkedIn / noVNC / chrome) added to
  ``compose.exclude`` since they have no production analog
- ``env_file:`` references become ``secrets:`` entries with ``source: file``
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from remote_compose.compose_warnings import _looks_like_django_service
from remote_compose.frameworks import Framework, detect_framework


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

# rc-e5u.46.3 / rc-e5u.47: the lifecycle.migrate hook command comes from the
# detected framework's preset (frameworks.py). Kept as a module-level
# constant for back-compat with callers/tests that imported it.
DEFAULT_DJANGO_MIGRATE_COMMAND = ["python", "manage.py", "migrate", "--noinput"]


def _any_framework_application_in(
    framework: Framework,
    services: dict[str, dict],
    compose_path: Path,
) -> bool:
    """True when at least one ``application``-typed service in compose
    matches the given framework. Used to gate the migrate-on-worker
    fallback."""
    for svc_name, svc_compose in services.items():
        fw = detect_framework(svc_compose, compose_path)
        if fw is None or fw.name != framework.name:
            continue
        if infer_service_type(svc_name, svc_compose) == "application":
            return True
    return False


def _alpha_first_framework_worker(
    framework: Framework,
    services: dict[str, dict],
    compose_path: Path,
) -> Optional[str]:
    """Return the alphabetically-first ``worker``-typed service that
    matches ``framework``. Deterministic fallback for the migrate hook
    when the project has no application service of this framework."""
    candidates = sorted(
        n for n, sc in services.items()
        if (fw := detect_framework(sc, compose_path)) is not None
        and fw.name == framework.name
        and infer_service_type(n, sc) == "worker"
    )
    return candidates[0] if candidates else None


# Back-compat shims for tests and external callers that imported these.
def _any_django_application_in(services: dict[str, dict], compose_path: Path) -> bool:
    from remote_compose.frameworks import DJANGO
    return _any_framework_application_in(DJANGO, services, compose_path)


def _alpha_first_django_worker(services: dict[str, dict], compose_path: Path) -> Optional[str]:
    from remote_compose.frameworks import DJANGO
    return _alpha_first_framework_worker(DJANGO, services, compose_path)


# rc-e5u.46.4 / rc-e5u.47: testing-defaults env vars come from the framework
# preset. Re-exported for back-compat — equals the Django preset.
TESTING_DEFAULTS_ENV: dict[str, str] = {
    "DJANGO_ALLOWED_HOSTS": "*",
    "CSRF_TRUSTED_ORIGINS": "*",
    "DJANGO_DEBUG": "False",
}


def _service_has_marker_env(svc_compose: dict, marker_keys: tuple[str, ...]) -> bool:
    """True when compose's environment already declares any of the
    framework's marker keys — signal that the user has hand-wired the
    host validation already.

    env_file contents are out of scope: the file may not be readable at
    scaffold time and a missing file is a perfectly valid scaffold input.
    Goal is "user has already wired it" not exhaustive coverage.
    """
    if not marker_keys:
        return False
    env = svc_compose.get("environment")
    if isinstance(env, dict):
        return any(k in env for k in marker_keys)
    if isinstance(env, list):
        for entry in env:
            s = str(entry)
            for k in marker_keys:
                if s.startswith(f"{k}=") or s == k:
                    return True
    return False


# Back-compat shim.
def _service_already_has_allowed_hosts(svc_compose: dict) -> bool:
    from remote_compose.frameworks import DJANGO
    return _service_has_marker_env(svc_compose, DJANGO.testing_defaults_marker_keys)

INFRA_IMAGE_PREFIXES = (
    "postgres",
    "mysql",
    "mariadb",
    "redis",
    "memcached",
    "rabbitmq",
    "mongo",
    "elasticsearch",
    "opensearch",
    "kafka",
    "zookeeper",
    "etcd",
    "consul",
    "vault",
)

PROXY_IMAGE_PREFIXES = ("nginx", "caddy", "traefik", "haproxy", "envoy")

PROXY_NAMES = ("nginx", "web", "proxy", "gateway", "traefik", "ingress")

WORKER_COMMAND_PREFIXES = ("celery", "rq", "sidekiq", "bull", "dramatiq")

# Service-name patterns that indicate dev-only sidecars unsuitable for ECS:
# LinkedIn worker uses noVNC + persistent chrome profile; *-dev signals an
# alternate dev-mode container that has no production target.
EXCLUDE_PATTERNS = (
    re.compile(r"linkedin", re.IGNORECASE),
    re.compile(r"chrome", re.IGNORECASE),
    re.compile(r"novnc", re.IGNORECASE),
    re.compile(r"playwright.*head", re.IGNORECASE),
    re.compile(r".*-dev$", re.IGNORECASE),
)


def should_exclude(service_name: str) -> bool:
    """Should this compose service be in compose.exclude (dev-only sidecar)?"""
    return any(p.search(service_name) for p in EXCLUDE_PATTERNS)


def _image_base(image: Optional[str]) -> str:
    """Strip registry, tag, and digest from an image ref. Returns lowercase."""
    if not image:
        return ""
    s = image.split("@", 1)[0]
    s = s.split(":", 1)[0]
    s = s.rsplit("/", 1)[-1]
    return s.lower()


def _command_first_token(command) -> str:
    if command is None:
        return ""
    if isinstance(command, list):
        if not command:
            return ""
        first = str(command[0])
    else:
        first = str(command).strip()
    # Strip leading shell wrappers
    if first in ("/bin/sh", "/bin/bash", "sh", "bash"):
        return ""
    # First word of the first token
    return first.split()[0] if first else ""


def _has_published_ports(compose_svc: dict) -> bool:
    return bool(compose_svc.get("ports"))


def infer_service_type(name: str, compose_svc: dict) -> str:
    """Pick one of {infrastructure, worker, application, proxy} for an rc.yml service."""
    image_base = _image_base(compose_svc.get("image"))
    name_lower = name.lower()

    # Proxy: matches proxy image OR matches a proxy-shaped name
    if any(image_base.startswith(p) for p in PROXY_IMAGE_PREFIXES):
        return "proxy"
    if name_lower in PROXY_NAMES:
        return "proxy"

    # Infrastructure: well-known stateful images
    if any(image_base.startswith(p) for p in INFRA_IMAGE_PREFIXES):
        return "infrastructure"

    # Worker: command starts with a queue runner OR no published ports + has a command
    cmd_token = _command_first_token(compose_svc.get("command"))
    if cmd_token and any(cmd_token.startswith(p) for p in WORKER_COMMAND_PREFIXES):
        return "worker"
    if not _has_published_ports(compose_svc) and (
        cmd_token or compose_svc.get("build") or compose_svc.get("image")
    ):
        # No exposed port → not an HTTP service. Could be a worker or a CLI.
        # Workers are the common case for compose-style deploys.
        return "worker"

    # Default: application (has a port, runs HTTP-ish)
    return "application"


CPU_MEM_DEFAULTS: dict[str, tuple[int, int]] = {
    "infrastructure": (512, 1024),
    "worker": (512, 1024),
    "application": (1024, 2048),
    "proxy": (256, 512),
}


def infer_cpu_memory(svc_type: str) -> tuple[int, int]:
    return CPU_MEM_DEFAULTS.get(svc_type, (512, 1024))


def _container_port(port_entry) -> Optional[int]:
    """Extract container-side port from a compose ports[] entry."""
    if isinstance(port_entry, dict):
        target = port_entry.get("target")
        return int(target) if target is not None else None
    s = str(port_entry)
    if s.count(":") == 2:
        s = s.split(":", 1)[1]
    container = s.split(":")[-1].split("/", 1)[0].strip()
    return int(container) if container.isdigit() else None


def pick_public_service(
    compose_services: dict[str, dict],
    excluded: set[str],
    override: Optional[str] = None,
) -> Optional[str]:
    """Pick the single service that should be ALB-fronted.

    Precedence:
      1. ``override`` (--public-service flag), if provided and exists
      2. proxy-shaped name in PROXY_NAMES
      3. proxy-shaped image in PROXY_IMAGE_PREFIXES
      4. the only service publishing port 80
      5. None
    """
    candidates = {n: s for n, s in compose_services.items() if n not in excluded}
    if not candidates:
        return None

    if override:
        if override in candidates:
            return override
        # Override given but not present (or excluded) — fall through. Caller
        # decides whether to error.
        return None

    # By name
    for name in candidates:
        if name.lower() in PROXY_NAMES:
            return name

    # By image
    for name, svc in candidates.items():
        if any(_image_base(svc.get("image")).startswith(p) for p in PROXY_IMAGE_PREFIXES):
            return name

    # Lone port-80 publisher
    port_80_services = [
        n for n, s in candidates.items()
        if any(_container_port(p) == 80 for p in (s.get("ports") or []))
    ]
    if len(port_80_services) == 1:
        return port_80_services[0]

    return None


# ---------------------------------------------------------------------------
# Project + secrets derivation
# ---------------------------------------------------------------------------

_PROJECT_NAME_RE = re.compile(r"[^a-z0-9]+")


def derive_project_name(compose_path: Path) -> str:
    """Use the compose file's parent directory as the project slug."""
    raw = compose_path.parent.name or "my-project"
    slug = _PROJECT_NAME_RE.sub("-", raw.lower()).strip("-")
    return slug or "my-project"


def collect_env_files(compose_services: dict[str, dict]) -> list[str]:
    """Return unique env_file paths across all services, in first-seen order."""
    seen: list[str] = []
    for svc in compose_services.values():
        ef = svc.get("env_file")
        if ef is None:
            continue
        entries = [ef] if isinstance(ef, str) else list(ef)
        for entry in entries:
            if entry not in seen:
                seen.append(str(entry))
    return seen


def secret_name_from_path(path: str) -> str:
    """Generate a deterministic, human-readable secret name from an env_file path.

    `.envs/.local/.django` -> `local-django`
    `.envs/.production/.postgres` -> `production-postgres`
    `secrets/api.env` -> `api-env`
    """
    p = Path(path)
    parts = [seg.lstrip(".") for seg in p.parts if seg and seg != "."]
    # Drop leading 'envs' if present (a Django convention)
    if parts and parts[0].lower() == "envs":
        parts = parts[1:]
    slug = _PROJECT_NAME_RE.sub("-", "-".join(parts).lower()).strip("-")
    return slug or "secret"


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def _relpath(target: Path, base: Path) -> Path:
    """Path of ``target`` relative to ``base``. POSIX-style for rc.yml portability."""
    import os
    return Path(os.path.relpath(target, base))


def generate_v2_rc_yml(
    compose_path: Path,
    output_path: Optional[Path] = None,
    public_service: Optional[str] = None,
    region: str = "us-west-2",
    aws_profile: Optional[str] = None,
    testing_defaults: Optional[bool] = None,
) -> str:
    """Read a docker-compose file and return v2 rc.yml as a string.

    Pure function: takes paths but does not write anything. Caller writes
    the returned text to disk.

    If ``output_path`` is provided, ``compose_file`` and ``secrets[*].path``
    are written relative to its parent directory so the generated rc.yml
    works regardless of where it's saved (e.g., ``-o /tmp/rc.yml`` while
    the compose lives in ``/Users/foo/proj/``).

    rc-e5u.46.4: ``testing_defaults`` controls injection of
    DJANGO_ALLOWED_HOSTS=* / CSRF_TRUSTED_ORIGINS=* / DJANGO_DEBUG=False
    into Django-shaped services' rc.yml ``env:`` block. None (default) =
    auto-enable when project starts with ``rc-test-``, suppress otherwise.
    True / False force on or off. Skipped per-service when compose env
    already declares DJANGO_ALLOWED_HOSTS (user knows what they want).
    """
    compose_path = Path(compose_path).resolve()
    with compose_path.open() as f:
        compose = yaml.safe_load(f) or {}
    services = compose.get("services") or {}
    if not services:
        raise ValueError(f"no services found in {compose_path}")

    rc_yml_dir = (Path(output_path).resolve().parent
                  if output_path else compose_path.parent)
    compose_file_field = _relpath(compose_path, rc_yml_dir).as_posix()

    project = derive_project_name(compose_path)
    excluded = {n for n in services if should_exclude(n)}
    public = pick_public_service(services, excluded, override=public_service)

    # rc-e5u.46.4: auto-enable when the project name signals an ephemeral
    # test stack. Manual True/False from the CLI takes precedence.
    if testing_defaults is None:
        testing_defaults = project.startswith("rc-test-")

    # Build the service entries in a stable order: deterministic for diffs.
    rc_services: dict = {}
    for name in sorted(services):
        if name in excluded:
            continue
        svc_compose = services[name]
        svc_type = infer_service_type(name, svc_compose)
        cpu, memory = infer_cpu_memory(svc_type)
        entry: dict = {
            "cpu": cpu,
            "memory": memory,
            "type": svc_type,
        }
        if name == public:
            entry["public"] = True
            ports = [_container_port(p) for p in (svc_compose.get("ports") or [])]
            primary = next((p for p in ports if p), None)
            if primary is not None:
                entry["port"] = primary
            entry["health_check_path"] = "/"
            entry["default_target"] = True
        # rc-e5u.46.3 / rc-e5u.47: framework-aware lifecycle.migrate hook +
        # testing-defaults env injection. The framework registry centralises
        # the per-framework specifics (Django: manage.py migrate, Rails:
        # rails db:migrate, Phoenix: mix ecto.migrate; Django ALLOWED_HOSTS=*,
        # Rails RAILS_HOSTS=*, etc.) — adding a new framework lights up these
        # same code paths automatically.
        #
        # Why the migrate hook is separate from container startup: migrations
        # baked into the entrypoint cascade as ECS task crash-loops (exit 1
        # → ECS retries forever). A SEPARATE one-shot hook lets `rc deploy`
        # run migrations via execute-command BEFORE the new task def goes
        # live: failures surface clearly + the application container itself
        # only does runtime work.
        #
        # Only fire on the APPLICATION-typed service. Workers usually share
        # the same Dockerfile as the application and would also match the
        # framework, but running migrate on every worker is wasteful + noisy.
        # When ALL framework-shaped services have type=worker (no
        # application), we still need ONE migrate hook — pick alpha-first
        # for determinism.
        framework = detect_framework(svc_compose, compose_path)
        if (
            framework is not None
            and framework.migrate_command is not None
            and (
                svc_type == "application"
                or (
                    svc_type == "worker"
                    and not _any_framework_application_in(framework, services, compose_path)
                    and name == _alpha_first_framework_worker(framework, services, compose_path)
                )
            )
        ):
            entry["lifecycle"] = {
                "migrate": {
                    "command": list(framework.migrate_command),
                    "auto_on_deploy": True,
                }
            }
            # rc-e5u.46.4: inject framework-specific star-host defaults when
            # the project name signals an ephemeral test stack (or the user
            # opted in). Skip when compose already declares any of the
            # framework's marker env keys — they're aware. cli_v2 merges
            # this dict on top of compose env at deploy time.
            if (
                testing_defaults
                and framework.testing_defaults_env
                and not _service_has_marker_env(
                    svc_compose, framework.testing_defaults_marker_keys
                )
            ):
                entry["env"] = dict(framework.testing_defaults_env)
        rc_services[name] = entry

    # env_file_auto: a single declaration replaces N per-file `source: file`
    # entries. cli_v2 expands it at deploy time by walking compose env_files,
    # uploading each as an SM blob, and stripping those keys from the task
    # def's plaintext environment[]. Users wanting per-file control can
    # rewrite this as multiple `source: file` entries with explicit paths.
    env_files = collect_env_files(services)
    secrets_block: list[dict] = []
    if env_files:
        secrets_block.append({
            "name": "env",
            "source": "env_file_auto",
        })

    config: dict = {
        "version": 2,
        "project": project,
        "compose_file": compose_file_field,
        "provider": "ecs",
        "provider_config": {
            "ecs": {
                "region": region,
                "cluster": f"{project}-cluster",
                "vpc_cidr": "10.42.0.0/16",
                "default_launch_type": "FARGATE",
            }
        },
        "terraform": {
            "output_dir": "./terraform/${provider}",
            "backend": {"type": "local"},
        },
        "services": rc_services,
    }
    if aws_profile:
        config["provider_config"]["ecs"]["aws_profile"] = aws_profile
    if excluded:
        config["compose"] = {"exclude": sorted(excluded)}
    if secrets_block:
        config["secrets"] = secrets_block

    testing_note = ""
    if testing_defaults:
        testing_note = (
            f"# Testing defaults: ON (project starts with 'rc-test-' or "
            f"--testing-defaults). Detected framework services get "
            f"host-validation overrides injected (Django: "
            f"DJANGO_ALLOWED_HOSTS=*, Rails: RAILS_HOSTS=*, Phoenix: "
            f"PHX_HOST=*) so plain `curl http://<ALB>/` works without\n"
            f"# nginx Host: rewrites. UNSAFE for production — rerun with "
            f"--no-testing-defaults to suppress.\n"
        )
    header = (
        f"# rc.yml — generated by `rc init --from-compose {compose_path.name}`\n"
        f"# Edit project / region / cpu / memory / health_check_path before deploying.\n"
        f"# Excluded services (dev-only sidecars): "
        f"{', '.join(sorted(excluded)) if excluded else '(none)'}\n"
        f"# Public service: {public if public else '(none — set public: true on one entry)'}\n"
        f"{testing_note}"
        f"#\n"
        f"# Fast rebuilds (rc-e5u.45.2): `rc deploy` uses `docker buildx build`\n"
        f"# with a shared ECR registry cache (<project>/buildcache), so layer\n"
        f"# cache survives across machines and CI runs. Speed up further by\n"
        f"# adding BuildKit cache mounts to your Dockerfile, e.g.:\n"
        f"#   RUN --mount=type=cache,target=/root/.cache/pip \\\n"
        f"#       pip install -r requirements.txt\n"
        f"#   RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \\\n"
        f"#       --mount=type=cache,target=/var/lib/apt,sharing=locked \\\n"
        f"#       apt-get update && apt-get install -y <pkgs>\n"
        f"# These mounts let pip/apt downloads survive even when their RUN\n"
        f"# layer is invalidated, dropping a code-only rebuild from minutes to\n"
        f"# seconds. Requires DOCKER_BUILDKIT=1 (default in modern Docker).\n\n"
    )
    body = yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
    return header + body


# ---------------------------------------------------------------------------
# Auto-fix: nginx upstream-resolver during `rc up` (rc-e5u.46.2)
# ---------------------------------------------------------------------------
#
# When `rc up --from-compose <path>` scaffolds a fresh rc.yml and the user's
# compose has an nginx service whose nginx.conf trips the .44.18 detector
# AND points at a Django-shaped upstream (.44.19), we silently chain the
# same logic as `rc fix nginx-conf`:
#   1. Generate compose/ecs/nginx/{Dockerfile,nginx.conf} alongside the
#      user's existing compose/local/nginx (write_ecs_nginx).
#   2. Patch services.nginx.dockerfile in the just-written rc.yml so
#      build_deploy_context (.46.1) builds the ECS-aware image.
#   3. Print a one-line "auto-fixed" notice. The deploy then proceeds.
#
# Why here (init_from_compose.py) and not cli.py: the helper is a pure
# transformation on (rc.yml path, compose path) — same shape as the
# scaffold function above. Keeping both in this module means cli.up()
# stays a thin orchestrator and the auto-fix can be unit-tested without
# spinning up a Click runner.


def detect_nginx_auto_fix_target(
    compose_path: Path,
    rc_v2_raw: dict,
) -> Optional[dict]:
    """Decide whether `rc up` should auto-run the nginx-conf fixer.

    Returns a dict ``{nginx_service, upstreams, project, vpc_cidr}`` when
    the .44.18 detector would fire on a service AND at least one of the
    referenced upstreams matches a framework that needs a Host: header
    rewrite (rc-e5u.47 — Django by default; Rails/Phoenix opt in via
    their preset's ``host_header_rewrite``). Returns None otherwise —
    caller skips the auto-fix in that case.
    """
    from remote_compose.compose_warnings import (
        _CONFIG_GLOBS,
        _MAX_FILE_BYTES,
        _NEVER_FLAG_HOSTS,
        _RESOLVER_DIRECTIVE_RE,
        _resolve_build_context,
        _scan_upstream_servers,
    )

    if not compose_path.exists():
        return None
    try:
        with compose_path.open() as f:
            compose = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(compose, dict):
        return None
    services = compose.get("services") or {}
    if not isinstance(services, dict) or not services:
        return None
    service_names = set(services.keys())

    # Walk every service that has a build context with a .conf in it; the
    # first one that trips the .44.18 condition becomes our auto-fix
    # target. CHECK PROXY-SHAPED SERVICES FIRST — for monorepo-style
    # compose where every service shares the same build.context (= project
    # root), any service's context-glob will find the nginx.conf at
    # compose/local/nginx/nginx.conf. Without this ordering, the auto-fix
    # mis-targets the first compose service (e.g., the django app), which
    # then gets built FROM nginx:1.25-alpine and dies on the migrate hook
    # because there's no python in nginx:alpine. (Verified the hard way
    # during .46.6 — burned ~8 minutes of fresh-stack deploy time.)
    proxy_first = sorted(
        services.keys(),
        key=lambda n: (
            0 if n.lower() in PROXY_NAMES else (
                1 if any(
                    _image_base(services[n].get("image") or "").startswith(p)
                    for p in PROXY_IMAGE_PREFIXES
                ) else 2
            ),
            n,
        ),
    )
    for nginx_name in proxy_first:
        nginx_compose = services[nginx_name]
        if not isinstance(nginx_compose, dict):
            continue
        ctx_path = _resolve_build_context(nginx_compose, compose_path)
        if ctx_path is None or not ctx_path.exists() or not ctx_path.is_dir():
            continue
        # Find the conf files (same budget as compose_warnings).
        candidates: list[Path] = []
        for pattern in _CONFIG_GLOBS:
            for p in ctx_path.glob(pattern):
                if p in candidates:
                    continue
                candidates.append(p)

        # Collect upstreams that map to compose service names — these
        # are the ones Cloud Map serves on ECS, i.e. the ones that need
        # the resolver+FQDN rewrite.
        upstream_hosts: dict[str, int] = {}
        tripped_resolver = False
        for fpath in candidates:
            try:
                if fpath.stat().st_size > _MAX_FILE_BYTES:
                    continue
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            # If the user already has a resolver directive they've
            # solved the problem manually — don't auto-fix.
            if _RESOLVER_DIRECTIVE_RE.search(content):
                continue
            for _upstream_name, host, port in _scan_upstream_servers(content):
                if host in _NEVER_FLAG_HOSTS:
                    continue
                if host not in service_names:
                    continue
                tripped_resolver = True
                # First port wins for a given host.
                upstream_hosts.setdefault(host, port)
        if not tripped_resolver or not upstream_hosts:
            continue

        # rc-e5u.47: filter to upstreams whose framework needs a Host:
        # header rewrite (Django: ALLOWED_HOSTS rejects on ALB DNS in Host;
        # other frameworks with strict host validation light this up by
        # setting host_header_rewrite on their preset). Skip auto-fix
        # entirely when no upstream needs the rewrite — manual
        # `rc fix nginx-conf` is still cheap and we don't want surprise
        # rewrites for stacks where the resolver fix is the only need.
        django_hosts: set[str] = set()
        for host in upstream_hosts:
            upstream_compose = services.get(host)
            if not isinstance(upstream_compose, dict):
                continue
            fw = detect_framework(upstream_compose, compose_path)
            if fw is not None and fw.host_header_rewrite:
                django_hosts.add(host)
        if not django_hosts:
            continue

        # Pull project + vpc_cidr from rc.yml so the generator's resolver
        # IP + Cloud Map FQDN match the actual stack.
        rc_raw = rc_v2_raw or {}
        project = str(rc_raw.get("project") or "")
        ecs_cfg = ((rc_raw.get("provider_config") or {}).get("ecs") or {})
        vpc_cidr = ecs_cfg.get("vpc_cidr")

        from remote_compose.fix_nginx_conf import Upstream
        # Stable order for deterministic output: declaration order from
        # the upstream-scan, with the Django one(s) marked.
        upstreams = [
            Upstream(name=host, port=port, django=host in django_hosts)
            for host, port in upstream_hosts.items()
        ]
        return {
            "nginx_service": nginx_name,
            "upstreams": upstreams,
            "project": project,
            "vpc_cidr": vpc_cidr,
        }
    return None


def _zone_from_domain_drop_leftmost(domain: str) -> str:
    """Heuristic Route 53 hosted-zone name derivation for ``rc up --domain``.

    Drops the LEFTMOST label of an FQDN — for ``a.b.c.d`` returns ``b.c.d``;
    for an apex ``b.c`` returns ``b.c`` unchanged. Different from the
    provider's last-two-labels fallback because users typically delegate a
    subdomain (e.g. ``rctest.ezapps.ai``) and deploy ephemeral apps under it
    (``startsimpli-test.rctest.ezapps.ai``) — the hosted zone is the parent
    of the FQDN, not the registrable apex. ``--route53-zone <Z>`` is the
    explicit override when this heuristic is wrong.
    """
    parts = domain.strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[1:])


def _patch_rc_yml_domain(
    rc_yml_path: Path,
    domain: str,
    aliases: list[str],
    route53_zone: Optional[str] = None,
) -> dict:
    """Wire ``--domain`` / ``--alias`` / ``--route53-zone`` into a scaffolded
    rc.yml on disk. Returns the resolved settings dict.

    Mutates the YAML body, preserves any leading comment block. Idempotent
    (re-running with the same values is a no-op). Targets the service that
    has ``public: true`` — that's the ALB-fronted entry the scaffolder
    picked. If no public service exists, raises ValueError.
    """
    text = rc_yml_path.read_text()
    lines = text.splitlines(keepends=True)
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            header_lines.append(line)
            continue
        body_start = i
        break
    body_text = "".join(lines[body_start:])
    raw = yaml.safe_load(body_text) or {}
    services = raw.get("services") or {}
    if not isinstance(services, dict):
        raise ValueError("services block in rc.yml is not a mapping")
    public_name = next(
        (n for n, s in services.items() if isinstance(s, dict) and s.get("public")),
        None,
    )
    if public_name is None:
        raise ValueError(
            "no public service in rc.yml — cannot wire --domain. Mark one "
            "service with `public: true` first."
        )
    public_entry = services[public_name]
    public_entry["domain"] = domain
    if aliases:
        # Stable order: declaration order, deduped.
        seen: set[str] = set()
        deduped: list[str] = []
        for a in aliases:
            if a in seen or a == domain:
                continue
            seen.add(a)
            deduped.append(a)
        public_entry["aliases"] = deduped

    zone = route53_zone or _zone_from_domain_drop_leftmost(domain)
    pc = raw.setdefault("provider_config", {})
    if not isinstance(pc, dict):
        pc = {}
        raw["provider_config"] = pc
    ecs_cfg = pc.setdefault("ecs", {})
    if not isinstance(ecs_cfg, dict):
        ecs_cfg = {}
        pc["ecs"] = ecs_cfg
    ecs_cfg["route53_zone"] = zone

    new_body = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    rc_yml_path.write_text("".join(header_lines) + new_body)
    return {
        "public_service": public_name,
        "domain": domain,
        "aliases": list(public_entry.get("aliases", [])),
        "route53_zone": zone,
    }


def _patch_rc_yml_dockerfile(
    rc_yml_path: Path,
    service_name: str,
    dockerfile_value: str,
) -> None:
    """Add ``services.<name>.dockerfile: <value>`` to an rc.yml on disk.

    Re-serialises with yaml.safe_dump so PyYAML's ordering is preserved
    for new keys (top-level keys keep their insertion order; we only
    mutate the nested service entry). Header comments above the YAML
    body are retained verbatim. The single-write approach keeps the
    helper idempotent — re-running on an already-patched rc.yml is a
    no-op (we still write but the value is the same).
    """
    text = rc_yml_path.read_text()
    # Split off the leading comment block (lines that start with `#` or
    # are blank) so we can preserve it verbatim. Once we hit the first
    # non-comment/non-blank line we stop — that's the YAML body.
    lines = text.splitlines(keepends=True)
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            header_lines.append(line)
            continue
        body_start = i
        break
    body_text = "".join(lines[body_start:])
    raw = yaml.safe_load(body_text) or {}
    services = raw.setdefault("services", {})
    if not isinstance(services, dict):
        services = {}
        raw["services"] = services
    svc_entry = services.setdefault(service_name, {})
    if not isinstance(svc_entry, dict):
        svc_entry = {}
        services[service_name] = svc_entry
    svc_entry["dockerfile"] = dockerfile_value
    new_body = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    rc_yml_path.write_text("".join(header_lines) + new_body)


def auto_fix_nginx_if_needed(
    rc_yml_path: Path,
    compose_path: Path,
    output_subdir: str = "compose/ecs/nginx",
) -> Optional[dict]:
    """Run the rc-fix-nginx-conf logic when warranted; mutate rc.yml.

    Wired into ``rc up`` after the scaffold step (rc-e5u.46.2). When the
    .44.18 detector would fire AND the upstream(s) include a Django-shaped
    service (.44.19), generate compose/ecs/nginx/{Dockerfile,nginx.conf}
    in the user's project and patch the rc.yml's services.<nginx>.dockerfile
    to point at the generated path (leveraging .46.1).

    Returns a dict ``{nginx_path, dockerfile_path, nginx_service,
    upstreams}`` describing what was done, or None if no auto-fix was
    needed (caller prints nothing in that case). Failures during write
    are surfaced as exceptions; cli.up() catches them and warns rather
    than aborting the deploy.
    """
    rc_yml_path = Path(rc_yml_path)
    compose_path = Path(compose_path)
    if not rc_yml_path.exists():
        return None
    rc_raw = yaml.safe_load(rc_yml_path.read_text()) or {}
    target = detect_nginx_auto_fix_target(compose_path, rc_raw)
    if target is None:
        return None

    from remote_compose.fix_nginx_conf import write_ecs_nginx

    # rc-e5u.46.6 finding: write the ECS-aware nginx files NEXT TO the user's
    # compose file (= the docker build context's parent in ~all real-world
    # layouts), not next to the rc.yml. Reason: when the rc.yml lives in /tmp
    # (scratch) but the compose lives in ~/code/myproj/, docker buildx looks
    # for ./compose/ecs/nginx/Dockerfile RELATIVE to the build context (=
    # compose dir = ~/code/myproj/). Writing to /tmp/compose/ecs/nginx/ leaves
    # that dir empty and docker picks up whatever ALREADY exists at
    # ~/code/myproj/compose/ecs/nginx/Dockerfile (a stale hand-edit, leftover
    # from a prior session, etc.) — the failure mode burned ~25min of e2e
    # debug time. Anchoring on compose_path means the files live with the
    # user's source tree, version-controllable, and the build context picks
    # them up correctly.
    project_dir = compose_path.parent.resolve()
    nginx_path, dockerfile_path = write_ecs_nginx(
        project_dir=project_dir,
        upstreams=target["upstreams"],
        project=target["project"],
        vpc_cidr=target["vpc_cidr"],
        force=True,  # `rc up` is the orchestrator; clobber prior auto-fix.
        output_subdir=output_subdir,
    )
    # Patch rc.yml to point services.<nginx>.dockerfile at the generated
    # Dockerfile. Path is relative-to-build-context (compose semantics);
    # compose's build.context for this service is typically the project
    # root, so a leading ./ keeps it user-readable.
    dockerfile_rel = f"./{output_subdir}/Dockerfile"
    _patch_rc_yml_dockerfile(
        rc_yml_path,
        target["nginx_service"],
        dockerfile_rel,
    )
    return {
        "nginx_service": target["nginx_service"],
        "nginx_path": nginx_path,
        "dockerfile_path": dockerfile_path,
        "upstreams": target["upstreams"],
        "dockerfile_rel": dockerfile_rel,
        "output_subdir": output_subdir,
    }
