"""Tests for orphan-log-group reconcile surfacing (rc-e5u.46.9).

Verified during .46.6 e2e: a pre-existing
/aws/ecs/containerinsights/<cluster>/performance log group must be
imported into terraform state before apply, otherwise apply fails with
ResourceAlreadyExistsException. The earlier revision swallowed every
AWS-side error silently (only printed via the optional progress
callback), which made the failure mode opaque on the user's machine.

These tests use boto3-stub-style mocks: real boto3 is not invoked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from remote_compose.provider.base import DeployContext, ServiceSpec
from remote_compose.provider.ecs.provider import ECSProvider
from remote_compose.terraform.runner import TerraformError


@pytest.fixture
def ctx(tmp_path: Path) -> DeployContext:
    return DeployContext(
        project="testp",
        compose_path=Path("/tmp/dc.yml"),
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-1", "cluster": "testp-cluster"}},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "app": ServiceSpec(name="app", cpu=256, memory=512, type="application"),
        },
        secrets=[],
    )


# ---------------------------------------------------------------------------
# Happy path: orphan exists → import called → progress emitted.
# ---------------------------------------------------------------------------


class TestOrphanFoundAndImported:
    def test_progress_emits_on_successful_import(self, ctx):
        progress_msgs: list[str] = []

        # Mock session/client so describe_log_groups returns the orphan.
        client = MagicMock()
        client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": "/aws/ecs/containerinsights/testp-cluster/performance"}
            ],
        }
        session = MagicMock()
        session.client.return_value = client

        provider = ECSProvider(
            session_factory=lambda c: session,
            progress=progress_msgs.append,
        )
        runner = MagicMock()
        # import_resource succeeds = no exception
        runner.import_resource.return_value = None

        provider._reconcile_orphan_log_groups(ctx, runner)

        runner.import_resource.assert_called_once_with(
            "aws_cloudwatch_log_group.container_insights",
            "/aws/ecs/containerinsights/testp-cluster/performance",
        )
        assert any("imported orphan log group" in m for m in progress_msgs)


# ---------------------------------------------------------------------------
# Already-imported path: terraform says 'already managed' → silently OK.
# ---------------------------------------------------------------------------


class TestImportFailureFallsBackToDelete:
    """When terraform import fails for any reason OTHER than 'already
    managed', the reconcile must fall back to deleting the orphan via
    boto3 — otherwise terraform apply blows up with
    ResourceAlreadyExistsException.

    The trigger we hit in production: --domain wiring adds a
    ``for_each = { ... aws_acm_certificate.main.domain_validation_options
    ... }`` block. terraform import validates the WHOLE module before
    importing, and ``aws_acm_certificate.main.domain_validation_options``
    is unknown-until-apply. Import fails with 'Invalid for_each
    argument'; without the boto3 fallback the next apply dies on the
    pre-existing log group.
    """

    def test_invalid_for_each_failure_triggers_boto3_delete(self, ctx):
        progress_msgs: list[str] = []
        client = MagicMock()
        client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": "/aws/ecs/containerinsights/testp-cluster/performance"}
            ],
        }
        session = MagicMock()
        session.client.return_value = client

        provider = ECSProvider(
            session_factory=lambda c: session,
            progress=progress_msgs.append,
        )
        runner = MagicMock()
        runner.import_resource.side_effect = TerraformError(
            cmd=["terraform", "import"],
            returncode=1,
            stdout="",
            stderr=(
                "Error: Invalid for_each argument\n"
                "  on /tmp/terraform/domain.tf line 25\n"
                "aws_acm_certificate.main.domain_validation_options is "
                "a set of object, known only after apply"
            ),
        )

        provider._reconcile_orphan_log_groups(ctx, runner)

        # Boto3 delete was invoked.
        client.delete_log_group.assert_called_once_with(
            logGroupName="/aws/ecs/containerinsights/testp-cluster/performance",
        )
        assert any("falling back to boto3 delete" in m for m in progress_msgs)
        assert any("deleted orphan log group" in m for m in progress_msgs)

    def test_boto3_delete_failure_emits_actionable_warning(self, ctx):
        progress_msgs: list[str] = []
        client = MagicMock()
        client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": "/aws/ecs/containerinsights/testp-cluster/performance"}
            ],
        }
        client.delete_log_group.side_effect = RuntimeError(
            "AccessDenied: logs:DeleteLogGroup",
        )
        session = MagicMock()
        session.client.return_value = client

        provider = ECSProvider(
            session_factory=lambda c: session,
            progress=progress_msgs.append,
        )
        runner = MagicMock()
        runner.import_resource.side_effect = TerraformError(
            cmd=["terraform", "import"],
            returncode=1,
            stdout="",
            stderr="Error: Invalid for_each argument",
        )

        provider._reconcile_orphan_log_groups(ctx, runner)

        assert any("AccessDenied" in m for m in progress_msgs)
        # Surfaces a copy-pasteable manual recovery command.
        assert any("aws logs delete-log-group" in m for m in progress_msgs)


class TestOrphanAlreadyImported:
    def test_already_managed_swallowed(self, ctx):
        progress_msgs: list[str] = []

        client = MagicMock()
        client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": "/aws/ecs/containerinsights/testp-cluster/performance"}
            ],
        }
        session = MagicMock()
        session.client.return_value = client

        provider = ECSProvider(
            session_factory=lambda c: session,
            progress=progress_msgs.append,
        )
        runner = MagicMock()
        runner.import_resource.side_effect = TerraformError(
            cmd=["terraform", "import"],
            returncode=1,
            stdout="",
            stderr="Resource is already managed by Terraform.",
        )

        provider._reconcile_orphan_log_groups(ctx, runner)

        # Already-managed → no warning printed (idempotent).
        assert not any("warning" in m for m in progress_msgs)
        assert not any("imported orphan" in m for m in progress_msgs)


# ---------------------------------------------------------------------------
# Silent-fail surfacing (the .46.9 fix): AWS describe failure now visible.
# ---------------------------------------------------------------------------


class TestAWSDescribeFailureSurfaces:
    def test_no_credentials_error_surfaces_via_progress(self, ctx):
        progress_msgs: list[str] = []

        # session_factory raises (e.g. NoCredentialsError) — earlier revision
        # silently returned. .46.9 surfaces this so the user can fix creds.
        def boom(_ctx):
            raise RuntimeError("Unable to locate credentials")

        provider = ECSProvider(
            session_factory=boom,
            progress=progress_msgs.append,
        )
        runner = MagicMock()

        provider._reconcile_orphan_log_groups(ctx, runner)

        # Visible warning + does NOT raise (so apply still gets a chance).
        assert any("orphan log-group reconcile skipped" in m for m in progress_msgs)
        assert any("Unable to locate credentials" in m for m in progress_msgs)
        runner.import_resource.assert_not_called()

    def test_describe_log_groups_failure_surfaces(self, ctx):
        progress_msgs: list[str] = []

        client = MagicMock()
        client.describe_log_groups.side_effect = RuntimeError(
            "AccessDenied: logs:DescribeLogGroups"
        )
        session = MagicMock()
        session.client.return_value = client

        provider = ECSProvider(
            session_factory=lambda c: session,
            progress=progress_msgs.append,
        )
        runner = MagicMock()

        provider._reconcile_orphan_log_groups(ctx, runner)

        assert any("AccessDenied" in m for m in progress_msgs)
        runner.import_resource.assert_not_called()


# ---------------------------------------------------------------------------
# No orphan exists: silent return is correct (don't bother the user).
# ---------------------------------------------------------------------------


class TestNoOrphanFound:
    def test_no_log_group_means_no_warning(self, ctx):
        progress_msgs: list[str] = []

        client = MagicMock()
        client.describe_log_groups.return_value = {"logGroups": []}
        session = MagicMock()
        session.client.return_value = client

        provider = ECSProvider(
            session_factory=lambda c: session,
            progress=progress_msgs.append,
        )
        runner = MagicMock()

        provider._reconcile_orphan_log_groups(ctx, runner)

        assert progress_msgs == []
        runner.import_resource.assert_not_called()


# ---------------------------------------------------------------------------
# _emit fallback: stderr when no progress callback is set.
# ---------------------------------------------------------------------------


class TestEmitFallback:
    def test_emit_falls_back_to_stderr_without_progress(self, capsys):
        provider = ECSProvider()  # no progress callback
        provider._emit("hello world")

        captured = capsys.readouterr()
        assert "hello world" in captured.err

    def test_emit_uses_progress_when_set(self, capsys):
        msgs: list[str] = []
        provider = ECSProvider(progress=msgs.append)
        provider._emit("hello world")

        assert msgs == ["hello world"]
        # And NOT to stderr — progress is the sole sink when present.
        captured = capsys.readouterr()
        assert "hello world" not in captured.err
