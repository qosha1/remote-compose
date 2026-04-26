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
    detect_external_volumes,
    detect_multi_port_alb,
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
                "db": {"image": "postgres", "volumes": ["pgdata:/var/lib/postgresql/data"]},
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
                "db": {"image": "postgres", "volumes": ["foo:/var/lib/postgresql/data"]},
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
                "db": {"image": "postgres", "volumes": ["foo:/var/lib/postgresql/data"]},
            },
            "volumes": {"foo": {"external": True}},
        }
        # User wired EFS in rc.yml — no warning expected.
        rc_v2 = {
            "services": {
                "db": {
                    "cpu": 256, "memory": 512,
                    "volumes": [{"name": "foo", "container_path": "/var/lib/postgresql/data"}],
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
        _write_compose(compose_path, {
            "services": {
                "nginx": {"build": {"context": "./nginx"}, "image": "nginx"},
            },
        })
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
        _write_compose(compose_path, {
            "services": {
                "nginx": {"build": {"context": "./nginx"}},
            },
        })
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_bad_hosts(compose, compose_path) == []

    def test_no_build_context_skips_scan(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(compose_path, {
            "services": {
                "api": {"image": "busybox"},  # no build:
            },
        })
        compose = yaml.safe_load(compose_path.read_text())
        assert detect_bad_hosts(compose, compose_path) == []

    def test_nested_conf_files_scanned(self, tmp_path):
        ctx = tmp_path / "build"
        (ctx / "deeply" / "nested").mkdir(parents=True)
        (ctx / "deeply" / "nested" / "service.conf").write_text(
            "upstream { server host.docker.internal:5000; }\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(compose_path, {
            "services": {
                "app": {"build": {"context": "./build"}},
            },
        })
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
# Aggregator + integration with rc plan output
# ---------------------------------------------------------------------------


class TestCollectComposeWarnings:
    def test_aggregates_all_four_detectors(self, tmp_path):
        ctx_dir = tmp_path / "build"
        ctx_dir.mkdir()
        (ctx_dir / "nginx.conf").write_text(
            "server host.docker.internal:3000;\n"
        )
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(compose_path, {
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
        })
        rc_v2 = {"services": {"db": {"cpu": 256, "memory": 512}}}
        warns = collect_compose_warnings(compose_path, rc_v2)
        # All four should fire on this fixture.
        assert any("bind mount" in w for w in warns)
        assert any("data will NOT persist" in w for w in warns)
        assert any("host.docker.internal" in w for w in warns)
        assert any("only" in w and "reachable via the ALB" in w for w in warns)

    def test_clean_compose_emits_no_warnings(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        _write_compose(compose_path, {
            "services": {
                "api": {"image": "busybox", "ports": ["3000:3000"]},
            },
        })
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
        rc.write_text(yaml.safe_dump({
            "version": 2, "project": "p",
            "compose_file": "docker-compose.yml",
            "provider": "fake",
            "services": {},
            "terraform": {"backend": {"type": "local"}},
        }))
        return rc

    def test_bind_mount_warning_appears_in_plan_output(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2
        rc = self._setup(tmp_path, {
            "services": {
                "api": {"image": "busybox", "volumes": ["./src:/app"]},
            },
        })
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "'api'" in out
        assert "./src:/app" in out

    def test_no_warnings_section_when_clean(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2
        rc = self._setup(tmp_path, {
            "services": {"api": {"image": "busybox"}},
        })
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
        rc = self._setup(tmp_path, {
            "services": {
                "nginx": {"build": {"context": "./nginx"}},
            },
        })
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "host.docker.internal" in out
        assert "nginx.conf" in out

    def test_multi_port_warning_appears_in_plan(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2
        rc = self._setup(tmp_path, {
            "services": {
                "app": {"image": "busybox", "ports": ["3000:3000", "6080:6080"]},
            },
        })
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "'app'" in out
        assert "6080" in out

    def test_external_volume_warning_appears_in_plan(self, tmp_path, capsys):
        from remote_compose.cli_v2 import dispatch_if_v2
        rc = self._setup(tmp_path, {
            "services": {
                "db": {"image": "postgres", "volumes": ["foo:/data"]},
            },
            "volumes": {"foo": {"external": True}},
        })
        ok = dispatch_if_v2(rc, "plan")
        assert ok is True
        out = capsys.readouterr().out
        assert "Warnings:" in out
        assert "'db'" in out
        assert "'foo'" in out
        assert "data will NOT persist" in out
