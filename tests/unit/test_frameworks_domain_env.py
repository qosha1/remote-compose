"""rc-32x: Framework.domain_env() renders the per-framework env vars
that need to know the ALB-fronted hostname (DJANGO_ALLOWED_HOSTS,
CSRF_TRUSTED_ORIGINS, RAILS_HOSTS, PHX_HOST). Without this, every
`rc up --domain` deploy hits a CSRF / host-validation 403.
"""

from __future__ import annotations

import textwrap


from remote_compose.frameworks import DJANGO, RAILS, PHOENIX


class TestDjangoDomainEnv:
    def test_returns_empty_when_no_domain(self):
        assert DJANGO.domain_env("") == {}

    def test_single_domain(self):
        env = DJANGO.domain_env("app.example.com")
        assert env["DJANGO_ALLOWED_HOSTS"] == "app.example.com"
        assert env["CSRF_TRUSTED_ORIGINS"] == "https://app.example.com"

    def test_with_aliases(self):
        env = DJANGO.domain_env(
            "app.example.com",
            aliases=("www.example.com", "alt.example.com"),
        )
        assert env["DJANGO_ALLOWED_HOSTS"] == (
            "app.example.com,www.example.com,alt.example.com"
        )
        assert env["CSRF_TRUSTED_ORIGINS"] == (
            "https://app.example.com,https://www.example.com," "https://alt.example.com"
        )


class TestRailsDomainEnv:
    def test_single_domain(self):
        env = RAILS.domain_env("app.example.com")
        assert env["RAILS_HOSTS"] == "app.example.com"

    def test_with_aliases(self):
        env = RAILS.domain_env(
            "app.example.com",
            aliases=("alt.example.com",),
        )
        assert env["RAILS_HOSTS"] == "app.example.com,alt.example.com"


class TestPhoenixDomainEnv:
    def test_single_domain(self):
        env = PHOENIX.domain_env("app.example.com")
        assert env["PHX_HOST"] == "app.example.com"

    def test_aliases_dropped_for_phoenix(self):
        # PHX_HOST is single-valued; aliases would need an Endpoint check_origin
        # patch that env vars can't reach.
        env = PHOENIX.domain_env(
            "app.example.com",
            aliases=("alt.example.com",),
        )
        assert env["PHX_HOST"] == "app.example.com"


class TestBuildDeployContextInjectsDomainEnv:
    def _scaffold_django(self, tmp_path):
        ctx = tmp_path / "django"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text(
            "FROM python\nWORKDIR /app\nRUN pip install django\nCOPY manage.py /app/\n"
        )
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              django:
                build:
                  context: ./django
                ports: ['8000:8000']
        """).strip())
        return compose

    def test_django_domain_set_injects_allowed_hosts_and_csrf(self, tmp_path):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml

        self._scaffold_django(tmp_path)
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text(textwrap.dedent("""
            version: 2
            project: x
            compose_file: docker-compose.yml
            provider: ecs
            provider_config:
              ecs:
                region: us-west-1
                cluster: x
                vpc_cidr: 10.0.0.0/16
            services:
              django:
                public: true
                port: 8000
                domain: app.example.com
                aliases:
                  - alt.example.com
        """).strip())

        _, raw, v2 = load_rc_yml(rc_yml)
        ctx = build_deploy_context(v2, raw, rc_yml)
        env = ctx.services["django"].env
        assert env.get("DJANGO_ALLOWED_HOSTS") == ("app.example.com,alt.example.com")
        assert env.get("CSRF_TRUSTED_ORIGINS") == (
            "https://app.example.com,https://alt.example.com"
        )

    def test_user_rcyml_env_overrides_domain_defaults(self, tmp_path):
        # User has explicit ALLOWED_HOSTS in rc.yml.env → don't clobber.
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml

        self._scaffold_django(tmp_path)
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text(textwrap.dedent("""
            version: 2
            project: x
            compose_file: docker-compose.yml
            provider: ecs
            provider_config:
              ecs:
                region: us-west-1
                cluster: x
                vpc_cidr: 10.0.0.0/16
            services:
              django:
                public: true
                port: 8000
                domain: app.example.com
                env:
                  DJANGO_ALLOWED_HOSTS: '*'
        """).strip())

        _, raw, v2 = load_rc_yml(rc_yml)
        ctx = build_deploy_context(v2, raw, rc_yml)
        env = ctx.services["django"].env
        # User's explicit '*' wins over the domain-derived value.
        assert env["DJANGO_ALLOWED_HOSTS"] == "*"
        # CSRF_TRUSTED_ORIGINS wasn't set in rc.yml.env, so domain inject
        # still applies.
        assert env["CSRF_TRUSTED_ORIGINS"] == "https://app.example.com"

    def test_no_domain_no_injection(self, tmp_path):
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml

        self._scaffold_django(tmp_path)
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text(textwrap.dedent("""
            version: 2
            project: x
            compose_file: docker-compose.yml
            provider: ecs
            provider_config:
              ecs:
                region: us-west-1
                cluster: x
                vpc_cidr: 10.0.0.0/16
            services:
              django: {}
        """).strip())

        _, raw, v2 = load_rc_yml(rc_yml)
        ctx = build_deploy_context(v2, raw, rc_yml)
        env = ctx.services["django"].env
        assert "DJANGO_ALLOWED_HOSTS" not in env
        assert "CSRF_TRUSTED_ORIGINS" not in env

    def test_non_django_service_with_domain_unaffected(self, tmp_path):
        # nginx service with domain — shouldn't get DJANGO_* env (no
        # framework match on a stock nginx Dockerfile).
        from remote_compose.cli_v2 import build_deploy_context, load_rc_yml

        ctx_dir = tmp_path / "nginx"
        ctx_dir.mkdir()
        (ctx_dir / "Dockerfile").write_text("FROM nginx:alpine\n")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              nginx:
                build:
                  context: ./nginx
                ports: ['80:80']
        """).strip())
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text(textwrap.dedent("""
            version: 2
            project: x
            compose_file: docker-compose.yml
            provider: ecs
            provider_config:
              ecs:
                region: us-west-1
                cluster: x
                vpc_cidr: 10.0.0.0/16
            services:
              nginx:
                domain: app.example.com
                public: true
                port: 80
        """).strip())

        _, raw, v2 = load_rc_yml(rc_yml)
        ctx = build_deploy_context(v2, raw, rc_yml)
        env = ctx.services["nginx"].env
        assert "DJANGO_ALLOWED_HOSTS" not in env
        assert "CSRF_TRUSTED_ORIGINS" not in env
