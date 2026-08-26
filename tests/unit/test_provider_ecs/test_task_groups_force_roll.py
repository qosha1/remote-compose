"""Force-rolling a grouped stack (rc-ib01.1).

``_force_new_deployments`` iterated rc.yml SERVICE names and issued one
``update_service`` per name. Under grouping that is N sequential deployments of
the SAME task, and ``_DEPLOY_ORDER``'s type-based ordering stops meaning
anything inside a group — containers in one task start together.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.config.v2_schema import TaskGroupV2
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider

pytestmark = pytest.mark.unit


def _svc(name: str, **kw) -> ServiceSpec:
    kw.setdefault("cpu", 256)
    kw.setdefault("memory", 512)
    kw.setdefault("type", "application")
    return ServiceSpec(name=name, **kw)


def _services(**overrides) -> dict[str, ServiceSpec]:
    specs = {
        "nginx": _svc("nginx", type="proxy", public=True, port=80),
        "django": _svc("django", port=8000),
        "frontend": _svc("frontend", port=3000),
        "postgres": _svc("postgres", type="infrastructure", port=5432),
        "redis": _svc("redis", type="infrastructure", port=6379),
    }
    for name, kw in overrides.items():
        for k, v in kw.items():
            setattr(specs[name], k, v)
    return specs


def _groups() -> dict[str, TaskGroupV2]:
    return {
        "nginx": TaskGroupV2(name="nginx", services=["nginx", "django", "frontend"]),
        "postgres": TaskGroupV2(name="postgres", services=["postgres", "redis"]),
    }


def _ctx(tmp_path: Path, services=None, groups=None, **ecs_cfg) -> DeployContext:
    cfg = {"region": "us-west-2", "cluster": "t-cluster", "vpc_cidr": "10.0.0.0/16"}
    cfg.update(ecs_cfg)
    return DeployContext(
        project="tenant",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=_services() if services is None else services,
        task_groups=_groups() if groups is None else groups,
        secrets=[],
    )


def _roll(ctx, targets, **kw):
    """Run the force-roll against a mocked ECS client; return its calls."""
    client = mock.MagicMock()
    client.describe_services.return_value = {"services": []}
    provider = ECSProvider()
    provider.session_factory = lambda c: mock.MagicMock(client=lambda s: client)
    provider._watch_post_rollout_errors = lambda *a, **k: None
    provider._wait_for_services_stable = lambda *a, **k: None
    provider._force_new_deployments(ctx, targets, **kw)
    return client.update_service.call_args_list


class TestOneRollPerGroup:
    def test_members_of_one_group_produce_a_single_update_service(self, tmp_path):
        calls = _roll(_ctx(tmp_path), ["nginx", "django", "frontend"])
        assert [c.kwargs["service"] for c in calls] == ["nginx"]

    def test_rolling_every_service_rolls_each_group_once(self, tmp_path):
        calls = _roll(
            _ctx(tmp_path),
            ["nginx", "django", "frontend", "postgres", "redis"],
        )
        assert sorted(c.kwargs["service"] for c in calls) == ["nginx", "postgres"]

    def test_rolling_one_member_rolls_its_whole_group(self, tmp_path):
        """There is no smaller unit: the task is the deployment."""
        calls = _roll(_ctx(tmp_path), ["django"])
        assert [c.kwargs["service"] for c in calls] == ["nginx"]

    def test_force_new_deployment_is_set(self, tmp_path):
        calls = _roll(_ctx(tmp_path), ["django"])
        assert calls[0].kwargs["forceNewDeployment"] is True


class TestGroupOrdering:
    def test_a_group_takes_its_highest_priority_member_type(self, tmp_path):
        """postgres/redis are infrastructure, so their group primes before the
        app group — the cold-start ordering _DEPLOY_ORDER exists for. The nginx
        group ranks as `application` (django/frontend), not `proxy`: a group is
        ordered by its HIGHEST-priority member, since its containers all start
        together anyway."""
        calls = _roll(
            _ctx(tmp_path),
            ["nginx", "django", "frontend", "postgres", "redis"],
        )
        assert [c.kwargs["service"] for c in calls] == ["postgres", "nginx"]

    def test_ordering_is_independent_of_the_order_targets_arrive_in(self, tmp_path):
        calls = _roll(
            _ctx(tmp_path),
            ["frontend", "redis", "nginx", "postgres", "django"],
        )
        assert [c.kwargs["service"] for c in calls] == ["postgres", "nginx"]


class TestGroupRolloutPolicy:
    def test_a_stateless_group_gets_the_zero_downtime_config(self, tmp_path):
        calls = _roll(_ctx(tmp_path), ["django"])
        cfg = calls[0].kwargs["deploymentConfiguration"]
        assert cfg["minimumHealthyPercent"] == 100
        assert cfg["maximumPercent"] == 200
        assert cfg["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}

    def test_a_stateful_group_leaves_the_live_config_alone(self, tmp_path):
        services = _services(
            postgres={"volumes": [{"name": "pgdata", "mount": "/var/lib/pg"}]},
            redis={"stateful": True},
        )
        calls = _roll(_ctx(tmp_path, services), ["postgres"])
        assert "deploymentConfiguration" not in calls[0].kwargs

    def test_members_disagreeing_on_stateful_is_rejected(self, tmp_path):
        """--no-state bypasses terraform, so this is the only place the
        conflict can be caught before it reaches AWS."""
        services = _services(
            postgres={"volumes": [{"name": "pgdata", "mount": "/var/lib/pg"}]}
        )
        with pytest.raises(ProviderConfigError, match="stateful"):
            _roll(_ctx(tmp_path, services), ["postgres", "redis"])

    def test_members_disagreeing_on_deployment_percentages_is_rejected(self, tmp_path):
        services = _services(
            django={
                "deployment": {
                    "minimum_healthy_percent": 50,
                    "maximum_percent": 100,
                }
            }
        )
        with pytest.raises(ProviderConfigError, match="deployment"):
            _roll(_ctx(tmp_path, services), ["nginx", "django"])

    def test_a_uniform_override_is_applied_once_to_the_group(self, tmp_path):
        # replicas=2: at 50/100 with a single replica ECS can neither start a
        # replacement nor stop the old task, and rc already rejects that
        # deadlock (rc-6akx) before it reaches this code.
        override = {"minimum_healthy_percent": 50, "maximum_percent": 100}
        services = _services(
            nginx={"deployment": dict(override), "replicas": 2},
            django={"deployment": dict(override), "replicas": 2},
            frontend={"deployment": dict(override), "replicas": 2},
        )
        calls = _roll(_ctx(tmp_path, services), ["nginx", "django", "frontend"])
        assert len(calls) == 1
        cfg = calls[0].kwargs["deploymentConfiguration"]
        assert cfg["minimumHealthyPercent"] == 50
        assert cfg["maximumPercent"] == 100


class TestReconcileScale:
    def test_desired_count_is_the_groups_replicas(self, tmp_path):
        services = _services(
            nginx={"replicas": 2}, django={"replicas": 2}, frontend={"replicas": 2}
        )
        calls = _roll(
            _ctx(tmp_path, services),
            ["nginx", "django", "frontend"],
            reconcile_scale=True,
        )
        assert calls[0].kwargs["desiredCount"] == 2

    def test_members_disagreeing_on_replicas_is_rejected(self, tmp_path):
        services = _services(django={"replicas": 3})
        with pytest.raises(ProviderConfigError, match="replicas"):
            _roll(
                _ctx(tmp_path, services),
                ["nginx", "django"],
                reconcile_scale=True,
            )

    def test_desired_count_is_absent_without_reconcile_scale(self, tmp_path):
        calls = _roll(_ctx(tmp_path), ["django"])
        assert "desiredCount" not in calls[0].kwargs


class TestUngroupedStackIsUnchanged:
    def test_every_service_still_rolls_on_its_own(self, tmp_path):
        calls = _roll(_ctx(tmp_path, groups={}), ["django", "nginx", "postgres"])
        # _DEPLOY_ORDER: infrastructure 0 < application 1 < worker 2 < proxy 3
        assert [c.kwargs["service"] for c in calls] == [
            "postgres",
            "django",
            "nginx",
        ]

    def test_the_guard_is_gone_from_force_roll(self, tmp_path):
        from remote_compose.provider.ecs import provider as prov

        src = Path(prov.__file__).read_text()
        assert "_reject_grouped_service_lookup" not in src
