"""rc-562: detect_stale_nginx_resolver_ip warns when an nginx config has
a hardcoded `resolver <IP>` that doesn't match the rc.yml-declared VPC's
internal DNS address. Sentinal repro: nginx.conf had `resolver 10.42.0.2`
left over from a previous vpc_cidr=10.42 run; current rc.yml had
vpc_cidr=10.43 → Cloud Map lookups fail → 502.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


from remote_compose.compose_warnings import detect_stale_nginx_resolver_ip


def _scaffold(
    tmp_path: Path,
    nginx_conf_content: str,
    compose_yaml: str = None,
    nginx_path: str = "compose/ecs/nginx",
) -> Path:
    nginx_dir = tmp_path / nginx_path
    nginx_dir.mkdir(parents=True)
    (nginx_dir / "nginx.conf").write_text(nginx_conf_content)
    (nginx_dir / "Dockerfile").write_text(
        "FROM nginx:alpine\nCOPY nginx.conf /etc/nginx/nginx.conf\n"
    )
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(compose_yaml or textwrap.dedent(f"""
        services:
          nginx:
            build:
              context: {nginx_path}
              dockerfile: Dockerfile
            ports: ['80:80']
    """).strip())
    return compose


_GOOD_CONF_TPL = textwrap.dedent("""
    user nginx;
    worker_processes auto;
    events {{ worker_connections 1024; }}
    http {{
        resolver {ip} valid=10s ipv6=off;
        server {{
            listen 80;
            location / {{ proxy_pass http://upstream; }}
        }}
    }}
""")


class TestStaleResolverDetection:
    def test_warns_when_resolver_ip_doesnt_match_vpc_cidr(self, tmp_path):
        # nginx.conf has 10.42.0.2 (old VPC), rc.yml says vpc_cidr=10.43.0.0/16.
        compose = _scaffold(tmp_path, _GOOD_CONF_TPL.format(ip="10.42.0.2"))
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        rc_raw = {"provider_config": {"ecs": {"vpc_cidr": "10.43.0.0/16"}}}
        warnings = detect_stale_nginx_resolver_ip(compose_obj, compose, rc_raw)
        assert len(warnings) == 1
        w = warnings[0]
        assert "10.42.0.2" in w
        assert "10.43.0.2" in w  # expected
        assert "rc fix nginx-conf" in w

    def test_no_warning_when_resolver_matches(self, tmp_path):
        compose = _scaffold(tmp_path, _GOOD_CONF_TPL.format(ip="10.43.0.2"))
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        rc_raw = {"provider_config": {"ecs": {"vpc_cidr": "10.43.0.0/16"}}}
        warnings = detect_stale_nginx_resolver_ip(compose_obj, compose, rc_raw)
        assert warnings == []

    def test_no_warning_when_no_resolver_directive(self, tmp_path):
        # nginx.conf without a resolver line → not our concern.
        compose = _scaffold(
            tmp_path,
            "events { worker_connections 1024; }\nhttp { server { listen 80; } }\n",
        )
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        rc_raw = {"provider_config": {"ecs": {"vpc_cidr": "10.43.0.0/16"}}}
        warnings = detect_stale_nginx_resolver_ip(compose_obj, compose, rc_raw)
        assert warnings == []

    def test_default_vpc_cidr_when_unset(self, tmp_path):
        # No vpc_cidr in rc.yml → expects the default's resolver IP.
        from remote_compose.defaults import VPC_CIDR_DEFAULT
        import ipaddress

        default_ip = str(
            ipaddress.ip_network(VPC_CIDR_DEFAULT, strict=False).network_address + 2
        )
        # Use a CLEARLY wrong IP (not the default and not a typical octet).
        compose = _scaffold(tmp_path, _GOOD_CONF_TPL.format(ip="172.31.0.2"))
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        rc_raw = {}  # no vpc_cidr → uses default
        warnings = detect_stale_nginx_resolver_ip(compose_obj, compose, rc_raw)
        if "172.31.0.2" == default_ip:
            # If the default happens to be 172.31.0.2 there's no mismatch.
            # Use a different IP for the test.
            assert warnings == []
        else:
            assert len(warnings) == 1
            assert default_ip in warnings[0]

    def test_multiple_resolvers_only_unique_ones_flagged(self, tmp_path):
        # Two resolver directives with the SAME wrong IP → one warning.
        conf = textwrap.dedent("""
            events { worker_connections 1024; }
            http {
                resolver 10.42.0.2 valid=10s;
                server {
                    listen 80;
                    resolver 10.42.0.2;
                    location / { proxy_pass http://x; }
                }
            }
        """)
        compose = _scaffold(tmp_path, conf)
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        rc_raw = {"provider_config": {"ecs": {"vpc_cidr": "10.43.0.0/16"}}}
        warnings = detect_stale_nginx_resolver_ip(compose_obj, compose, rc_raw)
        assert len(warnings) == 1
