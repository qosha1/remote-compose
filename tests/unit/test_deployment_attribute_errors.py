"""Regression tests for the three Deployment-model AttributeErrors
(remote-compose-mps).

1. ``Deployment.Status.IN_PROGRESS`` doesn't exist (should be RUNNING).
2. ``deployment.duration = X`` blows up because duration is a @property.
3. ``log.level`` is wrong — DeploymentLog field is ``log_level``.

The fix requires the source-side names to match the model. These tests
assert the model surface so that any future rename gets caught at the
test layer instead of crashing at runtime.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Bug 1: Status enum sanity
# ---------------------------------------------------------------------------


class TestDeploymentStatusEnum:
    def test_running_exists(self):
        from remote_compose.models import Deployment

        # The valid name. Code referenced this but used IN_PROGRESS.
        assert hasattr(Deployment.Status, "RUNNING")
        assert Deployment.Status.RUNNING == "running"

    def test_in_progress_does_not_exist(self):
        from remote_compose.models import Deployment

        # If someone re-adds IN_PROGRESS, the codebase should rename
        # uses to RUNNING (or both, but with explicit aliasing).
        assert not hasattr(Deployment.Status, "IN_PROGRESS")


# ---------------------------------------------------------------------------
# Bug 2: duration is a read-only @property
# ---------------------------------------------------------------------------


class TestDeploymentDurationIsReadonly:
    def test_duration_is_a_property_not_a_field(self):
        from remote_compose.models import Deployment

        # If duration were a regular descriptor (like a Django field),
        # ``__class__.duration`` would be a ``DeferredAttribute``. Its
        # being a property means assignment raises AttributeError.
        attr = Deployment.__dict__.get("duration")
        assert isinstance(attr, property), (
            "Deployment.duration is documented as computed from "
            "completed_at - started_at. If you turn it into a stored "
            "field, update ecs_deployment_service.update() too."
        )


# ---------------------------------------------------------------------------
# Bug 3: DeploymentLog field is log_level, not level
# ---------------------------------------------------------------------------


class TestDeploymentLogFieldName:
    def test_log_level_field_exists(self):
        from remote_compose.models import DeploymentLog

        # Django model class lets us inspect _meta.get_fields().
        names = {f.name for f in DeploymentLog._meta.get_fields()}
        assert "log_level" in names
        assert "level" not in names, (
            "If you rename log_level → level, update ecs_service "
            "management command (line 237) and any other readers."
        )


# ---------------------------------------------------------------------------
# Code-shape regression: verify the source-side fixes are in place.
# ---------------------------------------------------------------------------


class TestSourceSideFixes:
    """Belt-and-suspenders: grep the source for the broken patterns. If
    a future revert reintroduces them, this test fails before the
    integration test would crash at runtime."""

    def test_no_in_progress_reference_in_deployment_service(self):
        path = "remote_compose/services/ecs_deployment_service.py"
        with open(path) as f:
            text = f.read()
        assert "Deployment.Status.IN_PROGRESS" not in text

    def test_no_assignment_to_deployment_dot_duration(self):
        # We allow ``deployment._duration = ...`` (dead but harmless),
        # but ``deployment.duration = ...`` raises AttributeError.
        path = "remote_compose/services/ecs_deployment_service.py"
        with open(path) as f:
            text = f.read()
        assert "deployment.duration =" not in text

    def test_management_command_uses_log_level(self):
        path = "remote_compose/management/commands/ecs_service.py"
        with open(path) as f:
            text = f.read()
        assert "log.log_level" in text
        # Make sure we didn't leave a stray log.level (regex-aware:
        # accept ``log_level`` substring overlap).
        for line in text.splitlines():
            stripped = line.lstrip()
            if "log.level" in stripped and "log.log_level" not in stripped:
                pytest.fail(f"stray log.level usage: {stripped}")
