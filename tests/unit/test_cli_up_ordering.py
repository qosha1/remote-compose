"""Failing-RED tests for remote-compose-1bk.

`rc up` currently calls dispatch_if_v2('deploy') BEFORE _secrets_push_v2.
provider.deploy() ends with _force_new_deployments which calls
ecs.update_service(forceNewDeployment=True). On a fresh stack the SM secrets
are still placeholders at that point, so the rolled tasks fail with
ResourceInitializationError unable to pull secrets, the run_auto_on_deploy
wait_for_stable loop times out at 5 min, and each lifecycle hook's
provider.exec waits another 5 min for a RUNNING task that never appears.
Total hang: 30+ min on what should be a working deploy.

The fix: either (a) populate secrets BEFORE the force-roll, or (b) gate
the force-roll on a pre-flight check that no SM secret returns a
SecretsManagerException for empty/placeholder values, surfacing
'rc secrets push first'.

Either fix passes this test: assert update_service(forceNewDeployment=True)
is NEVER called while SM secrets are placeholders.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from remote_compose.cli import cli


COMPOSE_FIXTURE = textwrap.dedent(
    """
    services:
      web:
        image: nginx:alpine
        env_file: .env
        ports:
          - "80:80"
    """
)


@pytest.fixture
def runner():
    return CliRunner()


def _write_v2_stack(tmp_path):
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text(textwrap.dedent(
        """
        version: 2
        project: ordering-test
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
            cluster: ordering-test-cluster
            vpc_cidr: 10.0.0.0/16
        services:
          web:
            port: 80
        secrets:
          - name: web
            source: file
            path: .env
        """
    ).strip())
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_FIXTURE)
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://x\n")
    return rc_yml


def test_rc_up_does_not_force_roll_until_secrets_populated(runner, tmp_path):
    """Track the call order of update_service vs put_secret_value.

    A correct rc up either:
      (a) calls put_secret_value BEFORE any update_service(forceNewDeployment=True), or
      (b) skips update_service entirely until secrets are detected as populated.

    Today rc up calls deploy() (which force-rolls inside _force_new_deployments)
    BEFORE _secrets_push_v2 runs put_secret_value. This test fails until 1bk
    is fixed.
    """
    rc_yml = _write_v2_stack(tmp_path)

    boto_calls: list[tuple[str, str]] = []

    def fake_session(profile_name=None, region_name=None):
        sess = MagicMock()

        def client_factory(svc_name, **_kw):
            client = MagicMock()
            if svc_name == "ecs":
                def update_service(**kw):
                    if kw.get("forceNewDeployment"):
                        boto_calls.append(("ecs.update_service.force", kw.get("service", "")))
                    return {}
                client.update_service.side_effect = update_service
            if svc_name == "secretsmanager":
                def put_secret_value(**kw):
                    boto_calls.append(("sm.put_secret_value", kw.get("SecretId", "")))
                    return {}
                client.put_secret_value.side_effect = put_secret_value
                client.get_secret_value.return_value = {"SecretString": "{}"}
            return client

        sess.client.side_effect = client_factory
        return sess

    # The provider's _default_session_factory + dispatchers.py both go
    # through boto3.Session — patch that one entry point so EVERY boto
    # client routes through our recorder.
    # _default_session_factory + dispatchers.py both call boto3.Session —
    # patch the global once and every boto client routes through our recorder.
    # Force the build phase to "succeed" with a non-empty pushed list so the
    # force-roll path inside provider.deploy actually fires (this is what
    # the sentinal stack experienced — services with build_context did
    # build+push, then force-rolled while SM secrets were still placeholders).
    with patch("boto3.Session", side_effect=fake_session), \
         patch(
             "remote_compose.provider.ecs.provider.ECSProvider._build_and_push_images",
             return_value=["web"],
         ), \
         patch("remote_compose.terraform.runner.TerraformRunner.init",
               return_value=None), \
         patch("remote_compose.terraform.runner.TerraformRunner.apply",
               return_value=None), \
         patch("remote_compose.terraform.runner.TerraformRunner.output",
               return_value={"ecr_repositories": {"value": {
                   "web": "111.dkr.ecr.us-west-1.amazonaws.com/ordering-test/web",
               }}}), \
         patch("remote_compose.cli_v2.run_auto_on_deploy_hooks_for_path",
               return_value=None), \
         patch("remote_compose.provider.ecs.provider.ECSProvider._reconcile_orphan_log_groups",
               return_value=None):
        result = runner.invoke(
            cli, ["-c", str(rc_yml), "up"], catch_exceptions=False,
        )

    # Find first force-roll and first secret-put.
    first_force = next(
        (i for i, c in enumerate(boto_calls) if c[0] == "ecs.update_service.force"),
        None,
    )
    first_put = next(
        (i for i, c in enumerate(boto_calls) if c[0] == "sm.put_secret_value"),
        None,
    )

    assert first_force is not None, (
        f"expected force-roll to occur during rc up to repro 1bk; "
        f"calls={boto_calls!r} output={result.output!r}"
    )

    assert first_put is not None, (
        f"rc up force-rolled at index {first_force} without ever pushing "
        f"secrets — that's the exact 1bk hang trigger. calls={boto_calls!r}"
    )
    assert first_put < first_force, (
        f"rc up force-rolled BEFORE pushing secrets "
        f"(force at {first_force}, put at {first_put}). This causes the "
        f"sentinal-style 30+ min hang because the rolled tasks fail with "
        f"ResourceInitializationError. calls={boto_calls!r}"
    )
