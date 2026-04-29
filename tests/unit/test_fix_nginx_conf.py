"""Unit tests for ``rc fix nginx-conf`` (rc-e5u.44.21).

Covers the pure-template rendering, the rc.yml-driven upstream derivation,
and the click-command file emission. The reference output is the manual
nginx.conf hand-written for rc-test-startsimpli on 2026-04-26 — anything
the generator produces should be substantively equivalent (resolver IP +
NO upstream blocks + variable-based proxy_pass + Django Host rewrite when
flagged).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.fix_nginx_conf import (
    Upstream,
    parse_upstream_arg,
    render_dockerfile,
    render_nginx_conf,
    upstreams_from_rc_v2,
    write_ecs_nginx,
)


# ---------------------------------------------------------------------------
# Pure-template rendering
# ---------------------------------------------------------------------------


class TestRenderNginxConf:
    def test_includes_resolver_directive_at_http_level(self):
        out = render_nginx_conf(
            [Upstream("django", 8000)], project="myapp", vpc_cidr="10.42.0.0/16",
        )
        # The .2 of the VPC CIDR; the only resolver reachable from a
        # Fargate ENI.
        assert "resolver 10.42.0.2 valid=10s ipv6=off" in out

    def test_default_vpc_cidr_falls_back_to_10_dot_0_dot_0_dot_2(self):
        out = render_nginx_conf([Upstream("django", 8000)], project="myapp")
        assert "resolver 10.0.0.2 valid=10s ipv6=off" in out

    def test_no_upstream_blocks_emitted(self):
        # The whole point: stock 'upstream { server X:Y; }' caches DNS at
        # config-load and breaks on Cloud Map task replacement. The
        # generator must NOT emit an `upstream NAME { ... }` directive.
        # (Comments mentioning the word "upstream" are fine.)
        out = render_nginx_conf(
            [Upstream("django", 8000)], project="myapp", vpc_cidr="10.42.0.0/16",
        )
        # The literal "upstream X {" syntax must not appear.
        import re
        assert re.search(r"^\s*upstream\s+\S+\s*\{", out, re.MULTILINE) is None
        # And no `server django:8000;` outside server{} blocks
        # (the upstream-block antipattern).
        assert "server django:8000" not in out

    def test_proxy_pass_uses_variable_form_with_fqdn(self):
        out = render_nginx_conf(
            [Upstream("django", 8000)], project="myapp", vpc_cidr="10.42.0.0/16",
        )
        # FQDN form because nginx's resolver doesn't follow the
        # /etc/resolv.conf search domain.
        assert 'set $u "django.myapp.local:8000"' in out
        assert "proxy_pass http://$u" in out

    def test_django_upstream_gets_host_localhost_header(self):
        out = render_nginx_conf(
            [Upstream("django", 8000, django=True)],
            project="myapp",
            vpc_cidr="10.42.0.0/16",
        )
        # Django's ALLOWED_HOSTS check rejects ALB DNS Host headers.
        assert "proxy_set_header Host localhost;" in out

    def test_non_django_upstream_no_host_localhost(self):
        out = render_nginx_conf(
            [Upstream("api", 5000, django=False)],
            project="myapp",
        )
        assert "proxy_set_header Host localhost" not in out

    def test_first_upstream_is_default_server(self):
        out = render_nginx_conf(
            [
                Upstream("django", 8000, django=True),
                Upstream("api", 5000),
            ],
            project="myapp",
        )
        # The catch-all default_server should land on the first upstream.
        assert "listen 80 default_server" in out
        # The named block for `api` lives at server_name api.localhost.
        assert "server_name api.localhost" in out

    def test_multiple_upstreams_each_get_own_server_block(self):
        out = render_nginx_conf(
            [
                Upstream("django", 8000, django=True),
                Upstream("api", 5000),
                Upstream("worker", 6379),
            ],
            project="x",
        )
        # default_server + 2 named blocks = 3 'server {' opens.
        assert out.count("server {") == 3

    def test_empty_upstream_list_raises(self):
        with pytest.raises(ValueError):
            render_nginx_conf([], project="x")

    def test_unset_project_uses_placeholder(self):
        # The user can run `rc fix nginx-conf` without a `project:` set
        # in rc.yml; we still emit valid syntax with a placeholder so the
        # output isn't broken — but the user has to fix it before deploy.
        out = render_nginx_conf([Upstream("django", 8000)], project="")
        assert "<project>.local" in out


# ---------------------------------------------------------------------------
# Dockerfile rendering
# ---------------------------------------------------------------------------


class TestRenderDockerfile:
    def test_starts_from_nginx_alpine(self):
        out = render_dockerfile()
        assert "FROM nginx:" in out

    def test_copies_ecs_nginx_conf(self):
        # Mirrors the manual reference at compose/ecs/nginx/Dockerfile —
        # COPYs the ECS-aware variant, NOT compose/local/nginx/nginx.conf.
        out = render_dockerfile()
        assert "COPY ./compose/ecs/nginx/nginx.conf /etc/nginx/nginx.conf" in out
        # Critically: no COPY/RUN line references the local conf.
        for line in out.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            assert "compose/local/nginx" not in line


# ---------------------------------------------------------------------------
# upstreams_from_rc_v2 — fallback when --upstream is not passed
# ---------------------------------------------------------------------------


class TestUpstreamsFromRcV2:
    def test_picks_services_with_numeric_port(self):
        rc = {
            "services": {
                "django": {"port": 8000, "public": True},
                "worker": {"type": "worker"},  # no port → skip
                "redis": {"port": 6379},
            },
        }
        out = upstreams_from_rc_v2(rc)
        names = {u.name for u in out}
        assert names == {"django", "redis"}

    def test_skips_nginx_service(self):
        # The nginx front itself isn't an upstream — it IS the proxy.
        rc = {
            "services": {
                "nginx": {"port": 80, "public": True},
                "django": {"port": 8000, "public": True},
            },
        }
        out = upstreams_from_rc_v2(rc)
        assert {u.name for u in out} == {"django"}

    def test_marks_django_services(self):
        rc = {"services": {"django": {"port": 8000}}}
        out = upstreams_from_rc_v2(rc, django_services={"django"})
        assert len(out) == 1
        assert out[0].django is True

    def test_empty_rc_returns_empty(self):
        assert upstreams_from_rc_v2({}) == []


# ---------------------------------------------------------------------------
# parse_upstream_arg — CLI parsing
# ---------------------------------------------------------------------------


class TestParseUpstreamArg:
    def test_basic_name_port(self):
        u = parse_upstream_arg("django:8000", django_names=set())
        assert u == Upstream(name="django", port=8000, django=False)

    def test_django_flag_when_name_in_set(self):
        u = parse_upstream_arg("django:8000", django_names={"django"})
        assert u.django is True

    def test_django_flag_off_when_name_not_in_set(self):
        u = parse_upstream_arg("api:5000", django_names={"django"})
        assert u.django is False

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError, match="expected name:port"):
            parse_upstream_arg("django8000", django_names=set())

    def test_non_integer_port_raises(self):
        with pytest.raises(ValueError, match="not an integer"):
            parse_upstream_arg("django:eighty", django_names=set())

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="missing service name"):
            parse_upstream_arg(":8000", django_names=set())


# ---------------------------------------------------------------------------
# write_ecs_nginx — file emission
# ---------------------------------------------------------------------------


class TestWriteEcsNginx:
    def test_writes_both_files_under_default_subdir(self, tmp_path):
        nginx_path, dockerfile_path = write_ecs_nginx(
            project_dir=tmp_path,
            upstreams=[Upstream("django", 8000, django=True)],
            project="myapp",
            vpc_cidr="10.42.0.0/16",
        )
        assert nginx_path == (tmp_path / "compose/ecs/nginx/nginx.conf").resolve()
        assert dockerfile_path == (tmp_path / "compose/ecs/nginx/Dockerfile").resolve()
        assert nginx_path.is_file()
        assert dockerfile_path.is_file()
        # Spot-check: the resolver IP made it into the file.
        assert "10.42.0.2" in nginx_path.read_text()

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        out_dir = tmp_path / "compose/ecs/nginx"
        out_dir.mkdir(parents=True)
        (out_dir / "nginx.conf").write_text("# preserve me\n")
        with pytest.raises(FileExistsError):
            write_ecs_nginx(
                project_dir=tmp_path,
                upstreams=[Upstream("django", 8000)],
                project="myapp",
            )
        # Original content is preserved.
        assert (out_dir / "nginx.conf").read_text() == "# preserve me\n"

    def test_force_overwrites(self, tmp_path):
        out_dir = tmp_path / "compose/ecs/nginx"
        out_dir.mkdir(parents=True)
        (out_dir / "nginx.conf").write_text("# old\n")
        nginx_path, _ = write_ecs_nginx(
            project_dir=tmp_path,
            upstreams=[Upstream("django", 8000)],
            project="myapp",
            force=True,
        )
        assert "# old" not in nginx_path.read_text()
        assert "resolver" in nginx_path.read_text()

    def test_custom_output_subdir(self, tmp_path):
        nginx_path, _ = write_ecs_nginx(
            project_dir=tmp_path,
            upstreams=[Upstream("django", 8000)],
            project="myapp",
            output_subdir="docker/nginx-ecs",
        )
        assert nginx_path == (tmp_path / "docker/nginx-ecs/nginx.conf").resolve()


# ---------------------------------------------------------------------------
# rc fix nginx-conf — click command end-to-end
# ---------------------------------------------------------------------------


class TestFixNginxConfCommand:
    def _write_rc(self, tmp_path: Path, **rc_extras) -> Path:
        rc = tmp_path / "rc.yml"
        doc = {
            "version": 2,
            "project": "myapp",
            "compose_file": "docker-compose.yml",
            "provider": "ecs",
            "provider_config": {
                "ecs": {"region": "us-west-2", "vpc_cidr": "10.42.0.0/16"},
            },
            "services": {},
            "terraform": {"backend": {"type": "local"}},
        }
        doc.update(rc_extras)
        rc.write_text(yaml.safe_dump(doc))
        return rc

    def test_explicit_upstream_with_django(self, tmp_path):
        rc = self._write_rc(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(rc), "fix", "nginx-conf",
             "--upstream", "django:8000", "--django", "django"],
        )
        assert result.exit_code == 0, result.output
        nginx_path = tmp_path / "compose/ecs/nginx/nginx.conf"
        dockerfile_path = tmp_path / "compose/ecs/nginx/Dockerfile"
        assert nginx_path.is_file()
        assert dockerfile_path.is_file()
        content = nginx_path.read_text()
        assert "resolver 10.42.0.2" in content
        assert 'set $u "django.myapp.local:8000"' in content
        assert "proxy_set_header Host localhost" in content
        # Output guides the user on next step.
        assert "Wire it into your compose" in result.output

    def test_falls_back_to_rc_yml_services(self, tmp_path):
        # No --upstream → use rc.yml services with port set.
        rc = self._write_rc(tmp_path, services={
            "django": {"port": 8000, "public": True},
            "redis": {"port": 6379},
        })
        runner = CliRunner()
        result = runner.invoke(
            cli, ["-c", str(rc), "fix", "nginx-conf"],
        )
        assert result.exit_code == 0, result.output
        content = (tmp_path / "compose/ecs/nginx/nginx.conf").read_text()
        # Both services appear as upstreams in the generated conf.
        assert 'django.myapp.local:8000' in content
        assert 'redis.myapp.local:6379' in content

    def test_no_upstreams_anywhere_errors(self, tmp_path):
        rc = self._write_rc(tmp_path, services={})
        runner = CliRunner()
        result = runner.invoke(
            cli, ["-c", str(rc), "fix", "nginx-conf"],
        )
        assert result.exit_code != 0
        assert "no --upstream" in result.output

    def test_missing_rc_yml_errors(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(tmp_path / "missing.yml"), "fix", "nginx-conf",
             "--upstream", "django:8000"],
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        rc = self._write_rc(tmp_path)
        out_dir = tmp_path / "compose/ecs/nginx"
        out_dir.mkdir(parents=True)
        (out_dir / "nginx.conf").write_text("# preserve me\n")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(rc), "fix", "nginx-conf",
             "--upstream", "django:8000"],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output
        # Run with --force succeeds.
        result = runner.invoke(
            cli,
            ["-c", str(rc), "fix", "nginx-conf",
             "--upstream", "django:8000", "--force"],
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Reference-shape comparison vs the manual rc-test-startsimpli conf
# ---------------------------------------------------------------------------


class TestReferenceShape:
    """The hand-tuned 2026-04-26 nginx.conf at
    /Users/qosha/Repos/start-simpli/start-simpli-api/compose/ecs/nginx/nginx.conf
    is the verified-working baseline. Anything the generator produces for
    the same inputs should share its load-bearing ingredients.
    """

    def test_generator_output_has_same_load_bearing_pieces(self):
        # rc-test-startsimpli's vpc_cidr is 10.42.0.0/16; primary upstream
        # is django:8000 (Django).
        out = render_nginx_conf(
            [Upstream("django", 8000, django=True)],
            project="rc-test-startsimpli",
            vpc_cidr="10.42.0.0/16",
        )
        # Three load-bearing ingredients of the manual file.
        assert "resolver 10.42.0.2 valid=10s ipv6=off" in out
        assert 'set $u "django.rc-test-startsimpli.local:8000"' in out
        assert "proxy_pass http://$u" in out
        assert "proxy_set_header Host localhost" in out
        # And the anti-pattern is absent.
        assert "upstream django {" not in out
