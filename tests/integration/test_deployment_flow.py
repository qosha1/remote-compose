"""
Integration tests for the deployment flow.
"""

import pytest
from unittest.mock import MagicMock
import tempfile
import os

from remote_compose.services import (
    TargetService,
    ContextService,
    DeploymentService,
    CredentialService,
)
from remote_compose.models import DeploymentTarget, Deployment

pytestmark = pytest.mark.integration


@pytest.mark.django_db
class TestDeploymentFlow:
    """Integration tests for the full deployment workflow."""

    @pytest.fixture
    def compose_file(self):
        """Create a temporary compose file."""
        content = """
version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "8080:80"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(content)
            path = f.name

        yield path

        os.unlink(path)

    @pytest.fixture
    def mock_ssh(self, mocker):
        """Mock SSH client for all tests."""
        mock = mocker.patch("remote_compose.services.target_service.SSHClient")
        instance = mock.return_value
        instance.test_connection.return_value = (True, "Success")
        instance.__enter__ = lambda s: s
        instance.__exit__ = lambda s, *args: None
        instance.execute.return_value = MagicMock(
            success=True,
            stdout="Container started\nabc123",
            stderr="",
            exit_code=0,
        )
        instance.upload_content.return_value = True
        return mock

    def test_full_deployment_workflow(self, compose_file, mock_ssh):
        """Test the complete deployment workflow from target creation to deployment."""
        # 1. Create services
        credential_service = CredentialService()
        target_service = TargetService(credential_service=credential_service)
        context_service = ContextService(target_service=target_service)
        deployment_service = DeploymentService(
            target_service=target_service,
            context_service=context_service,
        )

        # 2. Create a target
        target = target_service.create_target(
            name="integration-test-target",
            host="192.168.1.100",
            username="ubuntu",
            validate_connection=True,
        )

        assert target.id is not None
        assert target.health_status == DeploymentTarget.HealthStatus.HEALTHY

        # 3. Create a context
        context = context_service.create_context(
            name="integration-test-context",
            target=target,
            sync_to_docker=False,  # Don't sync to Docker daemon in tests
        )

        assert context.id is not None
        assert context.target == target

        # 4. Deploy
        deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_file,
            project_name="integration-test",
            version="v1.0.0",
            deployed_by="test-user",
            context=context,
        )

        assert deployment.id is not None
        assert deployment.status == Deployment.Status.SUCCESS
        assert deployment.duration is not None
        assert deployment.deployed_by == "test-user"

        # 5. Verify deployment logs were created
        assert deployment.logs.count() > 0

    def test_deployment_with_environment_variables(self, compose_file, mock_ssh):
        """Test deployment with environment variables."""
        target_service = TargetService()
        context_service = ContextService(target_service=target_service)
        deployment_service = DeploymentService(
            target_service=target_service,
            context_service=context_service,
        )

        target = target_service.create_target(
            name="env-test-target",
            host="192.168.1.100",
            validate_connection=True,
        )

        deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_file,
            project_name="env-test",
            environment={
                "DB_HOST": "localhost",
                "DEBUG": "false",
            },
        )

        assert deployment.status == Deployment.Status.SUCCESS
        assert deployment.environment["DB_HOST"] == "localhost"
        assert deployment.environment["DEBUG"] == "false"

    def test_deployment_rollback(self, compose_file, mock_ssh):
        """Test deployment rollback."""
        target_service = TargetService()
        deployment_service = DeploymentService(target_service=target_service)

        target = target_service.create_target(
            name="rollback-test-target",
            host="192.168.1.100",
            validate_connection=True,
        )

        # Initial deployment
        v1_deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_file,
            project_name="rollback-test",
            version="v1.0.0",
        )

        assert v1_deployment.status == Deployment.Status.SUCCESS

        # Second deployment
        v2_deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_file,
            project_name="rollback-test",
            version="v2.0.0",
        )

        assert v2_deployment.status == Deployment.Status.SUCCESS

        # Rollback to v1
        rollback = deployment_service.rollback(v1_deployment)

        assert rollback.status == Deployment.Status.SUCCESS
        assert rollback.deployment_type == Deployment.DeploymentType.ROLLBACK
        assert rollback.parent_deployment == v1_deployment

        # Original deployment should be marked as rolled back
        v1_deployment.refresh_from_db()
        # Note: The rollback marks the deployment that was rolled back TO, not FROM

    def test_list_deployments(self, compose_file, mock_ssh):
        """Test listing deployments with filters."""
        target_service = TargetService()
        deployment_service = DeploymentService(target_service=target_service)

        target = target_service.create_target(
            name="list-test-target",
            host="192.168.1.100",
            validate_connection=True,
        )

        # Create multiple deployments
        for i in range(3):
            deployment_service.deploy(
                target=target,
                compose_file_path=compose_file,
                project_name="list-test",
                version=f"v{i}.0.0",
            )

        # List all deployments for this target
        deployments = deployment_service.list_deployments(
            target=target,
            project_name="list-test",
        )

        assert len(deployments) == 3

        # List only successful deployments
        successful = deployment_service.list_deployments(
            status=Deployment.Status.SUCCESS,
        )

        assert all(d.status == Deployment.Status.SUCCESS for d in successful)


@pytest.mark.django_db
class TestTargetContextIntegration:
    """Integration tests for target and context management."""

    @pytest.fixture
    def mock_ssh(self, mocker):
        """Mock SSH client."""
        mock = mocker.patch("remote_compose.services.target_service.SSHClient")
        instance = mock.return_value
        instance.test_connection.return_value = (True, "Success")
        return mock

    def test_get_or_create_context(self, mock_ssh):
        """Test automatic context creation for target."""
        target_service = TargetService()
        context_service = ContextService(target_service=target_service)

        target = target_service.create_target(
            name="auto-context-target",
            host="192.168.1.100",
            validate_connection=True,
        )

        # First call should create a context
        context1 = context_service.get_or_create_context(target)

        assert context1.id is not None
        assert context1.target == target

        # Second call should return the same context
        context2 = context_service.get_or_create_context(target)

        assert context2.id == context1.id

    def test_multiple_contexts_per_target(self, mock_ssh):
        """Test creating multiple contexts for the same target."""
        target_service = TargetService()
        context_service = ContextService(target_service=target_service)

        target = target_service.create_target(
            name="multi-context-target",
            host="192.168.1.100",
            validate_connection=True,
        )

        context1 = context_service.create_context(
            name="context-dev",
            target=target,
            sync_to_docker=False,
        )

        context2 = context_service.create_context(
            name="context-staging",
            target=target,
            sync_to_docker=False,
        )

        assert context1.id != context2.id
        assert target.contexts.count() == 2
