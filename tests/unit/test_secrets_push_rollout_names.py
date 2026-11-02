"""RED for startsim-hsp7p: the post-apply force-roll in `rc up` /
`rc secrets push` must address ECS services by the name terraform rendered.

On a grouped task on a shared cluster the live services are
``<service_name_prefix><group>`` (``tandem-nginx``, ``tandem-postgres``).
``_secrets_push_v2`` instead rolled the bare compose names, so every call hit
ServiceNotFoundException (harmless -- terraform had already rolled them -- but
it buries real failures). Mocked boto3 only; nothing here touches AWS.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

COMPOSE = textwrap.dedent("""
    services:
      nginx:
        image: nginx:alpine
      django:
        image: example/django
        env_file: .env
      postgres:
        image: postgres:16
      redis:
        image: redis:7
    """)

RC_YML = textwrap.dedent("""
    version: 2
    project: tandem
    compose_file: docker-compose.yml
    provider: ecs
    provider_config:
      ecs:
        region: us-west-2
        cluster: foundry-tenants
        service_name_prefix: tandem-
        vpc_cidr: 10.0.0.0/16
    task_groups:
      nginx:
        services: [nginx, django]
      postgres:
        services: [postgres, redis]
    services:
      nginx:
        port: 80
      django:
        port: 8000
      postgres: {}
      redis: {}
    secrets:
      - name: web
        source: file
        path: .env
    """)


def _run_push(tmp_path, capsys=None):
    (tmp_path / "rc.yml").write_text(RC_YML.strip())
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    (tmp_path / ".env").write_text("A=1\n")

    ecs = MagicMock()
    live = {"tandem-nginx", "tandem-postgres"}

    def update_service(**kw):
        # what the real API does for a name that is not a live service
        if kw["service"] not in live:
            raise RuntimeError("ServiceNotFoundException: " + kw["service"])
        return {}

    ecs.update_service.side_effect = update_service
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": '{"A": "1"}'}
    ecs.describe_services.return_value = {"services": []}
    sess = MagicMock()
    sess.client.side_effect = lambda name, **_: ecs if name == "ecs" else sm

    from remote_compose.cli_commands._dispatchers import _secrets_push_v2

    with patch("boto3.Session", return_value=sess):
        _secrets_push_v2(str(tmp_path / "rc.yml"), rollout=True)
    return ecs


def test_rollout_uses_prefixed_group_service_names(tmp_path):
    ecs = _run_push(tmp_path)
    rolled = sorted(c.kwargs["service"] for c in ecs.update_service.call_args_list)
    assert rolled == ["tandem-nginx", "tandem-postgres"]


def test_rollout_hits_each_group_once_on_the_shared_cluster(tmp_path):
    ecs = _run_push(tmp_path)
    for call in ecs.update_service.call_args_list:
        assert call.kwargs["cluster"] == "foundry-tenants"
        assert call.kwargs["forceNewDeployment"] is True
    assert ecs.update_service.call_count == 2


def test_orphan_key_check_describes_prefixed_names(tmp_path):
    ecs = _run_push(tmp_path)
    described = sorted(
        s for c in ecs.describe_services.call_args_list for s in c.kwargs["services"]
    )
    assert described == ["tandem-nginx", "tandem-postgres"]


def test_rollout_prints_no_service_not_found(tmp_path, capsys):
    _run_push(tmp_path)
    out = capsys.readouterr()
    assert "ServiceNotFound" not in out.out + out.err
    assert "rollout failed" not in out.out + out.err
