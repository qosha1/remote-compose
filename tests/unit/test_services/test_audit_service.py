"""
Unit tests for AuditService.
"""

import pytest
from unittest.mock import MagicMock, patch


from remote_compose.services import AuditService, AuditAction, AuditEntry
from remote_compose.models import AuditLog


class TestAuditEntry:
    """Tests for AuditEntry dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        entry = AuditEntry(
            action="deployment.started",
            actor="testuser",
            timestamp="2024-01-01T00:00:00",
            resource_type="deployment",
            resource_id=1,
            resource_name="test-project",
            details={"version": "v1.0.0"},
        )

        data = entry.to_dict()

        assert data["action"] == "deployment.started"
        assert data["actor"] == "testuser"
        assert data["resource_id"] == 1

    def test_to_json(self):
        """Test conversion to JSON string."""
        entry = AuditEntry(
            action="deployment.started",
            actor="testuser",
            timestamp="2024-01-01T00:00:00",
        )

        json_str = entry.to_json()

        assert "deployment.started" in json_str
        assert "testuser" in json_str


class TestAuditAction:
    """Tests for AuditAction enum."""

    def test_action_values(self):
        """Test that action values are strings."""
        assert AuditAction.DEPLOYMENT_STARTED.value == "deployment.started"
        assert AuditAction.DEPLOYMENT_COMPLETED.value == "deployment.completed"
        assert AuditAction.CREDENTIAL_ACCESSED.value == "credential.accessed"

    def test_all_actions_have_values(self):
        """Test that all actions have string values."""
        for action in AuditAction:
            assert isinstance(action.value, str)
            assert "." in action.value  # Should have category.action format


@pytest.mark.django_db
class TestAuditService:
    """Tests for the AuditService."""

    @pytest.fixture
    def service(self):
        return AuditService()

    def test_log_creates_entry(self, service):
        """Test that log creates an audit entry."""
        with patch.object(AuditLog.objects, "create") as mock_create:
            entry = service.log(
                action=AuditAction.DEPLOYMENT_STARTED,
                actor="testuser",
                resource_type="deployment",
                resource_id=1,
            )

        assert entry.action == "deployment.started"
        assert entry.actor == "testuser"
        mock_create.assert_called_once()

    def test_log_sanitizes_details(self, service):
        """Test that sensitive details are sanitized."""
        with patch.object(AuditLog.objects, "create") as mock_create:
            service.log(
                action=AuditAction.DEPLOYMENT_STARTED,
                actor="testuser",
                details={
                    "password": "secret123",
                    "host": "localhost",
                },
            )

        # The details passed to create should be sanitized
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["details"].get("password") == "***"
        assert call_kwargs["details"].get("host") == "localhost"

    def test_log_deployment_started(self, service):
        """Test convenience method for deployment started."""
        mock_deployment = MagicMock()
        mock_deployment.id = 1
        mock_deployment.project_name = "test-project"
        mock_deployment.target_id = 1
        mock_deployment.target.name = "test-target"
        mock_deployment.version = "v1.0.0"
        mock_deployment.deployment_type = "deploy"

        with patch.object(service, "log") as mock_log:
            service.log_deployment_started(mock_deployment, "testuser")

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[1]["action"] == AuditAction.DEPLOYMENT_STARTED
        assert call_args[1]["resource_type"] == "deployment"

    def test_log_deployment_completed(self, service):
        """Test convenience method for deployment completed."""
        mock_deployment = MagicMock()
        mock_deployment.id = 1
        mock_deployment.project_name = "test-project"
        mock_deployment.target_id = 1
        mock_deployment.target.name = "test-target"
        mock_deployment.version = "v1.0.0"
        mock_deployment.duration = 30.5
        mock_deployment.container_ids = ["abc123"]

        with patch.object(service, "log") as mock_log:
            service.log_deployment_completed(mock_deployment, "testuser")

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[1]["action"] == AuditAction.DEPLOYMENT_COMPLETED

    def test_log_deployment_failed(self, service):
        """Test convenience method for deployment failed."""
        mock_deployment = MagicMock()
        mock_deployment.id = 1
        mock_deployment.project_name = "test-project"
        mock_deployment.target_id = 1
        mock_deployment.target.name = "test-target"
        mock_deployment.version = "v1.0.0"

        with patch.object(service, "log") as mock_log:
            service.log_deployment_failed(
                mock_deployment,
                "testuser",
                "Connection refused",
            )

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[1]["action"] == AuditAction.DEPLOYMENT_FAILED
        assert call_args[1]["success"] is False
        assert call_args[1]["error_message"] == "Connection refused"

    def test_log_credential_access(self, service):
        """Test convenience method for credential access."""
        mock_credential = MagicMock()
        mock_credential.id = 1
        mock_credential.name = "prod-ssh-key"
        mock_credential.credential_type = "ssh_key"

        with patch.object(service, "log") as mock_log:
            service.log_credential_access(mock_credential, "testuser")

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[1]["action"] == AuditAction.CREDENTIAL_ACCESSED

    def test_log_rate_limit_exceeded(self, service):
        """Test convenience method for rate limit exceeded."""
        with patch.object(service, "log") as mock_log:
            service.log_rate_limit_exceeded(
                actor="testuser",
                limit_type="per_target",
                details={"target_id": 1},
            )

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[1]["action"] == AuditAction.RATE_LIMIT_EXCEEDED
        assert call_args[1]["success"] is False

    def test_query_logs(self, service):
        """Test querying audit logs."""
        with patch.object(AuditLog.objects, "all") as mock_all:
            mock_qs = MagicMock()
            mock_all.return_value = mock_qs
            mock_qs.filter.return_value = mock_qs
            mock_qs.__getitem__ = MagicMock(return_value=[])

            service.query_logs(
                action="deployment.started",
                actor="testuser",
                limit=50,
            )

        mock_qs.filter.assert_called()

    def test_get_activity_summary(self, service):
        """Test getting activity summary."""
        mock_logs = MagicMock()
        mock_logs.count.return_value = 10
        mock_logs.filter.return_value.count.return_value = 8
        mock_logs.values.return_value.distinct.return_value.count.return_value = 3
        mock_logs.__iter__ = MagicMock(
            return_value=iter(
                [
                    MagicMock(action="deployment.started"),
                    MagicMock(action="deployment.completed"),
                ]
            )
        )

        with patch.object(AuditLog.objects, "filter", return_value=mock_logs):
            summary = service.get_activity_summary(hours=24)

        assert summary["period_hours"] == 24
        assert "total_events" in summary
        assert "action_counts" in summary

    def test_cleanup_old_logs(self, service):
        """Test cleaning up old logs."""
        with (
            patch.object(AuditLog.objects, "filter") as mock_filter,
            patch.object(service, "log"),
        ):
            mock_qs = MagicMock()
            mock_qs.delete.return_value = (100, {})
            mock_filter.return_value = mock_qs

            deleted = service.cleanup_old_logs(retention_days=90)

        assert deleted == 100


@pytest.mark.django_db
class TestAuditLogModel:
    """Tests for the AuditLog model."""

    def test_create_audit_log(self):
        """Test creating an audit log entry."""
        log = AuditLog.objects.create(
            action="deployment.started",
            actor="testuser",
            resource_type="deployment",
            resource_id=1,
            details={"version": "v1.0.0"},
        )

        assert log.id is not None
        assert log.action == "deployment.started"
        assert log.actor == "testuser"
        assert log.success is True

    def test_audit_log_class_method(self):
        """Test the log class method."""
        log = AuditLog.log(
            action="deployment.completed",
            actor="testuser",
            resource_type="deployment",
            resource_id=1,
            details={"duration": 30},
        )

        assert log.id is not None
        assert log.action == "deployment.completed"

    def test_audit_log_str(self):
        """Test string representation."""
        log = AuditLog(
            action="deployment.started",
            actor="testuser",
        )

        str_repr = str(log)

        assert "deployment.started" in str_repr
        assert "testuser" in str_repr
