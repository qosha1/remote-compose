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


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

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
) -> str:
    """Read a docker-compose file and return v2 rc.yml as a string.

    Pure function: takes paths but does not write anything. Caller writes
    the returned text to disk.

    If ``output_path`` is provided, ``compose_file`` and ``secrets[*].path``
    are written relative to its parent directory so the generated rc.yml
    works regardless of where it's saved (e.g., ``-o /tmp/rc.yml`` while
    the compose lives in ``/Users/foo/proj/``).
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

    header = (
        f"# rc.yml — generated by `rc init --from-compose {compose_path.name}`\n"
        f"# Edit project / region / cpu / memory / health_check_path before deploying.\n"
        f"# Excluded services (dev-only sidecars): "
        f"{', '.join(sorted(excluded)) if excluded else '(none)'}\n"
        f"# Public service: {public if public else '(none — set public: true on one entry)'}\n\n"
    )
    body = yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
    return header + body
