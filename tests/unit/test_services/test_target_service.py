"""
Unit tests for TargetService.
"""

import pytest

from remote_compose.services import TargetService
from remote_compose.models import DeploymentTarget
from remote_compose.exceptions import ValidationError, SSHConnectionError


@pytest.mark.django_db
class TestTargetService:

    @pytest.fixture
    def service(self):
        return TargetService()

    @pytest.fixture
    def mock_ssh_client(self, mocker):
        """Mock SSH client that returns successful connection."""
        mock = mocker.patch("remote_compose.services.target_service.SSHClient")
        instance = mock.return_value
        instance.test_connection.return_value = (True, "Connection successful")
        return mock

    def test_create_target_without_validation(self, service):
        """Test creating target without connection validation."""
        target = service.create_target(
            name="test-target",
            host="192.168.1.100",
            username="ubuntu",
            port=22,
            environment=DeploymentTarget.Environment.DEVELOPMENT,
            validate_connection=False,
        )

        assert target.id is not None
        assert target.name == "test-target"
        assert target.host == "192.168.1.100"
        assert target.health_status == DeploymentTarget.HealthStatus.UNKNOWN

    def test_create_target_with_validation(self, service, mock_ssh_client):
        """Test creating target with connection validation."""
        target = service.create_target(
            name="validated-target",
            host="192.168.1.100",
            username="ubuntu",
            validate_connection=True,
        )

        assert target.id is not None
        assert target.health_status == DeploymentTarget.HealthStatus.HEALTHY
        mock_ssh_client.assert_called()

    def test_create_target_validation_fails(self, service, mocker):
        """Test creating target when validation fails."""
        mock = mocker.patch("remote_compose.services.target_service.SSHClient")
        mock.return_value.test_connection.return_value = (False, "Connection refused")

        with pytest.raises(SSHConnectionError):
            service.create_target(
                name="fail-target",
                host="192.168.1.100",
                validate_connection=True,
            )

    def test_get_target_by_name(self, service):
        """Test getting target by name."""
        created = service.create_target(
            name="get-by-name",
            host="192.168.1.100",
            validate_connection=False,
        )

        target = service.get_target_by_name("get-by-name")

        assert target.id == created.id

    def test_get_target_by_name_not_found(self, service):
        """Test getting non-existent target."""
        with pytest.raises(ValidationError):
            service.get_target_by_name("nonexistent")

    def test_update_target(self, service):
        """Test updating target."""
        target = service.create_target(
            name="update-test",
            host="192.168.1.100",
            validate_connection=False,
        )

        updated = service.update_target(
            target,
            host="192.168.1.200",
            description="Updated description",
        )

        assert updated.host == "192.168.1.200"
        assert updated.description == "Updated description"

    def test_delete_target(self, service):
        """Test deleting target."""
        target = service.create_target(
            name="delete-test",
            host="192.168.1.100",
            validate_connection=False,
        )
        target_id = target.id

        result = service.delete_target(target)

        assert result is True
        assert not DeploymentTarget.objects.filter(id=target_id).exists()

    def test_list_targets(self, service):
        """Test listing targets."""
        service.create_target(
            name="list-dev",
            host="192.168.1.100",
            environment=DeploymentTarget.Environment.DEVELOPMENT,
            validate_connection=False,
        )
        service.create_target(
            name="list-prod",
            host="192.168.1.101",
            environment=DeploymentTarget.Environment.PRODUCTION,
            validate_connection=False,
        )

        all_targets = service.list_targets()
        dev_targets = service.list_targets(environment="development")
        prod_targets = service.list_targets(environment="production")

        assert all_targets.count() >= 2
        assert dev_targets.count() >= 1
        assert prod_targets.count() >= 1

    def test_test_connection(self, service, mock_ssh_client):
        """Test testing target connection."""
        target = service.create_target(
            name="connection-test",
            host="192.168.1.100",
            validate_connection=False,
        )

        result = service.test_connection(target)

        assert result["success"] is True
        assert target.health_status == DeploymentTarget.HealthStatus.HEALTHY

    def test_test_connection_failure(self, service, mocker):
        """Test testing target connection when it fails."""
        mock = mocker.patch("remote_compose.services.target_service.SSHClient")
        mock.return_value.test_connection.return_value = (False, "Timeout")

        target = service.create_target(
            name="fail-connection-test",
            host="192.168.1.100",
            validate_connection=False,
        )

        result = service.test_connection(target)

        assert result["success"] is False
        assert target.health_status == DeploymentTarget.HealthStatus.UNHEALTHY

    def test_get_ssh_client(self, service, mocker):
        """Test getting SSH client for target."""
        mock = mocker.patch("remote_compose.services.target_service.SSHClient")

        target = service.create_target(
            name="ssh-client-test",
            host="192.168.1.100",
            username="testuser",
            port=2222,
            validate_connection=False,
        )

        service.get_ssh_client(target)

        mock.assert_called_once_with(
            host="192.168.1.100",
            port=2222,
            username="testuser",
            key_content=None,
        )
