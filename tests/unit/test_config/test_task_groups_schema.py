"""rc.yml v2 ``task_groups:`` schema + merged-set reference validation (rc-l6l8).

Two layers, split for the same reason ``validate_network_refs`` is:

  * ``parse()`` validates STRUCTURE only. It cannot check membership, because
    ``build_deploy_context`` resolves the deploy set as
    ``compose_names | rc_names`` — a group may legitimately name a service that
    exists only in docker-compose.yml and never appears in rc.yml.
  * ``validate_task_groups`` runs against the MERGED specs (the ECS provider
    calls it at emit time) and owns every semantic reject.

The decision this implements is rc-4seu's DESIGN field.
"""

from __future__ import annotations

import pytest

from remote_compose.config._task_group_types import (
    DERIVED_UNIFORM_FIELDS,
    UNIFORM_MEMBER_FIELDS,
)
from remote_compose.config.v2_schema import (
    ConfigError,
    TaskGroupV2,
    parse,
    resolve_task_groups,
    validate_task_groups,
)
from remote_compose.provider import ServiceSpec

pytestmark = pytest.mark.unit


def _cfg(**extra):
    base = {
        "version": 2,
        "project": "p",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
    }
    base.update(extra)
    return base


def _spec(name: str, **kw) -> ServiceSpec:
    kw.setdefault("cpu", 256)
    kw.setdefault("memory", 512)
    return ServiceSpec(name=name, **kw)


def _tenant_specs(**overrides) -> dict[str, ServiceSpec]:
    """The foundry-tenant shape the epic measures against."""
    specs = {
        "nginx": _spec("nginx", memory=512, public=True, port=80),
        "django": _spec("django", memory=2048, port=8000),
        "frontend": _spec("frontend", memory=1024, port=3000),
        "reingest": _spec("reingest", memory=512),
        "postgres": _spec("postgres", memory=1024, port=5432, stateful=True),
        "redis": _spec("redis", memory=512, port=6379, stateful=True),
    }
    for name, kw in overrides.items():
        for k, v in kw.items():
            setattr(specs[name], k, v)
    return specs


def _tenant_groups() -> dict[str, TaskGroupV2]:
    return parse(
        _cfg(
            task_groups={
                "nginx": {"services": ["nginx", "django", "frontend", "reingest"]},
                "postgres": {"services": ["postgres", "redis"]},
            }
        )
    ).task_groups


# ---------------------------------------------------------------------------
# Back-compat: the no-regression guard for every existing rc user
# ---------------------------------------------------------------------------


class TestBackCompat:
    def test_absent_block_defaults_to_empty(self):
        assert parse(_cfg()).task_groups == {}

    def test_empty_block_is_accepted(self):
        assert parse(_cfg(task_groups={})).task_groups == {}

    def test_no_groups_resolves_to_one_group_per_service_named_after_it(self):
        """The implicit group-of-one path. Byte-identical output depends on it."""
        specs = _tenant_specs()
        resolved = resolve_task_groups({}, specs)
        assert sorted(resolved) == sorted(specs)
        for name, group in resolved.items():
            assert group.name == name
            assert group.members == [name]
            assert group.is_implicit

    def test_implicit_group_order_matches_sorted_service_order(self):
        """services.tf.j2 iterates groups; byte-identical needs the same order."""
        specs = _tenant_specs()
        assert list(resolve_task_groups({}, specs)) == sorted(specs)


# ---------------------------------------------------------------------------
# Structure (parse time)
# ---------------------------------------------------------------------------


class TestParseStructure:
    def test_group_round_trips(self):
        cfg = parse(
            _cfg(
                task_groups={
                    "nginx": {
                        "services": ["nginx", "django"],
                        "ingress": "nginx",
                        "memory": 3072,
                    }
                }
            )
        )
        group = cfg.task_groups["nginx"]
        assert group.name == "nginx"
        assert group.services == ["nginx", "django"]
        assert group.ingress == "nginx"
        assert group.memory == 3072

    def test_ingress_and_memory_default_to_none(self):
        group = parse(_cfg(task_groups={"g": {"services": ["a", "b"]}})).task_groups[
            "g"
        ]
        assert group.ingress is None
        assert group.memory is None

    def test_block_must_be_a_mapping(self):
        with pytest.raises(ConfigError, match="task_groups must be a mapping"):
            parse(_cfg(task_groups=["nginx"]))

    def test_group_body_must_be_a_mapping(self):
        with pytest.raises(ConfigError, match="must be a mapping"):
            parse(_cfg(task_groups={"g": ["a", "b"]}))

    def test_unknown_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown task_groups.g keys"):
            parse(_cfg(task_groups={"g": {"services": ["a"], "replicas": 2}}))

    def test_services_is_required(self):
        with pytest.raises(ConfigError, match="requires 'services'"):
            parse(_cfg(task_groups={"g": {"ingress": "a"}}))

    def test_services_must_be_a_non_empty_list(self):
        with pytest.raises(ConfigError, match="non-empty list"):
            parse(_cfg(task_groups={"g": {"services": []}}))

    def test_services_entries_must_be_strings(self):
        with pytest.raises(ConfigError, match="service names"):
            parse(_cfg(task_groups={"g": {"services": ["a", 7]}}))

    def test_duplicate_member_within_a_group_rejected(self):
        with pytest.raises(ConfigError, match="lists 'a' twice"):
            parse(_cfg(task_groups={"g": {"services": ["a", "a"]}}))

    def test_ingress_must_be_a_member(self):
        with pytest.raises(ConfigError, match="ingress 'zzz' is not a member"):
            parse(_cfg(task_groups={"g": {"services": ["a"], "ingress": "zzz"}}))

    def test_memory_must_be_a_positive_int(self):
        with pytest.raises(ConfigError, match="memory must be a positive integer"):
            parse(_cfg(task_groups={"g": {"services": ["a"], "memory": 0}}))

    def test_service_in_two_groups_rejected(self):
        with pytest.raises(ConfigError, match="'shared' is in two task groups"):
            parse(
                _cfg(
                    task_groups={
                        "one": {"services": ["a", "shared"]},
                        "two": {"services": ["b", "shared"]},
                    }
                )
            )

    def test_parse_does_not_reject_a_compose_only_member(self):
        """rc.yml services are a SUBSET of the deploy set — see module docstring."""
        cfg = parse(
            _cfg(
                services={"django": {"cpu": 256, "memory": 512}},
                task_groups={"django": {"services": ["django", "nginx"]}},
            )
        )
        assert cfg.task_groups["django"].services == ["django", "nginx"]


# ---------------------------------------------------------------------------
# Semantics (merged specs)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_the_foundry_tenant_split_validates(self):
        validate_task_groups(_tenant_groups(), _tenant_specs())

    def test_resolution_keeps_declared_member_order(self):
        resolved = resolve_task_groups(_tenant_groups(), _tenant_specs())
        assert resolved["nginx"].members == [
            "nginx",
            "django",
            "frontend",
            "reingest",
        ]

    def test_ungrouped_service_becomes_its_own_group(self):
        specs = _tenant_specs()
        specs["cron"] = _spec("cron")
        groups = parse(
            _cfg(task_groups={"nginx": {"services": ["nginx", "django"]}})
        ).task_groups
        resolved = resolve_task_groups(groups, specs)
        assert resolved["cron"].members == ["cron"]
        assert resolved["cron"].is_implicit
        assert not resolved["nginx"].is_implicit

    def test_group_memory_defaults_to_sum_of_members(self):
        resolved = resolve_task_groups(_tenant_groups(), _tenant_specs())
        # 512 + 2048 + 1024 + 512
        assert resolved["nginx"].memory == 4096
        assert resolved["postgres"].memory == 1536

    def test_declared_group_memory_overrides_the_sum(self):
        groups = parse(
            _cfg(
                task_groups={
                    "postgres": {"services": ["postgres", "redis"], "memory": 1024}
                }
            )
        ).task_groups
        resolved = resolve_task_groups(groups, _tenant_specs())
        assert resolved["postgres"].memory == 1024

    def test_group_of_one_memory_is_the_member_memory(self):
        resolved = resolve_task_groups({}, _tenant_specs())
        assert resolved["django"].memory == 2048


class TestMembershipRejects:
    def test_unknown_member_rejected(self):
        groups = parse(
            _cfg(task_groups={"nginx": {"services": ["nginx", "ghost"]}})
        ).task_groups
        with pytest.raises(ConfigError, match="names service 'ghost', which"):
            validate_task_groups(groups, _tenant_specs())

    def test_group_name_colliding_with_a_non_member_rejected(self):
        groups = parse(
            _cfg(task_groups={"redis": {"services": ["nginx", "django"]}})
        ).task_groups
        with pytest.raises(ConfigError, match="collides with service 'redis'"):
            validate_task_groups(groups, _tenant_specs())

    def test_group_named_after_one_of_its_own_members_is_the_recommended_form(self):
        groups = parse(
            _cfg(task_groups={"nginx": {"services": ["nginx", "django"]}})
        ).task_groups
        validate_task_groups(groups, _tenant_specs())


class TestPortRejects:
    def test_duplicate_primary_port_rejected(self):
        specs = _tenant_specs(frontend={"port": 8000})
        with pytest.raises(ConfigError, match="port 8000"):
            validate_task_groups(_tenant_groups(), specs)

    def test_collision_via_extra_ports_rejected(self):
        """awsvpc forces hostPort == containerPort, so extra_ports collide too."""
        specs = _tenant_specs(reingest={"extra_ports": [3000]})
        with pytest.raises(ConfigError, match="port 3000"):
            validate_task_groups(_tenant_groups(), specs)

    def test_same_port_in_different_groups_is_fine(self):
        specs = _tenant_specs(redis={"port": 80})
        validate_task_groups(_tenant_groups(), specs)


class TestUniformityRejects:
    @pytest.mark.parametrize(
        "field,value,yml_key",
        [
            ("auto_roll", False, "auto_roll"),
            ("replicas", 3, "replicas"),
            ("iam_role", "worker-role", "iam_role"),
            # The message names the rc.yml key an operator would edit
            # (``subnets``), not the ServiceSpec attribute (``subnet_group``).
            ("subnet_group", "private", "subnets"),
            ("ephemeral_storage", 40, "ephemeral_storage"),
        ],
    )
    def test_members_must_agree(self, field, value, yml_key):
        specs = _tenant_specs(django={field: value})
        with pytest.raises(ConfigError, match=yml_key):
            validate_task_groups(_tenant_groups(), specs)

    def test_security_groups_must_agree(self):
        specs = _tenant_specs(django={"security_groups": ["locked-down"]})
        with pytest.raises(ConfigError, match="security_groups"):
            validate_task_groups(_tenant_groups(), specs)

    def test_uniform_non_default_value_is_accepted(self):
        specs = _tenant_specs(
            nginx={"replicas": 2},
            django={"replicas": 2},
            frontend={"replicas": 2},
            reingest={"replicas": 2},
        )
        validate_task_groups(_tenant_groups(), specs)

    def test_the_error_names_both_members_and_the_field(self):
        specs = _tenant_specs(django={"replicas": 3})
        with pytest.raises(ConfigError) as exc:
            validate_task_groups(_tenant_groups(), specs)
        msg = str(exc.value)
        assert "django" in msg and "nginx" in msg and "replicas" in msg

    @pytest.mark.parametrize("field", DERIVED_UNIFORM_FIELDS)
    def test_derived_fields_are_not_checked_here(self, field):
        """launch_type / stateful / deployment are DERIVED, so the rc.yml value
        is not the rendered one. Checking them against the raw spec would both
        miss real conflicts (postgres is stateful via its EFS volume, not via
        the flag) and invent false ones (launch_type is None until
        default_launch_type applies). The ECS provider re-checks all three
        against the computed views — see _validate_group_render_uniformity and
        TestStatefulIsComputedNotDeclared."""
        assert field not in UNIFORM_MEMBER_FIELDS


class TestVolumeRejects:
    def test_two_members_mounting_the_same_volume_name_rejected(self):
        """rc gives each SERVICE its own access point per volume
        (``<service>__<volume>``), so one task cannot carry both under one
        ``volume`` block name."""
        specs = _tenant_specs(
            postgres={"volumes": [{"name": "data", "mount": "/var/lib/postgresql"}]},
            redis={"volumes": [{"name": "data", "mount": "/data"}]},
        )
        with pytest.raises(ConfigError, match="both mount volume 'data'"):
            validate_task_groups(_tenant_groups(), specs)

    def test_same_volume_name_in_different_groups_is_fine(self):
        specs = _tenant_specs(
            postgres={"volumes": [{"name": "data", "mount": "/var/lib/postgresql"}]},
            django={"volumes": [{"name": "data", "mount": "/srv/data"}]},
        )
        validate_task_groups(_tenant_groups(), specs)

    def test_distinct_volume_names_within_a_group_are_fine(self):
        specs = _tenant_specs(
            postgres={"volumes": [{"name": "pgdata", "mount": "/var/lib/postgresql"}]},
            redis={"volumes": [{"name": "redisdata", "mount": "/data"}]},
        )
        validate_task_groups(_tenant_groups(), specs)


class TestEssentialRejects:
    def test_all_members_non_essential_rejected(self):
        """AWS: 'All tasks must have at least one essential container.'"""
        specs = _tenant_specs(
            nginx={"essential": False},
            django={"essential": False},
            frontend={"essential": False},
            reingest={"essential": False},
        )
        with pytest.raises(ConfigError, match="at least one essential container"):
            validate_task_groups(_tenant_groups(), specs)

    def test_some_members_non_essential_is_fine(self):
        specs = _tenant_specs(
            frontend={"essential": False}, reingest={"essential": False}
        )
        validate_task_groups(_tenant_groups(), specs)

    def test_a_lone_non_essential_service_is_still_rejected(self):
        """A group of one is still a task, and a task needs an essential
        container — even the implicit group nobody declared."""
        specs = {"solo": _spec("solo", essential=False)}
        with pytest.raises(ConfigError, match="at least one essential container"):
            validate_task_groups({}, specs)


class TestIngressRejects:
    def test_two_public_members_without_ingress_rejected(self):
        specs = _tenant_specs(django={"public": True})
        with pytest.raises(ConfigError, match="ingress"):
            validate_task_groups(_tenant_groups(), specs)

    def test_two_public_members_with_ingress_accepted(self):
        groups = parse(
            _cfg(
                task_groups={
                    "nginx": {
                        "services": ["nginx", "django", "frontend", "reingest"],
                        "ingress": "nginx",
                    },
                    "postgres": {"services": ["postgres", "redis"]},
                }
            )
        ).task_groups
        specs = _tenant_specs(django={"public": True})
        validate_task_groups(groups, specs)

    def test_ingress_naming_a_member_with_no_port_rejected(self):
        groups = parse(
            _cfg(
                task_groups={
                    "nginx": {
                        "services": ["nginx", "django", "frontend", "reingest"],
                        "ingress": "reingest",
                    }
                }
            )
        ).task_groups
        with pytest.raises(ConfigError, match="declares no port"):
            validate_task_groups(groups, _tenant_specs())

    def test_resolved_group_exposes_the_ingress_container(self):
        resolved = resolve_task_groups(_tenant_groups(), _tenant_specs())
        assert resolved["nginx"].ingress == "nginx"
        assert resolved["postgres"].ingress is None


class TestStrandedDomain:
    """A group gets ONE load_balancer block. A domain on a non-ingress member
    is routed nowhere and parse()'s duplicate-hostname check will not catch it,
    because the hostname is perfectly unique — it simply stops resolving."""

    def _groups(self):
        return parse(
            _cfg(
                task_groups={
                    "nginx": {
                        "services": ["nginx", "django", "frontend", "reingest"],
                        "ingress": "nginx",
                    },
                    "postgres": {"services": ["postgres", "redis"]},
                }
            )
        ).task_groups

    def test_domain_on_a_non_ingress_member_is_rejected(self):
        specs = _tenant_specs(
            nginx={"domain": "acme.example.com"},
            django={"public": True, "domain": "api.example.com"},
        )
        with pytest.raises(ConfigError, match="routed nowhere"):
            validate_task_groups(self._groups(), specs)

    def test_domain_on_the_ingress_member_is_fine(self):
        specs = _tenant_specs(nginx={"domain": "acme.example.com"})
        validate_task_groups(self._groups(), specs)

    def test_domain_on_an_ungrouped_service_is_fine(self):
        specs = _tenant_specs()
        specs["standalone"] = _spec(
            "standalone", public=True, port=8080, domain="solo.example.com"
        )
        validate_task_groups(self._groups(), specs)


class TestExcludedMemberErrorNamesTheCause:
    def test_missing_member_error_mentions_compose_filtering(self):
        """`deploy_names` is filtered by compose.include/exclude BEFORE the
        provider validates, so 'not in compose or rc.yml' is often a lie."""
        groups = parse(
            _cfg(task_groups={"nginx": {"services": ["nginx", "reingest"]}})
        ).task_groups
        specs = {k: v for k, v in _tenant_specs().items() if k != "reingest"}
        with pytest.raises(ConfigError, match="compose.exclude"):
            validate_task_groups(groups, specs)
