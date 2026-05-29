"""ECS-ready nginx scaffolder — ``rc fix nginx-conf``.

Produces an nginx.conf + Dockerfile pair that proxies one or more upstreams
using the variable-based proxy_pass pattern that survives Cloud Map task
replacements + Django ALLOWED_HOSTS rejection.

Why a separate file (option B from the bead): in-place rewrites of a
hand-tuned nginx.conf are hairy (the grammar is permissive + per-directive,
and we'd have to preserve comments/server blocks/etc.) and the user
typically wants to keep their LOCAL conf untouched. Instead we emit a
NEW pair under ``compose/ecs/nginx/`` and tell the user how to wire it
into their compose ECS variant.

Background — why this generator exists:
  Verified 2026-04-26 against the rc-test-startsimpli stack: getting an
  nginx-as-front pattern working on ECS required hand-writing a new
  nginx.conf with three pieces in lockstep —
    1. ``resolver <vpc_cidr_base+2> valid=10s ipv6=off;`` at http{} level
       (NOT 169.254.169.253 — that's the EC2-host metadata IP, unreachable
       from a Fargate task ENI).
    2. NO ``upstream { server X:Y; }`` blocks (stock nginx caches the
       lookup at config-load time → stale IP after task replacement +
       startup-fail when the upstream isn't yet reachable).
    3. Each ``proxy_pass http://name`` rewritten to ``set $u "<svc>.<project>
       .local:<port>"; proxy_pass http://$u;`` — the variable form forces
       per-request resolution AND uses the Cloud Map FQDN (nginx's
       resolver does not honour /etc/resolv.conf's search domain).
  For Django upstreams we additionally inject ``proxy_set_header Host
  localhost;`` so Django's ALLOWED_HOSTS check passes regardless of the
  ALB DNS name (which is dynamic + not in Django's allowed list).

Beads:
  rc-e5u.44.21 — this generator
  rc-e5u.44.18/.19 — the warnings that point at this fix
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


from .defaults import VPC_CIDR_DEFAULT as _VPC_CIDR_DEFAULT


@dataclass(frozen=True)
class Upstream:
    """One upstream service to be proxied through the generated nginx.

    ``name``     — compose service name; doubles as the location-block
                   server_name and the Cloud Map registration name.
    ``port``     — container port the upstream listens on.
    ``django``   — when True, inject ``proxy_set_header Host localhost;``
                   so Django's ALLOWED_HOSTS check passes.
    """

    name: str
    port: int
    django: bool = False


def _resolver_ip_for(vpc_cidr: Optional[str]) -> str:
    """Return the VPC's internal DNS resolver address (network base + 2).

    Mirrors the helper in compose_warnings._resolver_ip_for so that the
    generator and the warning text agree on the exact IP.
    """
    cidr = vpc_cidr or _VPC_CIDR_DEFAULT
    try:
        network = ipaddress.ip_network(cidr, strict=False)
        return str(network.network_address + 2)
    except (ValueError, TypeError):
        return "10.0.0.2"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


_DOCKERFILE_TEMPLATE = """\
# ECS-ready nginx (rc fix nginx-conf — rc-e5u.44.21).
#
# Sibling of compose/local/nginx/Dockerfile but COPYs the ECS-aware
# nginx.conf instead of the local one (resolver + variable-based
# proxy_pass survive Cloud Map task replacements). Wire into your
# compose ECS variant via:
#   services:
#     nginx:
#       build:
#         context: .
#         dockerfile: compose/ecs/nginx/Dockerfile
FROM nginx:1.25-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

COPY ./compose/ecs/nginx/nginx.conf /etc/nginx/nginx.conf

RUN apk add --no-cache curl

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
"""


_NGINX_HEADER_TEMPLATE = """\
# ECS-ready nginx config (rc fix nginx-conf — rc-e5u.44.21).
#
# Generated to proxy compose services via Cloud Map FQDNs with per-request
# DNS resolution. Three pieces matter and must stay in sync:
#   1. resolver <vpc_cidr_base+2> — the only DNS reachable from a Fargate
#      task ENI. NOT 169.254.169.253.
#   2. NO 'upstream {{ server X:Y; }}' blocks — stock nginx would cache the
#      resolution at config-load time and break on task replacement.
#   3. 'set $u "<svc>.<project>.local:<port>"; proxy_pass http://$u;' — the
#      variable form forces per-request resolution. FQDN is required because
#      nginx's resolver doesn't follow /etc/resolv.conf search domains.
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {{
    worker_connections 1024;
}}

http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    resolver {resolver_ip} valid=10s ipv6=off;

    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';
    access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;
    client_max_body_size 20M;

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml text/javascript
               application/json application/javascript application/xml+rss
               application/rss+xml font/truetype font/opentype
               application/vnd.ms-fontobject image/svg+xml;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_redirect off;
"""


_NGINX_DEFAULT_SERVER_TEMPLATE = """
    # Catch-all default_server — the ALB sends every request through Host =
    # the ALB DNS name, so without host-based listener rules wired up this
    # block answers everything. Proxies to the first declared upstream.
    server {{
        listen 80 default_server;
        server_name _;

        location / {{
            set $u "{fqdn}";
{django_host}            proxy_pass http://$u;
        }}
    }}
"""


_NGINX_NAMED_SERVER_TEMPLATE = """
    # {name} → {fqdn}
    server {{
        listen 80;
        server_name {name}.localhost;

        location / {{
            set $u "{fqdn}";
{django_host}            proxy_pass http://$u;
        }}
    }}
"""


_NGINX_FOOTER = "}\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_nginx_conf(
    upstreams: list[Upstream],
    project: str,
    vpc_cidr: Optional[str] = None,
) -> str:
    """Return the rendered nginx.conf as a string.

    The first upstream in the list becomes the catch-all default_server.
    Subsequent upstreams get named server blocks (server_name <name>.
    localhost) — purely for symmetry with the local compose conf; on ECS
    the ALB only ever lands traffic on the default unless host-routing
    rules are wired up separately.
    """
    if not upstreams:
        raise ValueError("render_nginx_conf: at least one upstream required")
    namespace = f"{project}.local" if project else "<project>.local"
    resolver_ip = _resolver_ip_for(vpc_cidr)

    parts = [_NGINX_HEADER_TEMPLATE.format(resolver_ip=resolver_ip)]

    primary = upstreams[0]
    parts.append(
        _NGINX_DEFAULT_SERVER_TEMPLATE.format(
            fqdn=f"{primary.name}.{namespace}:{primary.port}",
            django_host=_django_host_line(primary.django),
        )
    )
    for u in upstreams[1:]:
        parts.append(
            _NGINX_NAMED_SERVER_TEMPLATE.format(
                name=u.name,
                fqdn=f"{u.name}.{namespace}:{u.port}",
                django_host=_django_host_line(u.django),
            )
        )
    parts.append(_NGINX_FOOTER)
    return "".join(parts)


def render_dockerfile() -> str:
    """Return the rendered ECS-ready nginx Dockerfile as a string.

    Output path is a sibling of the compose ECS variant — we COPY the
    config from compose/ecs/nginx/nginx.conf which is the path the
    generator creates alongside this Dockerfile.
    """
    return _DOCKERFILE_TEMPLATE


def _django_host_line(is_django: bool) -> str:
    """Emit the proxy_set_header Host line (8-space indent) when needed."""
    if not is_django:
        return ""
    # Django's ALLOWED_HOSTS check rejects requests with the ALB DNS in the
    # Host header (returns 400 SuspiciousOperation). Local compose accepts
    # 'localhost' so we forward that. Alternative: DJANGO_ALLOWED_HOSTS=*.
    return "            proxy_set_header Host localhost;\n"


def write_ecs_nginx(
    project_dir: Path,
    upstreams: list[Upstream],
    project: str,
    vpc_cidr: Optional[str] = None,
    force: bool = False,
    output_subdir: str = "compose/ecs/nginx",
) -> tuple[Path, Path]:
    """Write nginx.conf + Dockerfile under ``<project_dir>/<output_subdir>``.

    Returns the absolute paths of (nginx.conf, Dockerfile). Refuses to
    overwrite existing files unless ``force`` is True — the user's
    expected workflow is "generate, inspect, edit", and clobbering a
    hand-tuned ECS conf on a re-run would be surprising.
    """
    out_dir = (project_dir / output_subdir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    nginx_path = out_dir / "nginx.conf"
    dockerfile_path = out_dir / "Dockerfile"

    for p in (nginx_path, dockerfile_path):
        if p.exists() and not force:
            raise FileExistsError(
                f"{p} already exists — re-run with --force to overwrite."
            )

    nginx_path.write_text(
        render_nginx_conf(upstreams, project=project, vpc_cidr=vpc_cidr)
    )
    dockerfile_path.write_text(render_dockerfile())
    return nginx_path, dockerfile_path


def upstreams_from_rc_v2(
    rc_v2_raw: dict,
    django_services: Optional[set[str]] = None,
) -> list[Upstream]:
    """Derive an Upstream list from rc.yml v2 services with public ports.

    Used as the fallback when the user runs ``rc fix nginx-conf`` without
    explicit ``--upstream`` flags. Includes any service with a numeric
    ``port`` set, marking ``django=True`` for names in ``django_services``
    (auto-detected from Dockerfile heuristics by the caller) so the
    generator knows to inject ``proxy_set_header Host localhost;``.
    """
    out: list[Upstream] = []
    services = (rc_v2_raw or {}).get("services") or {}
    if not isinstance(services, dict):
        return out
    djset = django_services or set()
    for name, cfg in services.items():
        if not isinstance(cfg, dict):
            continue
        # Skip the ALB front itself — it's the proxy, not an upstream.
        if name == "nginx":
            continue
        port = cfg.get("port")
        if not isinstance(port, int):
            continue
        out.append(Upstream(name=str(name), port=int(port), django=name in djset))
    return out


def parse_upstream_arg(spec: str, django_names: set[str]) -> Upstream:
    """Parse one ``--upstream`` CLI argument of the form ``name:port``.

    Examples:
      django:8000          → Upstream(name="django", port=8000, django=False)
      django:8000          + django_names={'django'} → django=True
    """
    if ":" not in spec:
        raise ValueError(f"--upstream {spec!r}: expected name:port")
    name, _, port_s = spec.partition(":")
    name = name.strip()
    if not name:
        raise ValueError(f"--upstream {spec!r}: missing service name")
    try:
        port = int(port_s)
    except ValueError as exc:
        raise ValueError(
            f"--upstream {spec!r}: port {port_s!r} is not an integer"
        ) from exc
    return Upstream(name=name, port=port, django=name in django_names)
