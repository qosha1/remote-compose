"""Resolving an rc.yml service name to its live ECS service under grouping (rc-ib01.2).

``emit_terraform`` renders one ECS service per GROUP, so a member has no service
of its own — it is a container inside its group's task. Every path that looks a
service up by name has to go through the member->group indirection, and every
path that builds a LIST of service names has to dedupe, or N members of one
group produce N calls against the same service.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.config.v2_schema import TaskGroupV2
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import (
    _ecs_service_name,
    _ecs_service_names,
    _member_to_group,
)

pytestmark = pytest.mark.unit


def _svc(name: str, **kw) -> ServiceSpec:
    kw.setdefault("cpu", 256)
    kw.setdefault("memory", 512)
    return ServiceSpec(name=name, **kw)


def _services() -> dict[str, ServiceSpec]:
    return {
        "nginx": _svc("nginx", public=True, port=80),
        "django": _svc("django", port=8000),
        "frontend": _svc("frontend", port=3000),
        "postgres": _svc("postgres", port=5432),
        "redis": _svc("redis", port=6379),
    }


def _groups() -> dict[str, TaskGroupV2]:
    return {
        "nginx": TaskGroupV2(name="nginx", services=["nginx", "django", "frontend"]),
        "postgres": TaskGroupV2(name="postgres", services=["postgres", "redis"]),
    }


def _ctx(tmp_path: Path, *, grouped: bool = True, **ecs_cfg) -> DeployContext:
    cfg = {"region": "us-west-2", "cluster": "t-cluster", "vpc_cidr": "10.0.0.0/16"}
    cfg.update(ecs_cfg)
    return DeployContext(
        project="tenant",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=_services(),
        task_groups=_groups() if grouped else {},
        secrets=[],
    )


class TestMemberToGroup:
    def test_maps_every_member_to_its_group(self, tmp_path):
        assert _member_to_group(_ctx(tmp_path)) == {
            "nginx": "nginx",
            "django": "nginx",
            "frontend": "nginx",
            "postgres": "postgres",
            "redis": "postgres",
        }

    def test_ungrouped_stack_maps_nothing(self, tmp_path):
        assert _member_to_group(_ctx(tmp_path, grouped=False)) == {}


class TestEcsServiceName:
    def test_member_resolves_to_its_group(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert _ecs_service_name(ctx, "django") == "nginx"
        assert _ecs_service_name(ctx, "redis") == "postgres"

    def test_group_name_resolves_to_itself(self, tmp_path):
        assert _ecs_service_name(_ctx(tmp_path), "nginx") == "nginx"

    def test_ungrouped_service_is_unchanged(self, tmp_path):
        assert _ecs_service_name(_ctx(tmp_path, grouped=False), "django") == "django"

    def test_the_service_prefix_still_applies(self, tmp_path):
        ctx = _ctx(tmp_path, service_name_prefix="acme-")
        assert _ecs_service_name(ctx, "django") == "acme-nginx"

    def test_an_unknown_name_passes_through(self, tmp_path):
        """Pre-existing behaviour: rc does not own every name it is handed."""
        assert _ecs_service_name(_ctx(tmp_path), "mystery") == "mystery"


class TestEcsServiceNamesDedupes:
    def test_members_of_one_group_collapse_to_one_name(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert _ecs_service_names(ctx, ["nginx", "django", "frontend"]) == ["nginx"]

    def test_order_is_preserved_by_first_appearance(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert _ecs_service_names(ctx, ["redis", "django", "postgres"]) == [
            "postgres",
            "nginx",
        ]

    def test_ungrouped_stack_is_a_passthrough(self, tmp_path):
        ctx = _ctx(tmp_path, grouped=False)
        assert _ecs_service_names(ctx, ["django", "redis"]) == ["django", "redis"]


class TestStatusReportsGroups:
    """An ECS service's desired/running counts belong to the TASK, so a grouped
    stack reports one entry per group. Reporting per member would look up a
    service that does not exist and call every container 'service not found'."""

    @staticmethod
    def _client(describe_payload):
        client = mock.MagicMock()
        client.describe_services.return_value = describe_payload
        client.describe_task_definition.return_value = {
            "taskDefinition": {"revision": 4}
        }
        return client

    def _run(self, tmp_path, grouped: bool):
        payload = {
            "services": [
                {
                    "serviceName": "nginx",
                    "runningCount": 1,
                    "desiredCount": 1,
                    "taskDefinition": "arn:aws:ecs:r:a:task-definition/tenant-nginx:4",
                    "events": [],
                },
                {
                    "serviceName": "postgres",
                    "runningCount": 1,
                    "desiredCount": 1,
                    "taskDefinition": (
                        "arn:aws:ecs:r:a:task-definition/tenant-postgres:4"
                    ),
                    "events": [],
                },
            ]
        }
        client = self._client(payload)
        provider = ECSProvider()
        provider.session_factory = lambda ctx: mock.MagicMock(client=lambda svc: client)
        ctx = _ctx(tmp_path, grouped=grouped)
        return provider.status(ctx), client

    def test_one_status_entry_per_group(self, tmp_path):
        report, _ = self._run(tmp_path, grouped=True)
        assert sorted(s.name for s in report.services) == ["nginx", "postgres"]

    def test_no_member_is_reported_as_missing(self, tmp_path):
        report, _ = self._run(tmp_path, grouped=True)
        assert all(s.last_event != "service not found" for s in report.services)
        assert all(s.health != "unknown" for s in report.services)

    def test_describe_services_is_called_with_deduped_names(self, tmp_path):
        _, client = self._run(tmp_path, grouped=True)
        names = client.describe_services.call_args.kwargs["services"]
        assert names == ["nginx", "postgres"]

    def test_task_definition_family_is_the_groups(self, tmp_path):
        _, client = self._run(tmp_path, grouped=True)
        families = {
            c.kwargs["taskDefinition"]
            for c in client.describe_task_definition.call_args_list
        }
        assert families == {"tenant-nginx", "tenant-postgres"}


class TestUngroupedPathsAreUnchanged:
    def test_exec_and_run_no_longer_refuse_a_grouped_stack(self, tmp_path):
        """The rc-ib01.2 guard covered exec/run only because they could not
        resolve a member. They can now, so the guard must be gone from them."""
        from remote_compose.provider.ecs import provider as prov

        src = Path(prov.__file__).read_text()
        assert '_reject_grouped_service_lookup(ctx, "rc exec")' not in src
        assert '_reject_grouped_service_lookup(ctx, "rc run")' not in src


class TestRawDictHelpers:
    """`rc db` talks to AWS without ever building a DeployContext, so it needs
    the same indirection off the raw rc.yml mapping."""

    RAW = {
        "task_groups": {
            "nginx": {"services": ["nginx", "django", "frontend"]},
            "postgres": {"services": ["postgres", "redis"]},
        }
    }

    def test_member_resolves_to_its_group(self):
        from remote_compose.config.v2_schema import group_for_service

        assert group_for_service(self.RAW, "redis") == "postgres"
        assert group_for_service(self.RAW, "django") == "nginx"

    def test_group_name_and_unknown_names_pass_through(self):
        from remote_compose.config.v2_schema import group_for_service

        assert group_for_service(self.RAW, "postgres") == "postgres"
        assert group_for_service(self.RAW, "mystery") == "mystery"

    @pytest.mark.parametrize(
        "raw", [None, {}, {"task_groups": None}, {"task_groups": "nope"}, "junk"]
    )
    def test_malformed_or_absent_block_falls_back_to_the_name(self, raw):
        """This runs on the way to a psql prompt; a traceback there is worse
        than using the name the user typed."""
        from remote_compose.config.v2_schema import group_for_service

        assert group_for_service(raw, "postgres") == "postgres"

    def test_container_is_selected_by_name_not_position(self):
        from remote_compose.config.v2_schema import container_named

        cdefs = [
            {"name": "nginx", "environment": [{"name": "X", "value": "1"}]},
            {
                "name": "postgres",
                "environment": [{"name": "POSTGRES_PORT", "value": "6000"}],
            },
        ]
        assert container_named(cdefs, "postgres")["name"] == "postgres"

    def test_falls_back_to_the_first_container_when_unnamed(self):
        """An adopted or hand-written task def may not use rc's names."""
        from remote_compose.config.v2_schema import container_named

        cdefs = [{"name": "app"}, {"name": "sidecar"}]
        assert container_named(cdefs, "postgres")["name"] == "app"

    @pytest.mark.parametrize("cdefs", [None, [], "junk"])
    def test_missing_container_definitions_yield_an_empty_mapping(self, cdefs):
        from remote_compose.config.v2_schema import container_named

        assert container_named(cdefs, "postgres") == {}


class TestRunOneOffOnAGroupedTask:
    """ECS run_task starts a TASK, not a container: every container in the task
    def comes up and containerOverrides only changes the command of the named
    one."""

    @staticmethod
    def _provider(emitted):
        provider = ECSProvider()
        provider._emit = lambda msg: emitted.append(msg)
        return provider

    def _stateful_ctx(self, tmp_path):
        services = _services()
        services["postgres"].volumes = [{"name": "pgdata", "mount": "/var/lib/pg"}]
        services["redis"].stateful = True
        return DeployContext(
            project="tenant",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={"ecs": {"region": "us-west-2", "cluster": "c"}},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services=services,
            task_groups=_groups(),
            secrets=[],
        )

    def test_a_member_of_a_stateful_group_is_refused(self, tmp_path):
        """Would start a SECOND postgres against the same EFS access point —
        the split-brain, arriving through rc run instead of through a roll."""
        from remote_compose.provider.base import ProviderConfigError

        provider = self._provider([])
        with pytest.raises(ProviderConfigError, match="second copy"):
            provider._check_run_one_off_group(self._stateful_ctx(tmp_path), "redis")

    def test_a_stateless_group_warns_about_the_whole_task_starting(self, tmp_path):
        emitted: list[str] = []
        self._provider(emitted)._check_run_one_off_group(_ctx(tmp_path), "django")
        assert emitted and "whole task" in emitted[0]
        assert "nginx" in emitted[0] and "frontend" in emitted[0]

    def test_an_ungrouped_service_neither_warns_nor_raises(self, tmp_path):
        emitted: list[str] = []
        self._provider(emitted)._check_run_one_off_group(
            _ctx(tmp_path, grouped=False), "django"
        )
        assert emitted == []
