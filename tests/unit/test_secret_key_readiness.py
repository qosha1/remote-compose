"""rc-mbav: never roll a task def onto secret keys that aren't readable yet.

The production failure (2026-08-26, ~12 minutes of downtime on a live tenant):
a single ``rc up`` added two keys to a bundle AND regrouped the services that
reference them. ``terraform apply`` registered the new task definition and
pointed ``aws_ecs_service`` at it, so ECS began placing tasks straight away —
while the values were still on the operator's disk, because the secrets push
ran after the deploy returned. Placement failed with "Retrieved secret from
Secrets Manager did not contain json key NGINX_UPSTREAM", retries exhausted,
and the deployment circuit breaker rolled back onto the previous task
definition — whose sibling services that same apply had already destroyed.

Two independent defences, tested separately here:

  1. ``verify_secret_keys_readable`` — a pure read, before any terraform call.
     Deterministic: it cannot race, and it refuses the apply outright.
  2. the pre-apply push in ``rc up`` — closes the window so the common path
     never reaches defence 1 at all.

The ordering assertion that matters is put_secret_value BEFORE terraform
apply. The pre-existing rc-1bk test asserts put-before-force-roll, which is a
strictly weaker claim: terraform updating the service starts a rollout on its
own, minutes before rc's own force-roll call.
"""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs.provider import ECSProvider

COMPOSE = textwrap.dedent("""
    services:
      nginx:
        image: nginx:alpine
        env_file: .env
        ports:
          - "80:80"
    """)


def _stack(tmp_path, env_body="NGINX_UPSTREAM=django:8000\n"):
    rc = tmp_path / "rc.yml"
    rc.write_text(textwrap.dedent("""
        version: 2
        project: mbav
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-2
            cluster: mbav-cluster
            vpc_cidr: 10.0.0.0/16
        services:
          nginx:
            port: 80
            public: true
        secrets:
          - name: runtime
            source: file
            path: .env
        """).strip())
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    (tmp_path / ".env").write_text(env_body)
    return rc


def _ctx(tmp_path, monkeypatch, **kw):
    rc = _stack(tmp_path, **kw)
    monkeypatch.chdir(tmp_path)
    _v, raw, v2 = load_rc_yml(rc)
    return build_deploy_context(v2, raw, rc)


def _provider_with_secret(live_blob):
    """ECSProvider whose Secrets Manager returns ``live_blob``.

    ``live_blob`` is a dict (the secret's JSON body) or None to raise, which
    is how a not-yet-created secret behaves.
    """
    sm = MagicMock()
    if live_blob is None:
        sm.get_secret_value.side_effect = Exception("ResourceNotFoundException")
    else:
        sm.get_secret_value.return_value = {"SecretString": json.dumps(live_blob)}
    session = MagicMock()
    session.client.return_value = sm
    return ECSProvider(session_factory=lambda _ctx: session), sm


class TestRequiredSecretKeys:
    def test_keys_come_from_the_local_env_file(self, tmp_path, monkeypatch):
        # The task-def secrets[] entries are generated one-per-key from this
        # file, so it is the authority on what the new revision will demand.
        ctx = _ctx(
            tmp_path,
            monkeypatch,
            env_body="NGINX_UPSTREAM=django:8000\nNGINX_FRONTEND=frontend:3000\n",
        )
        provider, _sm = _provider_with_secret({})
        required = provider.required_secret_keys(ctx)
        assert required["mbav/runtime"] == ["NGINX_UPSTREAM", "NGINX_FRONTEND"]
        # env_file_auto derives a second secret from compose's `env_file: .env`
        # alongside the rc.yml-declared one. Both end up referenced by task-def
        # secrets[] entries, so the guard must cover both — a check that only
        # looked at rc.yml's `secrets:` list would miss the auto-expanded half.
        assert required["mbav/env"] == ["NGINX_UPSTREAM", "NGINX_FRONTEND"]


class TestVerifySecretKeysReadable:
    def test_refuses_when_an_existing_secret_lacks_a_referenced_key(
        self, tmp_path, monkeypatch
    ):
        """The outage, in one assertion.

        The secret exists and is populated — it simply does not have the key
        the new task definition references yet. This is the dangerous shape:
        there ARE running tasks and there IS a previous revision to roll back
        onto.
        """
        ctx = _ctx(
            tmp_path,
            monkeypatch,
            env_body="DATABASE_URL=postgres://x\nNGINX_UPSTREAM=django:8000\n",
        )
        provider, _sm = _provider_with_secret({"DATABASE_URL": "postgres://x"})
        with pytest.raises(ProviderConfigError) as exc:
            provider.verify_secret_keys_readable(ctx)
        assert "NGINX_UPSTREAM" in str(exc.value)
        assert "rc secrets push" in str(exc.value)
        # The message must name the rollback, not just the placement failure —
        # the rollback is what turned this from a blip into an outage.
        assert "circuit breaker" in str(exc.value)

    def test_passes_when_every_key_is_present(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path, monkeypatch)
        provider, _sm = _provider_with_secret({"NGINX_UPSTREAM": "django:8000"})
        provider.verify_secret_keys_readable(ctx)  # no raise

    def test_skips_a_secret_that_does_not_exist_yet(self, tmp_path, monkeypatch):
        """First apply must not be blocked.

        Terraform is about to create this secret. There are no running tasks
        to protect and no previous revision to roll back onto, so the failure
        mode this guard exists for cannot occur. The discriminator is
        deliberately "secret absent", not "first apply" — the two differ when
        a NEW secret is added to an EXISTING stack, and that second case is
        the safe one too.
        """
        ctx = _ctx(tmp_path, monkeypatch)
        provider, _sm = _provider_with_secret(None)
        provider.verify_secret_keys_readable(ctx)  # no raise

    def test_unreadable_secret_degrades_to_a_pass_not_a_block(
        self, tmp_path, monkeypatch
    ):
        # A principal without GetSecretValue must not lose the ability to
        # deploy: the checker failing is not the same as the check failing.
        ctx = _ctx(tmp_path, monkeypatch)
        provider, sm = _provider_with_secret({})
        sm.get_secret_value.side_effect = Exception("AccessDeniedException")
        provider.verify_secret_keys_readable(ctx)  # no raise

    def test_env_override_disables_the_block(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path, monkeypatch)
        provider, _sm = _provider_with_secret({})
        monkeypatch.setenv("RC_SKIP_SECRET_KEY_CHECK", "1")
        provider.verify_secret_keys_readable(ctx)  # no raise

    def test_runs_before_terraform_is_invoked(self, tmp_path, monkeypatch):
        """Placement is the wrong side of the line to catch this on.

        The guard is worthless if it runs after apply — by then terraform has
        already pointed the service at the new revision. Assert it raises
        without terraform init/apply ever being called.
        """
        ctx = _ctx(tmp_path, monkeypatch)
        provider, _sm = _provider_with_secret({"SOMETHING_ELSE": "x"})
        with (
            patch("remote_compose.terraform.runner.TerraformRunner.init") as init,
            patch("remote_compose.terraform.runner.TerraformRunner.apply") as apply_,
        ):
            with pytest.raises(ProviderConfigError):
                provider.deploy(ctx)
        assert not init.called, "terraform init ran before the secret guard"
        assert not apply_.called, "terraform apply ran despite unreadable keys"


class TestReadBackWait:
    def test_returns_true_once_the_keys_are_visible(self):
        from remote_compose.cli_commands._dispatchers import _wait_for_secret_keys

        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": json.dumps({"A": "1"})}
        assert _wait_for_secret_keys(sm, "p/s", ["A"], timeout_s=1.0) is True

    def test_returns_false_when_a_key_never_appears(self):
        from remote_compose.cli_commands._dispatchers import _wait_for_secret_keys

        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": json.dumps({"A": "1"})}
        assert _wait_for_secret_keys(sm, "p/s", ["A", "B"], timeout_s=0.05) is False

    def test_a_failing_read_is_not_fatal(self):
        # A throttled or flaky read means "not yet", never "crash the push".
        from remote_compose.cli_commands._dispatchers import _wait_for_secret_keys

        sm = MagicMock()
        sm.get_secret_value.side_effect = Exception("Throttling")
        assert _wait_for_secret_keys(sm, "p/s", ["A"], timeout_s=0.05) is False


class TestRcUpPushesBeforeApply:
    def test_put_secret_value_precedes_terraform_apply(self, tmp_path):
        """The ordering invariant that actually prevents the outage.

        rc-1bk's existing test asserts put-before-FORCE-ROLL. That is not
        enough: terraform updating aws_ecs_service.task_definition starts a
        rollout by itself, well before rc's force-roll call. The put must land
        before ``terraform apply``.
        """
        rc = _stack(tmp_path, env_body="NGINX_UPSTREAM=django:8000\n")
        calls: list[str] = []
        store: dict[str, str] = {}

        def fake_session(profile_name=None, region_name=None):
            sess = MagicMock()

            def client_factory(svc_name, **_kw):
                client = MagicMock()
                if svc_name == "secretsmanager":

                    def put(**kw):
                        calls.append("sm.put_secret_value")
                        store[kw["SecretId"]] = kw.get("SecretString", "{}")
                        return {}

                    def get(**kw):
                        if kw["SecretId"] not in store:
                            raise Exception("ResourceNotFoundException")
                        return {"SecretString": store[kw["SecretId"]]}

                    client.put_secret_value.side_effect = put
                    client.get_secret_value.side_effect = get
                    # The secret already exists — the shape that caused the
                    # outage. describe_secret succeeding is what makes the
                    # pre-apply pass push instead of skipping.
                    client.describe_secret.return_value = {"Name": "mbav/runtime"}
                return client

            sess.client.side_effect = client_factory
            return sess

        def record_apply(*_a, **_kw):
            calls.append("tf.apply")

        with (
            patch("boto3.Session", side_effect=fake_session),
            patch(
                "remote_compose.provider.ecs.provider.ECSProvider._build_and_push_images",
                return_value=["nginx"],
            ),
            patch("remote_compose.terraform.runner.TerraformRunner.init"),
            patch(
                "remote_compose.terraform.runner.TerraformRunner.apply",
                side_effect=record_apply,
            ),
            patch(
                "remote_compose.terraform.runner.TerraformRunner.output",
                return_value={"ecr_repositories": {"value": {}}},
            ),
            patch("remote_compose.cli_v2.run_auto_on_deploy_hooks_for_path"),
            patch(
                "remote_compose.provider.ecs.provider.ECSProvider"
                "._reconcile_orphan_log_groups"
            ),
        ):
            result = CliRunner().invoke(
                cli, ["-c", str(rc), "up"], catch_exceptions=False
            )

        assert (
            "tf.apply" in calls
        ), f"apply never ran; calls={calls} out={result.output}"
        first_put = calls.index("sm.put_secret_value")
        first_apply = calls.index("tf.apply")
        assert first_put < first_apply, (
            f"rc up ran terraform apply at {first_apply} before pushing "
            f"secrets at {first_put}. ECS starts placing tasks inside that "
            f"apply, so the new task def would reference keys Secrets Manager "
            f"does not have yet — this is rc-mbav. calls={calls}"
        )
