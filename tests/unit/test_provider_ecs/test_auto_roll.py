"""Per-service auto_roll opt-out (rc-7ga).

rc deploy force-rolls every service with a build context. For a single-task
EFS service (postgres) that roll opens a Cloud Map DNS gap on every deploy
(dependents get [Errno -2]). services.<svc>.auto_roll=false excludes a service
from the DEFAULT build+force-roll set (terraform still manages it); an explicit
--services filter naming it overrides the opt-out so deliberate rolls still
work.

GENERAL + opt-in + strictly ADDITIVE: auto_roll defaults True, so the default
build set is unchanged.
"""

from __future__ import annotations

from remote_compose.provider import ServiceSpec
from remote_compose.provider.ecs.provider import _services_to_build


def _svc(name: str, auto_roll: bool = True) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        cpu=256,
        memory=512,
        type="application",
        build_context="/src/" + name,
        auto_roll=auto_roll,
    )


def test_auto_roll_false_excluded_from_default_build_set():
    services = {"django": _svc("django"), "postgres": _svc("postgres", auto_roll=False)}
    names = [s.name for s in _services_to_build(services)]
    assert "django" in names
    assert "postgres" not in names


def test_explicit_filter_overrides_auto_roll_false():
    services = {"django": _svc("django"), "postgres": _svc("postgres", auto_roll=False)}
    names = [s.name for s in _services_to_build(services, services_filter={"postgres"})]
    assert names == ["postgres"]


def test_auto_roll_defaults_true_included():
    services = {"web": _svc("web")}
    assert [s.name for s in _services_to_build(services)] == ["web"]
