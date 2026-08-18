"""Unit tests for the compose-warning detectors surfaced by ``rc plan``.

Each detector has a triggering fixture and a false-positive control. The
exact substrings asserted on are the ones quoted in the bead acceptance
criteria (rc-e5u.44.6/.7/.8/.9).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from remote_compose.compose_warnings import (
    collect_compose_warnings,
    detect_bad_hosts,
    detect_bind_mounts,
    detect_django_allowed_hosts,
    detect_external_volumes,
    detect_multi_port_alb,
    detect_nginx_upstream_resolver,
    detect_partially_wired_shared_volume,
)


def _write_compose(path: Path, doc: dict) -> Path:
    path.write_text(yaml.safe_dump(doc))
    return path


# ---------------------------------------------------------------------------
# rc-e5u.44.6 — bind-mount detector
# ---------------------------------------------------------------------------


class TestBindMountDetector:
    def test_short_bind_mount_warns_with_service_and_path(self):
        compose = {
            "services": {
                "api": {"image": "busybox", "volumes": ["./src:/app"]},
            },
        }
        warns = detect_bind_mounts(compose)
        assert len(warns) == 1
        # Acceptance: warning must name the service 'api' and the bind
        # path './src:/app'.
        assert "'api'" in warns[0]
        assert "./src:/app" in warns[0]
        assert "image must already contain" in warns[0]

    def test_long_form_bind_mount_warns(self):
        compose = {
            "services": {
                "api": {
                    "image": "busybox",
                    "volumes": [
                        {"type": "bind", "source": "./src", "target": "/app"},
                    ],
                },
            },
        }
        warns = detect_bind_mounts(compose)
        assert any("'api'" in w and "./src:/app" in w for w in warns)

    def test_named_volume_does_not_trigger_bind_warning(self):
        compose = {
            "services": {
                "db": {
                    "image": "postgres",
                    "volumes": ["pgdata:/var/lib/postgresql/data"],
                },
            },
            "volumes": {"pgdata": {}},
        }
        assert detect_bind_mounts(compose) == []

    def test_dedupes_per_service_mount_pair(self):
        compose = {
            "services": {
                "api": {
                    "image": "busybox",
                    # Same mount listed twice — must warn once.
                    "volumes": ["./src:/app", "./src:/app"],
                },
            },
        }
        assert len(detect_bind_mounts(compose)) == 1

    def test_anonymous_volume_does_not_trigger(self):
        compose = {
            "services": {
                "api": {"image": "busybox", "volumes": ["/data"]},
            },
        }
        assert detect_bind_mounts(compose) == []


# ---------------------------------------------------------------------------
# rc-e5u.44.7 — external named volume detector
# ---------------------------------------------------------------------------


class TestExternalVolumeDetector:
    def test_external_volume_mounted_without_rc_coverage_warns(self):
        compose = {
            "services": {
                "db": {
                    "image": "postgres",
                    "volumes": ["foo:/var/lib/postgresql/data"],
                },
            },
            "volumes": {"foo": {"external": True}},
        }
        rc_v2 = {"services": {"db": {"cpu": 256, "memory": 512}}}
        warns = detect_external_volumes(compose, rc_v2)
        assert len(warns) == 1
        # Acceptance: warning names both 'db' and 'foo' and states data
        # will not persist across task restarts.
        assert "'db'" in warns[0]
        assert "'foo'" in warns[0]
        assert "data will NOT persist" in warns[0]

    def test_rc_yml_volumes_suppresses_warning(self):
        compose = {
            "services": {
                "db": {
                    "image": "postgres",
                    "volumes": ["foo:/var/lib/postgresql/data"],
                },
            },
            "volumes": {"foo": {"external": True}},
        }
        # User wired EFS in rc.yml — no warning expected.
        rc_v2 = {
            "services": {
                "db": {
                    "cpu": 256,
                    "memory": 512,
                    "volumes": [
                        {"name": "foo", "container_path": "/var/lib/postgresql/data"}
                    ],
                },
            },
        }
        assert detect_external_volumes(compose, rc_v2) == []

    def test_non_external_named_volume_does_not_warn(self):
        compose = {
            "services": {
                "db": {"image": "postgres", "volumes": ["foo:/data"]},
            },
            "volumes": {"foo": {}},  # not external
        }
        rc_v2 = {"services": {"db": {"cpu": 256, "memory": 512}}}
        assert detect_external_volumes(compose, rc_v2) == []


# ---------------------------------------------------------------------------
# rc-z0k.6 — partially-wired shared volume detector
# ---------------------------------------------------------------------------


class TestPartiallyWiredSharedVolumeDetector:
    def _compose(self, django_command=None):
        django_svc = {"image": "django-app", "volumes": ["media:/media"]}
        if django_command is not None:
            django_svc["command"] = django_command
        return {
            "services": {
                "django": django_svc,
                "celerybeat": {
                    "image": "django-app",
                    "command": ["celery", "-A", "proj", "beat"],
                    "volumes": ["media:/media"],
                },
            },
        }

    def test_singleton_wired_other_service_unwired_warns(self):
        """The browser-mgr repro shape: celerybeat (singleton, no explicit
        rc.yml volumes) auto-promotes; django (not a singleton, no explicit
        rc.yml volumes) gets nothing, despite compose making 'media' look
        shared by both."""
        compose = self._compose()
        rc_v2 = {
            "services": {
                "django": {"cpu": 512, "memory": 1024},
                "celerybeat": {"cpu": 256, "memory": 512},
            }
        }
        warns = detect_partially_wired_shared_volume(compose, rc_v2)
        assert len(warns) == 1
        assert "'media'" in warns[0]
        assert "['celerybeat']" in warns[0]
        assert "['django']" in warns[0]
        assert "DESTROY" in warns[0]

    def test_both_explicitly_wired_in_rc_yml_does_not_warn(self):
        compose = self._compose()
        rc_v2 = {
            "services": {
                "django": {
                    "cpu": 512,
                    "memory": 1024,
                    "volumes": [{"name": "media", "mount": "/media"}],
                },
                "celerybeat": {
                    "cpu": 256,
                    "memory": 512,
                    "volumes": [{"name": "media", "mount": "/media"}],
                },
            }
        }
        assert detect_partially_wired_shared_volume(compose, rc_v2) == []

    def test_solo_mount_does_not_warn(self):
        """Only one service mounts the volume -- nothing to be silently
        inconsistent about."""
        compose = {
            "services": {
                "celerybeat": {
                    "image": "django-app",
                    "command": ["celery", "-A", "proj", "beat"],
                    "volumes": ["media:/media"],
                },
            },
        }
        rc_v2 = {"services": {"celerybeat": {"cpu": 256, "memory": 512}}}
        assert detect_partially_wired_shared_volume(compose, rc_v2) == []

    def test_neither_wired_does_not_warn(self):
        """Two non-singleton services share a volume that neither has
        explicit rc.yml coverage for -- ephemeral for both equally, not the
        dangerous "looks safer than it is" shape this detector targets."""
        compose = {
            "services": {
                "web": {"image": "app", "volumes": ["cache:/cache"]},
                "worker": {"image": "app", "volumes": ["cache:/cache"]},
            },
        }
        rc_v2 = {
            "services": {
                "web": {"cpu": 256, "memory": 512},
                "worker": {"cpu": 256, "memory": 512},
            }
        }
        assert detect_partially_wired_shared_volume(compose, rc_v2) == []

    def test_django_has_unrelated_explicit_volume_still_unwired(self):
        """django has SOME explicit rc.yml volumes (a different volume),
        which disables singleton auto-promotion consideration for it (it's
        not a singleton anyway) but does NOT itself wire 'media' -- still
        counts as unwired since 'media' specifically isn't named."""
        compose = self._compose()
        rc_v2 = {
            "services": {
                "django": {
                    "cpu": 512,
                    "memory": 1024,
                    "volumes": [{"name": "other-vol", "mount": "/other"}],
                },
                "celerybeat": {"cpu": 256, "memory": 512},
            }
        }
        warns = detect_partially_wired_shared_volume(compose, rc_v2)
        assert len(warns) == 1
        assert "['django']" in warns[0]

    def test_explicit_rc_yml_volume_suppresses_auto_promotion_correctly(self):
        """celerybeat has ITS OWN explicit rc.yml volumes list -- disables
        auto-promotion for it (cli_v2.py's own gate). If that explicit list
        doesn't name 'media', celerybeat itself becomes unwired too, and
        with no wired service at all the detector stays silent (out of
        scope -- see test_neither_wired_does_not_warn)."""
        compose = self._compose()
        rc_v2 = {
            "services": {
                "django": {"cpu": 512, "memory": 1024},
                "celerybeat": {
                    "cpu": 256,
                    "memory": 512,
                    "volumes": [{"name": "other-vol", "mount": "/other"}],
                },
            }
        }
        assert detect_partially_wired_shared_volume(compose, rc_v2) == []


# ---------------------------------------------------------------------------
# rc-e5u.44.8 — host.docker.internal detector
# ---------------------------------------------------------------------------


class TestBadHostDetector:
    def test_nginx_conf_with_host_docker_internal_warns(self, tmp_path):
        ctx = tmp_path / "nginx"
        ctx.mkdir()
        nginx_conf = ctx / "nginx.conf"
        nginx_conf.write_text(
            "upstream landing { server host.docker.internal:3000; }\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "nginx": {"build": {"context": "./nginx"}, "image": "nginx"},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_bad_hosts(compose, compose_path)
        assert len(warns) == 1
        # Acceptance: warning names the file path and the offending host.
        assert "host.docker.internal" in warns[0]
        assert str(nginx_conf) in warns[0]
        assert "'nginx'" in warns[0]

    def test_localhost_does_not_trigger(self, tmp_path):
        ctx = tmp_path / "nginx"
        ctx.mkdir()
        (ctx / "nginx.conf").write_text(
            "upstream loop { server localhost:3000; }\n"
            "upstream loop2 { server 127.0.0.1:3000; }\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "nginx": {"build": {"context": "./nginx"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_bad_hosts(compose, compose_path) == []

    def test_no_build_context_skips_scan(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "api": {"image": "busybox"},  # no build:
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_bad_hosts(compose, compose_path) == []

    def test_nested_conf_files_scanned(self, tmp_path):
        ctx = tmp_path / "build"
        (ctx / "deeply" / "nested").mkdir(parents=True)
        (ctx / "deeply" / "nested" / "service.conf").write_text(
            "upstream { server host.docker.internal:5000; }\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "app": {"build": {"context": "./build"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_bad_hosts(compose, compose_path)
        assert any("host.docker.internal" in w and "service.conf" in w for w in warns)


# ---------------------------------------------------------------------------
# rc-e5u.44.9 — multi-port detector
# ---------------------------------------------------------------------------


class TestMultiPortDetector:
    def test_multi_port_service_warns(self):
        compose = {
            "services": {
                "app": {
                    "image": "busybox",
                    "ports": ["3000:3000", "6080:6080"],
                },
            },
        }
        warns = detect_multi_port_alb(compose, {})
        assert len(warns) == 1
        # Acceptance: warning names 'app' and the ports that won't reach
        # the ALB; mentions remediation (extra_listeners or compose.exclude).
        assert "'app'" in warns[0]
        assert "3000" in warns[0]
        assert "6080" in warns[0]
        assert "extra_listeners" in warns[0] or "compose.exclude" in warns[0]

    def test_single_port_service_does_not_warn(self):
        compose = {
            "services": {
                "app": {"image": "busybox", "ports": ["3000:3000"]},
            },
        }
        assert detect_multi_port_alb(compose, {}) == []

    def test_explicit_public_false_suppresses_warning(self):
        compose = {
            "services": {
                "vnc": {"image": "busybox", "ports": ["7788:7788", "5901:5901"]},
            },
        }
        rc_v2 = {"services": {"vnc": {"cpu": 256, "memory": 512, "public": False}}}
        assert detect_multi_port_alb(compose, rc_v2) == []

    def test_long_form_ports_counted(self):
        compose = {
            "services": {
                "app": {
                    "image": "busybox",
                    "ports": [
                        {"target": 3000, "published": 3000},
                        {"target": 6080, "published": 6080},
                    ],
                },
            },
        }
        warns = detect_multi_port_alb(compose, {})
        assert any("3000" in w and "6080" in w for w in warns)


# ---------------------------------------------------------------------------
# rc-e5u.44.18 — nginx upstream-resolver detector
# ---------------------------------------------------------------------------


class TestNginxUpstreamResolverDetector:
    def _setup(self, tmp_path, nginx_conf_text, services=None):
        services = services or {
            "nginx": {"build": {"context": "./build"}},
            "django": {"image": "django:latest"},
        }
        ctx_dir = tmp_path / "build"
        ctx_dir.mkdir()
        (ctx_dir / "nginx.conf").write_text(nginx_conf_text)
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(compose_path, {"services": services})
        return compose_path

    def test_warns_when_upstream_uses_compose_service_no_resolver(self, tmp_path):
        compose_path = self._setup(
            tmp_path,
            """\
http {
  upstream django {
    server django:8000;
  }
  server {
    listen 80;
    location / { proxy_pass http://django; }
  }
}
""",
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_nginx_upstream_resolver(compose, compose_path)
        assert len(warns) == 1
        w = warns[0]
        # The warning must name the offending file + the service + recommend
        # resolver + set $var pattern (per bead acceptance).
        assert "nginx.conf" in w
        assert "resolver" in w
        # No rc.yml passed → falls back to default vpc 10.0.0.0/16 → .2
        assert "resolver 10.0.0.2 valid=10s ipv6=off" in w
        assert "set $u" in w
        assert "proxy_pass http://$u" in w

    def test_suppressed_when_resolver_directive_present(self, tmp_path):
        compose_path = self._setup(
            tmp_path,
            """\
http {
  resolver 169.254.169.253 valid=10s;
  upstream django { server django:8000; }
}
""",
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_nginx_upstream_resolver(compose, compose_path) == []

    def test_external_hostname_not_flagged(self, tmp_path):
        # api.example.com is NOT a compose service — out of scope.
        compose_path = self._setup(
            tmp_path,
            """\
upstream api { server api.example.com:443; }
""",
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_nginx_upstream_resolver(compose, compose_path) == []

    def test_localhost_and_127_not_flagged(self, tmp_path):
        compose_path = self._setup(
            tmp_path,
            """\
upstream local1 { server localhost:8080; }
upstream local2 { server 127.0.0.1:9090; }
""",
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_nginx_upstream_resolver(compose, compose_path) == []

    def test_multiple_upstreams_each_warn_once(self, tmp_path):
        # Multiple services in compose, multiple upstream blocks
        compose_path = self._setup(
            tmp_path,
            """\
upstream django { server django:8000; }
upstream worker { server celery:5555; }
""",
            services={
                "nginx": {"build": {"context": "./build"}},
                "django": {"image": "x"},
                "celery": {"image": "x"},
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_nginx_upstream_resolver(compose, compose_path)
        assert len(warns) == 2

    # ----- rc-e5u.44.19: vpc-derived resolver, FQDN, Django Host hint -----

    def test_resolver_ip_derived_from_rc_yml_vpc_cidr(self, tmp_path):
        # vpc_cidr 10.42.0.0/16 → resolver 10.42.0.2 (network base + 2).
        compose_path = self._setup(tmp_path, "upstream django { server django:8000; }")
        compose = yaml.safe_load(compose_path.read_text())
        rc = {
            "project": "myapp",
            "provider_config": {"ecs": {"vpc_cidr": "10.42.0.0/16"}},
        }
        warns = detect_nginx_upstream_resolver(compose, compose_path, rc)
        assert len(warns) == 1
        assert "resolver 10.42.0.2" in warns[0]
        assert "10.0.0.2" not in warns[0]  # not the default fallback

    def test_resolver_ip_handles_other_cidrs(self, tmp_path):
        compose_path = self._setup(tmp_path, "upstream django { server django:8000; }")
        compose = yaml.safe_load(compose_path.read_text())
        rc = {"project": "x", "provider_config": {"ecs": {"vpc_cidr": "172.31.0.0/16"}}}
        warns = detect_nginx_upstream_resolver(compose, compose_path, rc)
        assert "resolver 172.31.0.2" in warns[0]

    def test_malformed_cidr_falls_back_to_default(self, tmp_path):
        compose_path = self._setup(tmp_path, "upstream django { server django:8000; }")
        compose = yaml.safe_load(compose_path.read_text())
        rc = {"project": "x", "provider_config": {"ecs": {"vpc_cidr": "not-a-cidr"}}}
        warns = detect_nginx_upstream_resolver(compose, compose_path, rc)
        assert "resolver 10.0.0.2" in warns[0]

    def test_uses_fqdn_in_recommended_set_u_directive(self, tmp_path):
        # set $u must use <host>.<project>.local:<port> — bare host returns
        # NXDOMAIN through nginx's resolver (doesn't follow search domain).
        compose_path = self._setup(tmp_path, "upstream django { server django:8000; }")
        compose = yaml.safe_load(compose_path.read_text())
        rc = {"project": "myapp", "provider_config": {"ecs": {}}}
        warns = detect_nginx_upstream_resolver(compose, compose_path, rc)
        assert 'set $u "django.myapp.local:8000"' in warns[0]
        # Must NOT recommend the bare form (the bug we just fixed)
        assert 'set $u "django:8000"' not in warns[0]

    def test_django_upstream_includes_allowed_hosts_hint(self, tmp_path):
        # Heuristic: Dockerfile has manage.py / wsgi.py / django dep → mention
        # ALLOWED_HOSTS + the Host header rewrite.
        ctx = tmp_path / "build"
        ctx.mkdir()
        django_ctx = tmp_path / "djbuild"
        django_ctx.mkdir()
        (django_ctx / "Dockerfile").write_text(
            "FROM python:3.12\nCOPY manage.py /app/\nRUN pip install django>=4.2\n"
        )
        (ctx / "nginx.conf").write_text("upstream django { server django:8000; }")
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "nginx": {"build": {"context": "./build"}},
                    "django": {"build": {"context": "./djbuild"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        rc = {"project": "myapp", "provider_config": {"ecs": {}}}
        warns = detect_nginx_upstream_resolver(compose, compose_path, rc)
        assert len(warns) == 1
        w = warns[0]
        assert "Django" in w
        assert "ALLOWED_HOSTS" in w
        assert "proxy_set_header Host localhost" in w

    def test_non_django_upstream_no_django_hint(self, tmp_path):
        # nginx in front of a stock redis / postgres / non-Python upstream
        # shouldn't get the Django ALLOWED_HOSTS noise.
        compose_path = self._setup(
            tmp_path,
            "upstream cache { server redis:6379; }",
            services={
                "nginx": {"build": {"context": "./build"}},
                "redis": {"image": "redis:7-alpine"},  # no build/Dockerfile
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        rc = {"project": "myapp", "provider_config": {"ecs": {}}}
        warns = detect_nginx_upstream_resolver(compose, compose_path, rc)
        assert len(warns) == 1
        assert "Django" not in warns[0]
        assert "ALLOWED_HOSTS" not in warns[0]

    def test_dedupe_when_same_pattern_appears_twice(self, tmp_path):
        # Same `server django:8000;` line twice in different upstream blocks
        # but identical (svc, file, upstream:host:port) triple — dedupes.
        compose_path = self._setup(
            tmp_path,
            """\
upstream a { server django:8000; }
upstream b { server django:8000; }
""",
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_nginx_upstream_resolver(compose, compose_path)
        # Two distinct upstream names → two warnings.
        assert len(warns) == 2
        # But re-running the detector doesn't produce more.
        warns2 = detect_nginx_upstream_resolver(compose, compose_path)
        assert warns2 == warns

    def test_no_build_context_no_warning(self, tmp_path):
        # nginx uses an image: ref (no build context) — nothing to scan.
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "nginx": {"image": "nginx:alpine"},
                    "django": {"image": "django:latest"},
                }
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_nginx_upstream_resolver(compose, compose_path) == []


# ---------------------------------------------------------------------------
# rc-e5u.44.23 — Django ALLOWED_HOSTS proactive detector
# ---------------------------------------------------------------------------


class TestDjangoAllowedHostsDetector:
    """One warning per Django-shaped service deployed via ALB.

    The nginx detector (.44.19) already includes a Django hint when it
    finds an nginx upstream pointing at a Django service. THIS detector
    fires even when there's no nginx — the bare Django-on-ECS case hits
    the same ALLOWED_HOSTS rejection.
    """

    def _write_django_dockerfile(self, ctx: Path) -> None:
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "Dockerfile").write_text(
            "FROM python:3.12\n"
            "COPY manage.py /app/\n"
            "RUN pip install django>=4.2\n"
            "CMD python manage.py runserver 0.0.0.0:8000\n"
        )

    def test_django_service_warns(self, tmp_path):
        ctx = tmp_path / "djbuild"
        self._write_django_dockerfile(ctx)
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "django": {"build": {"context": "./djbuild"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_django_allowed_hosts(compose, compose_path, {})
        assert len(warns) == 1
        w = warns[0]
        assert "'django'" in w
        assert "ALLOWED_HOSTS" in w
        assert "DJANGO_ALLOWED_HOSTS=*" in w
        assert "rc fix nginx-conf" in w
        assert "proxy_set_header Host localhost" in w

    def test_non_django_python_service_no_warning(self, tmp_path):
        # FastAPI / Flask / arbitrary Python — no Django markers, no warning.
        ctx = tmp_path / "fastbuild"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text(
            "FROM python:3.12\n"
            "RUN pip install fastapi uvicorn\n"
            "CMD uvicorn app:app --host 0.0.0.0\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "api": {"build": {"context": "./fastbuild"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_django_allowed_hosts(compose, compose_path, {}) == []

    def test_one_warning_per_service_not_per_upstream(self, tmp_path):
        # nginx + django + a second random service. Only one Django warning,
        # not "one per upstream".
        ctx = tmp_path / "djbuild"
        self._write_django_dockerfile(ctx)
        nginx_ctx = tmp_path / "nginx"
        nginx_ctx.mkdir()
        (nginx_ctx / "nginx.conf").write_text(
            "upstream a { server django:8000; }\n"
            "upstream b { server django:8000; }\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "django": {"build": {"context": "./djbuild"}},
                    "nginx": {"build": {"context": "./nginx"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_django_allowed_hosts(compose, compose_path, {})
        assert len(warns) == 1

    def test_image_only_service_not_flagged(self, tmp_path):
        # A compose 'image: django:latest' service has no Dockerfile to scan
        # — heuristic rightly skips it (we'd produce a false positive on
        # any tagged 'django' image otherwise).
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "django": {"image": "django:latest"},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_django_allowed_hosts(compose, compose_path, {}) == []

    def test_django_allowed_hosts_env_suppresses(self, tmp_path):
        # User already set DJANGO_ALLOWED_HOSTS in compose env — they're aware.
        ctx = tmp_path / "djbuild"
        self._write_django_dockerfile(ctx)
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "django": {
                        "build": {"context": "./djbuild"},
                        "environment": {"DJANGO_ALLOWED_HOSTS": "*"},
                    },
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_django_allowed_hosts(compose, compose_path, {}) == []

    def test_django_allowed_hosts_env_list_form_suppresses(self, tmp_path):
        # compose accepts `environment:` as a list of KEY=VALUE strings too.
        ctx = tmp_path / "djbuild"
        self._write_django_dockerfile(ctx)
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "django": {
                        "build": {"context": "./djbuild"},
                        "environment": ["DJANGO_ALLOWED_HOSTS=mydomain.com"],
                    },
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_django_allowed_hosts(compose, compose_path, {}) == []

    def test_rc_yml_env_override_suppresses(self, tmp_path):
        # rc.yml services.<svc>.env override also counts as "user is aware".
        ctx = tmp_path / "djbuild"
        self._write_django_dockerfile(ctx)
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "django": {"build": {"context": "./djbuild"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        rc = {"services": {"django": {"env": {"DJANGO_ALLOWED_HOSTS": "*"}}}}
        assert detect_django_allowed_hosts(compose, compose_path, rc) == []

    def test_two_django_services_two_warnings(self, tmp_path):
        ctx1 = tmp_path / "dj1"
        ctx2 = tmp_path / "dj2"
        self._write_django_dockerfile(ctx1)
        self._write_django_dockerfile(ctx2)
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "web": {"build": {"context": "./dj1"}},
                    "celery": {"build": {"context": "./dj2"}},
                },
            },
        )
        compose = yaml.safe_load(compose_path.read_text())
        warns = detect_django_allowed_hosts(compose, compose_path, {})
        assert len(warns) == 2
        # Each warning quotes the service name with single quotes.
        assert any("'web'" in w for w in warns)
        assert any("'celery'" in w for w in warns)


# ---------------------------------------------------------------------------
# Aggregator + integration with rc plan output
# ---------------------------------------------------------------------------


class TestCollectComposeWarnings:
    def test_aggregates_all_four_detectors(self, tmp_path):
        ctx_dir = tmp_path / "build"
        ctx_dir.mkdir()
        (ctx_dir / "nginx.conf").write_text("server host.docker.internal:3000;\n")
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "api": {
                        "image": "busybox",
                        "volumes": ["./src:/app"],
                        "ports": ["3000:3000", "6080:6080"],
                    },
                    "nginx": {
                        "build": {"context": "./build"},
                    },
                    "db": {
                        "image": "postgres",
                        "volumes": ["pgdata:/data"],
                    },
                },
                "volumes": {"pgdata": {"external": True}},
            },
        )
        rc_v2 = {"services": {"db": {"cpu": 256, "memory": 512}}}
        warns = collect_compose_warnings(compose_path, rc_v2)
        # All four should fire on this fixture.
        assert any("bind mount" in w for w in warns)
        assert any("data will NOT persist" in w for w in warns)
        assert any("host.docker.internal" in w for w in warns)
        assert any("only" in w and "reachable via the ALB" in w for w in warns)

    def test_clean_compose_emits_no_warnings(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(
            compose_path,
            {
                "services": {
                    "api": {"image": "busybox", "ports": ["3000:3000"]},
                },
            },
        )
        assert collect_compose_warnings(compose_path, {}) == []

    def test_missing_compose_file_returns_empty(self, tmp_path):
        assert collect_compose_warnings(tmp_path / "nope.yml", {}) == []


class TestRcPlanRendersWarnings:
    """End-to-end: the CLI dispatcher must surface compose warnings in
    'rc plan' output. Uses the fake provider so no terraform required."""

    def _setup(self, tmp_path, compose_doc):
        compose = tmp_path / "docker-compose.yml"
        _write_compose(compose, compose_doc)
        rc = tmp_path / "rc.yml"
        rc.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "project": "p",
                    "compose_file": "docker-compose.yml",
                    "provider": "fake",
                    "services": {},
                    "terraform": {"backend": {"type": "local"}},
                }
            )
        )
        return rc

    def test_bind_mount_warning_appears_in_plan_output(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2

        rc = self._setup(
            tmp_path,
            {
                "services": {
                    "api": {"image": "busybox", "volumes": ["./src:/app"]},
                },
            },
        )
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "'api'" in out
        assert "./src:/app" in out

    def test_no_warnings_section_when_clean(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2

        rc = self._setup(
            tmp_path,
            {
                "services": {"api": {"image": "busybox"}},
            },
        )
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" not in out

    def test_host_docker_internal_warning_appears_in_plan(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2

        ctx_dir = tmp_path / "nginx"
        ctx_dir.mkdir()
        (ctx_dir / "nginx.conf").write_text(
            "upstream { server host.docker.internal:3000; }\n"
        )
        rc = self._setup(
            tmp_path,
            {
                "services": {
                    "nginx": {"build": {"context": "./nginx"}},
                },
            },
        )
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "host.docker.internal" in out
        assert "nginx.conf" in out

    def test_multi_port_warning_appears_in_plan(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2

        rc = self._setup(
            tmp_path,
            {
                "services": {
                    "app": {"image": "busybox", "ports": ["3000:3000", "6080:6080"]},
                },
            },
        )
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "'app'" in out
        assert "6080" in out

    def test_external_volume_warning_appears_in_plan(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2

        rc = self._setup(
            tmp_path,
            {
                "services": {
                    "db": {"image": "postgres", "volumes": ["foo:/data"]},
                },
                "volumes": {"foo": {"external": True}},
            },
        )
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "'db'" in out
        assert "'foo'" in out
        assert "data will NOT persist" in out

    def test_django_allowed_hosts_warning_appears_in_plan(self, tmp_path, capsys):
        # rc-e5u.44.23: even without an nginx front, a Django-shaped service
        # gets a proactive ALLOWED_HOSTS heads-up during `rc plan`.
        from remote_compose.cli_v2 import dispatch_if_v2

        ctx_dir = tmp_path / "djbuild"
        ctx_dir.mkdir()
        (ctx_dir / "Dockerfile").write_text(
            "FROM python:3.12\nCOPY manage.py /app/\nRUN pip install django\n"
        )
        rc = self._setup(
            tmp_path,
            {
                "services": {
                    "django": {"build": {"context": "./djbuild"}},
                },
            },
        )
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "ALLOWED_HOSTS" in out
        assert "DJANGO_ALLOWED_HOSTS=*" in out
        assert "rc fix nginx-conf" in out
