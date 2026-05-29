"""Failing-RED tests for remote-compose-12d.

Lifecycle parent: env-file-leak (1.1=aio, 1.2=bjs, 2.1=iej).

Requirements covered:
  R1. Default-on env_file → SM auto-promotion. rc.yml `source: file`
      (NOT env_file_auto) MUST still result in compose env_file values
      landing as SM secret references.
  R2. Per-service env_file scoping. Service that only references
      .postgres MUST NOT receive .django-only keys in its task-def
      secrets[].
  R3. rc.yml secrets[].name precedence. When rc.yml declares a secret
      whose name matches a compose env_file basename, rc.yml's `path:`
      is the source of truth for SM content (ServiceSpec wiring routes
      via the rc.yml path).
  R4. UNION on key collision (rc.yml WINS). When rc.yml-path file and
      compose-env-file file share a KEY, the rc.yml-path VALUE wins
      at SM-push time.
  R5. Per-service plaintext env stripping. A KEY sourced from SM via
      env_file MUST NOT also appear in plaintext environment[] for the
      same service.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest import mock


from remote_compose.cli_v2 import build_deploy_context, load_rc_yml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ENV_DJANGO = textwrap.dedent("""
    REDIS_URL=redis://localhost:6379/0
    CELERY_BROKER_URL=redis://localhost:6379/0
""").strip()

ENV_POSTGRES = textwrap.dedent("""
    DATABASE_URL=postgres://x
    POSTGRES_DB=app
    POSTGRES_USER=app
    POSTGRES_PASSWORD=secret
""").strip()

# Test variant: same names but DIFFERENT values for collision testing
ENV_DJANGO_TEST = textwrap.dedent("""
    REDIS_URL=redis://test:6379/0
    CELERY_BROKER_URL=redis://test:6379/0
""").strip()


def _scaffold(tmp_path: Path) -> Path:
    """Lay out the sentinal-shaped repro: rc.yml + local compose +
    .test/.django + .test/.postgres + .local/.django + .local/.postgres.
    Returns the rc.yml path."""
    (tmp_path / "envs" / "test").mkdir(parents=True)
    (tmp_path / "envs" / "local").mkdir(parents=True)
    (tmp_path / "envs" / "test" / ".django").write_text(ENV_DJANGO_TEST)
    (tmp_path / "envs" / "test" / ".postgres").write_text(ENV_POSTGRES)
    (tmp_path / "envs" / "local" / ".django").write_text(ENV_DJANGO)
    (tmp_path / "envs" / "local" / ".postgres").write_text(ENV_POSTGRES)

    (tmp_path / "compose.yml").write_text(textwrap.dedent("""
        services:
          django:
            image: django:test
            env_file:
              - envs/local/.django
              - envs/local/.postgres
          postgres:
            image: postgres:15
            env_file:
              - envs/local/.postgres
          redis:
            image: redis:6
    """))

    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text(textwrap.dedent("""
        version: 2
        project: leak-test
        compose_file: compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
            cluster: leak-test-cluster
            vpc_cidr: 10.99.0.0/16
        services:
          django:
            port: 8000
          postgres:
            type: infrastructure
          redis:
            type: infrastructure
        secrets:
          - name: local-django
            source: file
            path: envs/test/.django
          - name: local-postgres
            source: file
            path: envs/test/.postgres
    """))
    return rc_yml


# ---------------------------------------------------------------------------
# R1 + R5 — default env_file → SM auto-promotion (no env_file_auto opt-in)
# ---------------------------------------------------------------------------


def test_R1_compose_env_file_keys_route_to_secrets_not_plaintext_env(tmp_path):
    """Even with rc.yml using `source: file` (not env_file_auto), every
    KEY from a compose env_file MUST end up reachable via secrets[],
    NOT plaintext environment[]."""
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    django_spec = ctx.services["django"]
    plaintext_env = django_spec.env

    leaked = {
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "DATABASE_URL",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    plaintext_leaks = leaked & plaintext_env.keys()
    assert not plaintext_leaks, (
        f"R1 FAIL: keys from compose env_file leaked as PLAINTEXT in "
        f"django.env: {sorted(plaintext_leaks)}. They must route through "
        f"SM secrets[] instead. Full plaintext env: {sorted(plaintext_env)}"
    )


def test_R1_auto_secrets_get_added_to_deploy_context(tmp_path):
    """The DeployContext.secrets list MUST grow to include auto-discovered
    env_file secrets when no env_file_auto opt-in is present."""
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    secret_names = {s.name for s in ctx.secrets}
    # The rc.yml declared two secrets with names 'local-django' / 'local-postgres'
    # whose paths happen to match the auto-name a compose env_file would
    # generate (.envs/local/.django → 'local-django'). After R3
    # precedence-merging, they should still appear under those names.
    assert "local-django" in secret_names, (
        f"R1 FAIL: 'local-django' missing from ctx.secrets; "
        f"got {sorted(secret_names)}"
    )
    assert "local-postgres" in secret_names, (
        f"R1 FAIL: 'local-postgres' missing from ctx.secrets; "
        f"got {sorted(secret_names)}"
    )


def test_R1_auto_promotes_compose_envfile_when_rcyaml_has_no_secrets(tmp_path):
    """The strict R1 case: rc.yml has NO secrets[] block at all; rc must
    still auto-discover and emit per-env-file SM secrets from compose's
    env_file directives. This is what makes the fix universal — not
    opt-in via `source: env_file_auto`."""
    rc_yml = _scaffold(tmp_path)
    # Strip rc.yml secrets[] entirely.
    text = rc_yml.read_text()
    # Drop everything from "secrets:" to EOF (it's at end of file in our scaffold).
    text = text.split("secrets:")[0].rstrip() + "\n"
    rc_yml.write_text(text)

    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    secret_names = {s.name for s in ctx.secrets}
    assert "local-django" in secret_names, (
        f"R1 FAIL: rc.yml had no secrets[] but compose has env_file: "
        f"envs/local/.django; expected auto-discovered 'local-django' "
        f"in ctx.secrets. Got: {sorted(secret_names)}"
    )
    assert "local-postgres" in secret_names, (
        f"R1 FAIL: auto-discovery missing 'local-postgres'. "
        f"Got: {sorted(secret_names)}"
    )


# ---------------------------------------------------------------------------
# R2 — per-service env_file scoping
# ---------------------------------------------------------------------------


def test_R2_service_with_only_postgres_envfile_does_not_get_django_keys(tmp_path):
    """postgres compose entry references only envs/local/.postgres.
    postgres ServiceSpec MUST track only postgres-secret keys, not
    django-only keys (REDIS_URL, CELERY_BROKER_URL).
    """
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    postgres = ctx.services["postgres"]
    # Per the design (R2), ServiceSpec gains env_file_secret_names: list[str].
    # postgres must list ONLY 'local-postgres', not 'local-django'.
    names = getattr(postgres, "env_file_secret_names", None)
    assert names is not None, (
        "R2 FAIL: ServiceSpec missing env_file_secret_names attr — "
        "fix needs to add this field per the 1.2 system requirements"
    )
    assert "local-django" not in names, (
        f"R2 FAIL: postgres got django secret reference. "
        f"env_file_secret_names={names}"
    )
    assert "local-postgres" in names, (
        f"R2 FAIL: postgres missing local-postgres ref. "
        f"env_file_secret_names={names}"
    )


def test_R2_service_referencing_both_envfiles_gets_both(tmp_path):
    """django compose references both envs/local/.django + .postgres.
    django ServiceSpec MUST have BOTH local-django and local-postgres
    in env_file_secret_names."""
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    django = ctx.services["django"]
    names = set(getattr(django, "env_file_secret_names", []) or [])
    assert {"local-django", "local-postgres"} <= names, (
        f"R2 FAIL: django missing one or both expected secret refs. "
        f"env_file_secret_names={sorted(names)}"
    )


def test_R2_service_with_no_envfile_gets_empty_list(tmp_path):
    """redis has no env_file in compose.
    redis ServiceSpec.env_file_secret_names MUST be empty."""
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    redis = ctx.services["redis"]
    names = getattr(redis, "env_file_secret_names", None)
    assert names == [] or names == (), (
        f"R2 FAIL: redis has no compose env_file but got "
        f"env_file_secret_names={names}"
    )


# ---------------------------------------------------------------------------
# R3 — rc.yml secret name precedence
# ---------------------------------------------------------------------------


def test_R3_rcyaml_secret_path_wins_over_compose_envfile_path(tmp_path):
    """When rc.yml declares secrets:[{name: 'local-django', path: 'envs/test/.django'}]
    AND a compose env_file 'envs/local/.django' auto-resolves to the
    same name 'local-django', rc.yml's path is the SM-content source.
    """
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    by_name = {s.name: s for s in ctx.secrets}
    sec = by_name.get("local-django")
    assert (
        sec is not None
    ), f"R3 FAIL: local-django not in ctx.secrets; got {sorted(by_name)}"
    assert "envs/test/.django" in str(
        sec.path
    ), f"R3 FAIL: rc.yml secret path should win. Got path={sec.path!r}"


# ---------------------------------------------------------------------------
# R4 — union on collision, rc.yml wins
# ---------------------------------------------------------------------------


def test_R4_union_collision_rcyaml_wins_at_secrets_push(tmp_path):
    """At _secrets_push_v2 time, when an SM secret has both an rc.yml
    path AND a compose env_file path with overlapping keys, the rc.yml
    file's VALUE wins.

    Setup: envs/test/.django has REDIS_URL=redis://test:... and
    envs/local/.django has REDIS_URL=redis://localhost:.... Push
    must upload redis://test:... (rc.yml-source) for REDIS_URL.
    """
    rc_yml = _scaffold(tmp_path)
    pushed: dict[str, dict] = {}

    def fake_session(profile_name=None, region_name=None):
        sess = mock.MagicMock()

        def client_factory(svc, **_kw):
            client = mock.MagicMock()
            if svc == "secretsmanager":

                def put_secret_value(**kw):
                    pushed[kw["SecretId"]] = json.loads(kw["SecretString"])
                    return {}

                client.put_secret_value.side_effect = put_secret_value
                client.get_secret_value.return_value = {"SecretString": "{}"}
            if svc == "ecs":
                client.update_service.return_value = {}
            return client

        sess.client.side_effect = client_factory
        return sess

    from click.testing import CliRunner
    from remote_compose.cli import cli

    runner = CliRunner()
    with mock.patch("boto3.Session", side_effect=fake_session):
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "secrets", "push"],
            catch_exceptions=False,
        )

    assert (
        result.exit_code == 0
    ), f"R4 FAIL: secrets push errored — output:\n{result.output}"
    body = pushed.get("leak-test/local-django")
    assert body is not None, (
        f"R4 FAIL: nothing pushed to leak-test/local-django. "
        f"pushed: {sorted(pushed)}"
    )
    assert body.get("REDIS_URL") == "redis://test:6379/0", (
        f"R4 FAIL: rc.yml-path REDIS_URL value should win on collision. "
        f"Got: {body.get('REDIS_URL')!r}"
    )


# ---------------------------------------------------------------------------
# R5 — per-service plaintext stripping (extends R1 with a stricter check)
# ---------------------------------------------------------------------------


def test_R5_postgres_keys_not_in_django_plaintext_env(tmp_path):
    """django references both .django + .postgres env_files. After fix,
    none of those keys should appear in django.env (they all go to
    secrets[])."""
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    all_envfile_keys = {
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "DATABASE_URL",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    django_plaintext = set(ctx.services["django"].env)
    leaked = all_envfile_keys & django_plaintext
    assert not leaked, (
        f"R5 FAIL: keys from compose env_files leaked into django.env: "
        f"{sorted(leaked)}. Should all be in secrets[]."
    )


def test_R5_redis_has_no_envfile_keys_anywhere(tmp_path):
    """redis has no compose env_file. Its env should be empty (or
    contain only rc.yml-set values, which we don't set here)."""
    rc_yml = _scaffold(tmp_path)
    _, raw, v2 = load_rc_yml(rc_yml)
    ctx = build_deploy_context(v2, raw, rc_yml)

    redis_env = ctx.services["redis"].env
    forbidden = {
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "DATABASE_URL",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    leaked = forbidden & redis_env.keys()
    assert not leaked, (
        f"R5 FAIL: redis (which has no env_file in compose) got keys "
        f"in plaintext env: {sorted(leaked)}"
    )
