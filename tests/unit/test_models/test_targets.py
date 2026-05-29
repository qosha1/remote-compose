"""
Unit tests for DeploymentTarget model.
"""

import pytest

from remote_compose.models import DeploymentTarget


@pytest.mark.django_db
class TestDeploymentTarget:

    def test_create_target(self):
        """Test creating a deployment target."""
        target = DeploymentTarget.objects.create(
            name="test-target",
            host="192.168.1.100",
            port=22,
            username="ubuntu",
            target_type=DeploymentTarget.TargetType.SSH,
            environment=DeploymentTarget.Environment.DEVELOPMENT,
        )

        assert target.id is not None
        assert target.name == "test-target"
        assert target.host == "192.168.1.100"
        assert target.port == 22
        assert target.username == "ubuntu"
        assert target.is_active is True
        assert target.health_status == DeploymentTarget.HealthStatus.UNKNOWN

    def test_connection_string_ssh(self):
        """Test SSH connection string generation."""
        target = DeploymentTarget(
            name="test",
            host="example.com",
            port=22,
            username="ubuntu",
            target_type=DeploymentTarget.TargetType.SSH,
        )

        assert target.connection_string == "ssh://ubuntu@example.com:22"

    def test_connection_string_tcp(self):
        """Test TCP connection string generation."""
        target = DeploymentTarget(
            name="test",
            host="example.com",
            port=2375,
            target_type=DeploymentTarget.TargetType.TCP,
        )

        assert target.connection_string == "tcp://example.com:2375"

    def test_connection_string_unix(self):
        """Test Unix socket connection string generation."""
        target = DeploymentTarget(
            name="test",
            host="/var/run/docker.sock",
            target_type=DeploymentTarget.TargetType.UNIX,
        )

        assert target.connection_string == "unix:///var/run/docker.sock"

    @pytest.mark.django_db
    def test_mark_healthy(self):
        """Test marking target as healthy."""
        target = DeploymentTarget.objects.create(
            name="test-target",
            host="192.168.1.100",
            health_status=DeploymentTarget.HealthStatus.UNKNOWN,
        )

        target.mark_healthy()
        target.refresh_from_db()

        assert target.health_status == DeploymentTarget.HealthStatus.HEALTHY
        assert target.last_health_check is not None

    @pytest.mark.django_db
    def test_mark_unhealthy(self):
        """Test marking target as unhealthy."""
        target = DeploymentTarget.objects.create(
            name="test-target",
            host="192.168.1.100",
            health_status=DeploymentTarget.HealthStatus.HEALTHY,
        )

        target.mark_unhealthy()
        target.refresh_from_db()

        assert target.health_status == DeploymentTarget.HealthStatus.UNHEALTHY
        assert target.last_health_check is not None

    @pytest.mark.django_db
    def test_unique_name_constraint(self):
        """Test that target names must be unique."""
        DeploymentTarget.objects.create(
            name="unique-target",
            host="192.168.1.100",
        )

        with pytest.raises(Exception):  # IntegrityError
            DeploymentTarget.objects.create(
                name="unique-target",
                host="192.168.1.101",
            )
