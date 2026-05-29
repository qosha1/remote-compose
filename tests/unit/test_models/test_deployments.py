"""
Unit tests for Deployment and DeploymentLog models.
"""

import pytest
from django.utils import timezone

from remote_compose.models import (
    Deployment,
    DeploymentLog,
    DeploymentTarget,
    DockerContext,
)


@pytest.mark.django_db
class TestDeployment:

    @pytest.fixture
    def target(self):
        return DeploymentTarget.objects.create(
            name="test-target",
            host="192.168.1.100",
        )

    @pytest.fixture
    def context(self, target):
        return DockerContext.objects.create(
            name="test-context",
            target=target,
            context_type=DockerContext.ContextType.SSH,
            endpoint=f"ssh://ubuntu@{target.host}:22",
        )

    def test_create_deployment(self, target, context):
        """Test creating a deployment."""
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/docker-compose.yml",
            compose_content='version: "3.8"',
            project_name="test-project",
        )

        assert deployment.id is not None
        assert deployment.status == Deployment.Status.PENDING
        assert deployment.deployment_type == Deployment.DeploymentType.DEPLOY
        assert deployment.is_terminal is False

    def test_start_deployment(self, target, context):
        """Test starting a deployment."""
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
        )

        deployment.start()
        deployment.refresh_from_db()

        assert deployment.status == Deployment.Status.RUNNING
        assert deployment.started_at is not None

    def test_succeed_deployment(self, target, context):
        """Test marking deployment as successful."""
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
            status=Deployment.Status.RUNNING,
            started_at=timezone.now(),
        )

        deployment.succeed(
            container_ids=["abc123", "def456"],
            service_status={"web": {"state": "running"}},
        )
        deployment.refresh_from_db()

        assert deployment.status == Deployment.Status.SUCCESS
        assert deployment.completed_at is not None
        assert deployment.container_ids == ["abc123", "def456"]
        assert deployment.is_terminal is True

    def test_fail_deployment(self, target, context):
        """Test marking deployment as failed."""
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
            status=Deployment.Status.RUNNING,
            started_at=timezone.now(),
        )

        deployment.fail("Connection refused", exit_code=1)
        deployment.refresh_from_db()

        assert deployment.status == Deployment.Status.FAILED
        assert deployment.completed_at is not None
        assert deployment.error_message == "Connection refused"
        assert deployment.exit_code == 1
        assert deployment.is_terminal is True

    def test_cancel_deployment(self, target, context):
        """Test cancelling a deployment."""
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
            status=Deployment.Status.RUNNING,
        )

        deployment.cancel()
        deployment.refresh_from_db()

        assert deployment.status == Deployment.Status.CANCELLED
        assert deployment.is_terminal is True

    def test_duration_property(self, target, context):
        """Test deployment duration calculation."""
        now = timezone.now()
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
            started_at=now,
            completed_at=now + timezone.timedelta(seconds=30),
        )

        assert deployment.duration == 30.0

    def test_duration_none_when_not_completed(self, target, context):
        """Test duration is None when not completed."""
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
            started_at=timezone.now(),
        )

        assert deployment.duration is None


@pytest.mark.django_db
class TestDeploymentLog:

    @pytest.fixture
    def deployment(self):
        target = DeploymentTarget.objects.create(
            name="test-target",
            host="192.168.1.100",
        )
        context = DockerContext.objects.create(
            name="test-context",
            target=target,
            context_type=DockerContext.ContextType.SSH,
            endpoint="ssh://ubuntu@192.168.1.100:22",
        )
        return Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path="/path/to/compose.yml",
            compose_content='version: "3.8"',
        )

    def test_create_log(self, deployment):
        """Test creating a deployment log."""
        log = DeploymentLog.objects.create(
            deployment=deployment,
            log_level=DeploymentLog.LogLevel.INFO,
            message="Test message",
        )

        assert log.id is not None
        assert log.message == "Test message"
        assert log.timestamp is not None

    def test_log_class_method(self, deployment):
        """Test DeploymentLog.log() class method."""
        log = DeploymentLog.log(
            deployment=deployment,
            message="Deployment started",
            level="info",
            command="docker compose up -d",
            output="Container started",
        )

        assert log.id is not None
        assert log.message == "Deployment started"
        assert log.command == "docker compose up -d"
        assert log.output == "Container started"

    def test_log_ordering(self, deployment):
        """Test logs are ordered by timestamp."""
        DeploymentLog.objects.create(
            deployment=deployment,
            log_level=DeploymentLog.LogLevel.INFO,
            message="First",
        )
        DeploymentLog.objects.create(
            deployment=deployment,
            log_level=DeploymentLog.LogLevel.INFO,
            message="Second",
        )
        DeploymentLog.objects.create(
            deployment=deployment,
            log_level=DeploymentLog.LogLevel.INFO,
            message="Third",
        )

        logs = list(deployment.logs.all())

        assert logs[0].message == "First"
        assert logs[1].message == "Second"
        assert logs[2].message == "Third"
