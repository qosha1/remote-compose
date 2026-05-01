"""Framework presets — generic web-framework registry (rc-e5u.47).

Centralises every place rc previously hardcoded ``"django"`` heuristics:
detection from a compose service's Dockerfile, the migrate command, the
star-host environment overrides for ephemeral test stacks, the singleton
command patterns the deploy gate uses, and the nginx Host-header rewrite
the upstream-resolver fixer emits.

Adding a new framework is one ``Framework(...)`` entry below — no other
file changes. The detection scans Dockerfiles in the compose service's
build context and returns the first registered framework whose markers
match. ``rc.yml`` ``services.<name>.framework: <name>`` (future) takes
precedence when ambiguous.

Built-ins:
  - django  — Python / manage.py / wsgi.py / asgi.py
  - rails   — Ruby on Rails / Gemfile / config/application.rb
  - phoenix — Elixir Phoenix / mix.exs / phx.server

The Django case is the verified-working baseline (rc-e5u.46.6 against the
start-simpli stack on us-west-1, 2026-04-26). Rails / Phoenix presets
mirror the structure but have NOT yet been end-to-end validated against a
real Rails or Phoenix compose. They light up the same code paths so that
a Rails user adding the right markers gets the same auto-fix surface
Django enjoys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Heuristic budget for Dockerfile scans (mirrors compose_warnings._MAX_FILE_BYTES).
_MAX_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class Framework:
    """A web-framework preset.

    ``name``                       — registry key (``"django"``, ``"rails"``, ...).
    ``dockerfile_markers``         — case-insensitive substrings; if ANY
                                     appears in the service's Dockerfile,
                                     this framework matches.
    ``migrate_command``            — argv to run as a ``lifecycle.migrate``
                                     hook on every ``rc deploy``. None =
                                     framework has no migrate concept.
    ``testing_defaults_env``       — env vars injected into rc.yml when
                                     the project name starts with
                                     ``rc-test-`` (or the user passes
                                     ``--testing-defaults``). The point is
                                     to relax host validation so a curl at
                                     a random ALB DNS succeeds without
                                     hand-editing ALLOWED_HOSTS / similar.
    ``testing_defaults_marker_keys`` — env keys that, if already present in
                                     compose, cause us to SKIP the
                                     testing-defaults injection (user has
                                     hand-wired it themselves).
    ``host_header_rewrite``        — when this framework is the upstream
                                     in an nginx-fronted stack, what
                                     value to use for
                                     ``proxy_set_header Host`` so the
                                     framework's host validation passes.
                                     None = no rewrite. Django needs
                                     ``"localhost"``; Rails / Phoenix do
                                     not by default.
    """

    name: str
    dockerfile_markers: tuple[str, ...]
    migrate_command: Optional[tuple[str, ...]] = None
    testing_defaults_env: dict[str, str] = field(default_factory=dict)
    testing_defaults_marker_keys: tuple[str, ...] = ()
    host_header_rewrite: Optional[str] = None
    # rc-32x: env vars to auto-inject when the service has a domain wired
    # (rc.yml services.<svc>.domain set, typically by `rc up --domain`).
    # Each value is a Python format string with ``{domain}`` and
    # ``{aliases_csv}`` placeholders. cli_v2.build_deploy_context fills them
    # in at deploy time and merges the result UNDER the user's rc.yml.env
    # (user override wins on collision).
    #
    # Django: ALLOWED_HOSTS rejects unknown Host headers (400 SuspiciousOp);
    # CSRF middleware rejects POSTs whose Origin doesn't match. Both must
    # know about the ALB-fronted hostname or admin login + every CSRF-
    # protected endpoint 403s.
    domain_env_template: dict[str, str] = field(default_factory=dict)

    def domain_env(
        self, domain: str, aliases: tuple[str, ...] = (),
    ) -> dict[str, str]:
        """rc-32x: render domain_env_template with the deploy's domain + aliases.

        Substitutions:
          {domain}              — primary FQDN
          {aliases_csv}         — leading comma + comma-separated aliases
                                  (empty when no aliases)
          {https_aliases_csv}   — leading comma + comma-separated
                                  https://<alias> URLs (empty when no aliases)

        Returns an empty dict when the framework has no template OR domain
        is falsy. Caller merges the result UNDER the user's rc.yml.env so
        explicit overrides win.
        """
        if not self.domain_env_template or not domain:
            return {}
        aliases_csv = ("," + ",".join(aliases)) if aliases else ""
        https_aliases_csv = (
            ("," + ",".join(f"https://{a}" for a in aliases)) if aliases else ""
        )
        out: dict[str, str] = {}
        for k, v in self.domain_env_template.items():
            out[k] = v.format(
                domain=domain,
                aliases_csv=aliases_csv,
                https_aliases_csv=https_aliases_csv,
            )
        return out
    # rc-e5u.35.7: per-framework lifecycle hooks injected when a service
    # declares ``framework: <name>`` (or detect_framework matches via
    # Dockerfile markers). Each entry is ``hook_name -> argv``; cli_v2
    # merges them into ServiceSpec.lifecycle for hooks not already
    # declared in rc.yml. Examples:
    #   django: createsuperuser, shell, collectstatic
    #   rails:  console, db:seed
    # Users can still override per-hook in rc.yml — explicit declarations
    # win over framework defaults.
    lifecycle_hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Built-in registry
# ---------------------------------------------------------------------------

DJANGO = Framework(
    name="django",
    dockerfile_markers=(
        "manage.py",
        "wsgi.py",
        "asgi.py",
        " django",
        "django>=",
        "django==",
    ),
    migrate_command=("python", "manage.py", "migrate", "--noinput"),
    testing_defaults_env={
        "DJANGO_ALLOWED_HOSTS": "*",
        "CSRF_TRUSTED_ORIGINS": "*",
        "DJANGO_DEBUG": "False",
    },
    testing_defaults_marker_keys=("DJANGO_ALLOWED_HOSTS",),
    host_header_rewrite="localhost",
    # rc-32x: when --domain is set, inject these so admin login + CSRF
    # work over the new hostname. The hostnames go in ALLOWED_HOSTS;
    # CSRF needs the full https:// scheme. SECURE_PROXY_SSL_HEADER lives
    # in settings.py (env injection can't construct a tuple), so we
    # only patch what env can reach.
    domain_env_template={
        "DJANGO_ALLOWED_HOSTS": "{domain}{aliases_csv}",
        "CSRF_TRUSTED_ORIGINS": "https://{domain}{https_aliases_csv}",
    },
    lifecycle_hooks={
        # `rc lifecycle createsuperuser <svc>` for one-off admin user.
        # --noinput honors DJANGO_SUPERUSER_EMAIL / _USERNAME / _PASSWORD
        # env vars (USERNAME_FIELD-aware: works on either pk model).
        "createsuperuser": (
            "python", "manage.py", "createsuperuser", "--noinput",
        ),
        # Static-file collection — typically already in /start, but
        # exposed as a hook for re-running after manual asset changes.
        "collectstatic": (
            "python", "manage.py", "collectstatic", "--noinput",
        ),
        # Interactive shell. CLI uses interactive=True via the lifecycle
        # subcommand, attaching stdin/tty.
        "shell": ("python", "manage.py", "shell"),
        # Database shell — same pattern.
        "dbshell": ("python", "manage.py", "dbshell"),
        # rc-2kj: fixture seeding. Symmetric with Rails 'seed'. Defaults
        # to fixtures/sample.json; override via env with the explicit
        # `rc lifecycle loaddata <svc> -- python manage.py loaddata <path>`
        # form when a different fixture file is needed.
        "loaddata": (
            "python", "manage.py", "loaddata",
            "fixtures/sample.json",
        ),
    },
)

RAILS = Framework(
    name="rails",
    dockerfile_markers=(
        "gemfile",
        "config/application.rb",
        "config/routes.rb",
        "bin/rails",
        "rails server",
        "rails s ",
    ),
    migrate_command=("bundle", "exec", "rails", "db:migrate"),
    testing_defaults_env={
        # Rails 6+ enforces config.hosts in non-development envs; setting
        # RAILS_HOSTS=* via an initializer is the convention the community
        # has settled on for ephemeral preview stacks. RAILS_FORCE_SSL=false
        # avoids the ALB-vs-app SSL handshake mismatch.
        "RAILS_HOSTS": "*",
        "RAILS_FORCE_SSL": "false",
    },
    testing_defaults_marker_keys=("RAILS_HOSTS",),
    host_header_rewrite=None,
    # rc-32x: domain → RAILS_HOSTS so config.hosts accepts it in production.
    domain_env_template={
        "RAILS_HOSTS": "{domain}{aliases_csv}",
    },
    lifecycle_hooks={
        "console": ("bundle", "exec", "rails", "console"),
        "dbconsole": ("bundle", "exec", "rails", "dbconsole"),
        # db:seed is destructive on schema-only seeds; --no-bundle skipped
        # because most users want the bundle env vars resolved.
        "seed": ("bundle", "exec", "rails", "db:seed"),
        # rails routes — useful for verifying route table inside the live
        # container after a deploy (compares ALB host-routing vs app-side).
        "routes": ("bundle", "exec", "rails", "routes"),
    },
)

PHOENIX = Framework(
    name="phoenix",
    dockerfile_markers=(
        "mix.exs",
        "phx.server",
        "mix phx",
    ),
    migrate_command=("mix", "ecto.migrate"),
    testing_defaults_env={
        # Phoenix Endpoint :url config validates Host against PHX_HOST in
        # release mode; '*' bypasses for ephemeral preview stacks.
        "PHX_HOST": "*",
    },
    testing_defaults_marker_keys=("PHX_HOST",),
    host_header_rewrite=None,
    # rc-32x: PHX_HOST is single-valued in Phoenix, so we use just the
    # primary domain. Aliases are dropped here — they'd need an Endpoint
    # check_origin patch that env can't reach.
    domain_env_template={
        "PHX_HOST": "{domain}",
    },
    lifecycle_hooks={
        # `iex -S mix` — the canonical Phoenix REPL.
        "console": ("iex", "-S", "mix"),
        "seed": ("mix", "run", "priv/repo/seeds.exs"),
        "rollback": ("mix", "ecto.rollback"),
    },
)


_BUILT_IN_FRAMEWORKS: tuple[Framework, ...] = (DJANGO, RAILS, PHOENIX)
_REGISTRY: list[Framework] = list(_BUILT_IN_FRAMEWORKS)


def register_framework(framework: Framework) -> None:
    """Add a framework to the detection registry. Later wins on duplicate name."""
    global _REGISTRY
    _REGISTRY = [f for f in _REGISTRY if f.name != framework.name] + [framework]


def all_frameworks() -> tuple[Framework, ...]:
    return tuple(_REGISTRY)


def framework_by_name(name: str) -> Optional[Framework]:
    """Return the framework registered under ``name`` (case-insensitive)."""
    if not name:
        return None
    n = name.strip().lower()
    for f in _REGISTRY:
        if f.name == n:
            return f
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _resolve_dockerfile(svc_compose: dict, compose_path: Path) -> Optional[Path]:
    """Return the absolute path of the service's Dockerfile, or None."""
    build = svc_compose.get("build")
    if not build:
        return None
    compose_dir = compose_path.parent
    if isinstance(build, str):
        ctx = (compose_dir / build).resolve()
        df_rel = "Dockerfile"
    elif isinstance(build, dict):
        ctx = (compose_dir / build.get("context", ".")).resolve()
        df_rel = build.get("dockerfile") or "Dockerfile"
    else:
        return None
    if Path(df_rel).is_absolute():
        return Path(df_rel)
    return (ctx / df_rel).resolve()


def detect_framework(
    svc_compose: dict,
    compose_path: Path,
) -> Optional[Framework]:
    """Detect the framework backing this compose service.

    Reads the service's Dockerfile (if any) and returns the first
    registered framework whose ``dockerfile_markers`` appear in the
    file's lower-cased contents. Returns None when no framework matches
    or the service has no buildable Dockerfile (image-only services).

    Stable order: registry order. Built-ins are django -> rails ->
    phoenix; community-registered frameworks come after. Conflicts in
    practice are rare — a Django Dockerfile won't say ``mix.exs``.
    """
    df_path = _resolve_dockerfile(svc_compose, compose_path)
    if df_path is None:
        return None
    try:
        if not df_path.is_file() or df_path.stat().st_size > _MAX_FILE_BYTES:
            return None
        content = df_path.read_text(errors="replace")
    except OSError:
        return None
    lowered = content.lower()
    for f in _REGISTRY:
        if any(m in lowered for m in f.dockerfile_markers):
            return f
    return None
