"""`rc plan` warning for the hostnames a task group retires (rc-2zzd).

ECS allows exactly one service registry per service, so a group of N containers
registers ONE Cloud Map A record — at the GROUP's name. Every member whose name
is not the group's loses its own hostname.

That is the one part of grouping that is NOT transparent to the application: the
README promises compose hostnames keep resolving, and for merged members it
stops being true. An operator who misses it gets a stack that comes up green and
then fails to connect, so the warning is part of the deliverable rather than a
nicety.
"""

from __future__ import annotations

import pytest

from remote_compose.compose_warnings import (
    collect_compose_warnings,
    detect_task_group_retired_hostnames,
)

pytestmark = pytest.mark.unit


def _compose(**extra_services) -> dict:
    services = {
        "nginx": {"image": "nginx:1"},
        "django": {"image": "dj:1"},
        "frontend": {"image": "fe:1"},
        "postgres": {"image": "postgres:16"},
        "redis": {"image": "redis:7"},
    }
    services.update(extra_services)
    return {"services": services}


def _rc(**extra) -> dict:
    raw = {
        "version": 2,
        "project": "tenant",
        "task_groups": {
            "nginx": {"services": ["nginx", "django", "frontend"]},
            "postgres": {"services": ["postgres", "redis"]},
        },
    }
    raw.update(extra)
    return raw


class TestNoGroupsNoWarning:
    def test_absent_task_groups_is_silent(self):
        assert detect_task_group_retired_hostnames(_compose(), {"project": "t"}) == []

    def test_a_group_of_one_retires_nothing(self):
        raw = _rc(task_groups={"django": {"services": ["django"]}})
        assert detect_task_group_retired_hostnames(_compose(), raw) == []

    def test_a_group_named_after_a_member_keeps_that_members_name(self):
        """The naming lever: only the OTHER members retire."""
        raw = _rc(task_groups={"django": {"services": ["django", "frontend"]}})
        out = detect_task_group_retired_hostnames(_compose(), raw)
        assert len(out) == 1
        assert "frontend" in out[0]
        # django keeps its record, so it must not be listed as retired
        assert "retires" in out[0]
        retired_clause = out[0].split("retires", 1)[1].split(".")[0]
        assert "django" not in retired_clause


class TestNamesTheRetiredHostnames:
    def test_every_retired_member_is_listed(self):
        out = detect_task_group_retired_hostnames(_compose(), _rc())
        joined = "\n".join(out)
        for retired in ("django", "frontend", "redis"):
            assert retired in joined

    def test_the_surviving_name_is_offered_as_the_replacement(self):
        out = detect_task_group_retired_hostnames(_compose(), _rc())
        joined = "\n".join(out)
        assert "tenant.local" in joined

    def test_one_warning_per_group(self):
        assert len(detect_task_group_retired_hostnames(_compose(), _rc())) == 2

    def test_a_group_named_after_no_member_retires_every_member(self):
        raw = _rc(task_groups={"data": {"services": ["postgres", "redis"]}})
        out = detect_task_group_retired_hostnames(_compose(), raw)
        assert "postgres" in out[0] and "redis" in out[0]


class TestNamesTheReferrer:
    """A bare list of retired names is a puzzle. Naming the service that dials
    one turns it into a work item."""

    def test_a_compose_env_var_referencing_a_retired_name_is_flagged(self):
        compose = _compose()
        compose["services"]["django"]["environment"] = {
            "REDIS_URL": "redis://redis:6379/0",
            "UNRELATED": "value",
        }
        out = detect_task_group_retired_hostnames(compose, _rc())
        joined = "\n".join(out)
        assert "REDIS_URL" in joined and "django" in joined

    def test_list_style_environment_is_scanned_too(self):
        compose = _compose()
        compose["services"]["django"]["environment"] = [
            "DATABASE_URL=postgres://u@postgres:5432/db"
        ]
        out = detect_task_group_retired_hostnames(compose, _rc())
        # postgres is the group name, so it SURVIVES — nothing to flag
        assert all("DATABASE_URL" not in w for w in out)

    def test_a_reference_to_a_surviving_name_is_not_flagged(self):
        compose = _compose()
        compose["services"]["frontend"]["environment"] = {
            "API": "http://nginx:80",
        }
        out = detect_task_group_retired_hostnames(compose, _rc())
        assert all("API" not in w for w in out)

    def test_a_reference_from_inside_the_same_group_is_still_flagged(self):
        """Localhost would work, but only after the app is changed — the
        hostname itself stops resolving either way."""
        compose = _compose()
        compose["services"]["nginx"]["environment"] = {
            "UPSTREAM": "http://django:8000",
        }
        out = detect_task_group_retired_hostnames(compose, _rc())
        assert "UPSTREAM" in "\n".join(out)

    def test_substring_matches_do_not_false_positive(self):
        compose = _compose()
        compose["services"]["nginx"]["environment"] = {
            "HOST": "redis-cluster.example.com",
        }
        out = detect_task_group_retired_hostnames(compose, _rc())
        assert all("HOST" not in w for w in out)


class TestWiredIntoRcPlan:
    def test_the_detector_runs_from_collect_compose_warnings(self, tmp_path):
        import yaml

        compose_path = tmp_path / "docker-compose.yml"
        compose = _compose()
        compose["services"]["django"]["environment"] = {
            "REDIS_URL": "redis://redis:6379/0"
        }
        compose_path.write_text(yaml.safe_dump(compose))
        warnings = collect_compose_warnings(compose_path, _rc())
        assert any("retires" in w for w in warnings)

    def test_an_ungrouped_stack_gets_no_such_warning(self, tmp_path):
        import yaml

        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(yaml.safe_dump(_compose()))
        warnings = collect_compose_warnings(compose_path, {"project": "tenant"})
        assert all("retires" not in w for w in warnings)


class TestMalformedInputIsTolerated:
    """This runs on every `rc plan`; a traceback here would block a deploy over
    a warning."""

    @pytest.mark.parametrize(
        "raw",
        [
            {"task_groups": "nope"},
            {"task_groups": {"g": "nope"}},
            {"task_groups": {"g": {"services": "nope"}}},
            {"task_groups": {"g": {}}},
            {"task_groups": {"g": {"services": [None, 7]}}},
        ],
    )
    def test_malformed_task_groups_do_not_raise(self, raw):
        assert detect_task_group_retired_hostnames(_compose(), raw) == []

    def test_a_member_missing_from_compose_is_skipped(self):
        raw = _rc(task_groups={"nginx": {"services": ["nginx", "ghost"]}})
        out = detect_task_group_retired_hostnames(_compose(), raw)
        assert out == [] or "ghost" not in "\n".join(out)
