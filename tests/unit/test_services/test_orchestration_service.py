"""
Unit tests for OrchestrationService.
"""

import pytest
from unittest.mock import MagicMock, patch

from remote_compose.services import (
    OrchestrationService,
    ServiceDeployment,
    DeploymentStrategy,
    OrchestrationResult,
)
from remote_compose.models import Deployment, DeploymentTarget
from remote_compose.exceptions import ValidationError


class TestServiceDeployment:
    """Tests for ServiceDeployment dataclass."""

    def test_create_service_deployment(self):
        """Test creating a ServiceDeployment."""
        sd = ServiceDeployment(
            target_id=1,
            compose_file_path="/path/to/compose.yml",
            project_name="myproject",
            environment={"DEBUG": "true"},
            version="v1.0.0",
            priority=1,
            depends_on=["other-project"],
        )

        assert sd.target_id == 1
        assert sd.project_name == "myproject"
        assert sd.environment == {"DEBUG": "true"}
        assert sd.depends_on == ["other-project"]

    def test_service_deployment_defaults(self):
        """Test ServiceDeployment default values."""
        sd = ServiceDeployment(
            target_id=1,
            compose_file_path="/path/to/compose.yml",
            project_name="myproject",
        )

        assert sd.environment == {}
        assert sd.version == ""
        assert sd.priority == 0
        assert sd.depends_on == []


class TestOrchestrationService:
    """Tests for the OrchestrationService."""

    @pytest.fixture
    def service(self):
        return OrchestrationService(max_parallel=2)

    @pytest.fixture
    def mock_deployment(self):
        """Create a mock deployment."""
        deployment = MagicMock(spec=Deployment)
        deployment.id = 1
        deployment.status = Deployment.Status.SUCCESS
        deployment.error_message = None
        return deployment

    @pytest.fixture
    def sample_services(self):
        """Create sample service deployments."""
        return [
            ServiceDeployment(
                target_id=1,
                compose_file_path="/path/compose1.yml",
                project_name="service-a",
                priority=0,
            ),
            ServiceDeployment(
                target_id=2,
                compose_file_path="/path/compose2.yml",
                project_name="service-b",
                priority=1,
            ),
        ]

    def test_sort_by_dependencies_no_deps(self, service, sample_services):
        """Test sorting services without dependencies."""
        result = service._sort_by_dependencies(sample_services)

        # Should maintain priority order
        assert result[0].project_name == "service-a"
        assert result[1].project_name == "service-b"

    def test_sort_by_dependencies_with_deps(self, service):
        """Test sorting services with dependencies."""
        services = [
            ServiceDeployment(
                target_id=1,
                compose_file_path="/path/compose1.yml",
                project_name="frontend",
                depends_on=["backend", "api"],
            ),
            ServiceDeployment(
                target_id=2,
                compose_file_path="/path/compose2.yml",
                project_name="backend",
                depends_on=["database"],
            ),
            ServiceDeployment(
                target_id=3,
                compose_file_path="/path/compose3.yml",
                project_name="database",
            ),
            ServiceDeployment(
                target_id=4,
                compose_file_path="/path/compose4.yml",
                project_name="api",
                depends_on=["database"],
            ),
        ]

        result = service._sort_by_dependencies(services)
        names = [s.project_name for s in result]

        # database should come before backend and api
        assert names.index("database") < names.index("backend")
        assert names.index("database") < names.index("api")
        # backend and api should come before frontend
        assert names.index("backend") < names.index("frontend")
        assert names.index("api") < names.index("frontend")

    def test_sort_by_dependencies_circular(self, service):
        """Test that circular dependencies raise error."""
        services = [
            ServiceDeployment(
                target_id=1,
                compose_file_path="/path/compose1.yml",
                project_name="service-a",
                depends_on=["service-b"],
            ),
            ServiceDeployment(
                target_id=2,
                compose_file_path="/path/compose2.yml",
                project_name="service-b",
                depends_on=["service-a"],
            ),
        ]

        with pytest.raises(ValidationError) as exc_info:
            service._sort_by_dependencies(services)

        assert "Circular dependency" in str(exc_info.value)

    def test_deploy_multiple_empty_list(self, service):
        """Test deploying empty service list."""
        with pytest.raises(ValidationError):
            service.deploy_multiple(services=[], deployed_by="test")

    def test_deploy_sequential(self, service, sample_services, mock_deployment):
        """Test sequential deployment strategy."""
        with patch.object(
            service, "_deploy_single_service", return_value=mock_deployment
        ):
            result = service.deploy_multiple(
                services=sample_services,
                strategy=DeploymentStrategy.SEQUENTIAL,
                deployed_by="test",
            )

        assert result.success is True
        assert result.total_services == 2
        assert result.strategy == "sequential"

    def test_deploy_sequential_stops_on_failure(self, service, sample_services):
        """Test that sequential deployment stops on first failure."""
        failed_deployment = MagicMock(spec=Deployment)
        failed_deployment.status = Deployment.Status.FAILED
        failed_deployment.error_message = "Deployment failed"

        with patch.object(service, "_deploy_single_service") as mock_deploy:
            mock_deploy.return_value = failed_deployment

            result = service.deploy_multiple(
                services=sample_services,
                strategy=DeploymentStrategy.SEQUENTIAL,
                deployed_by="test",
                rollback_on_failure=False,
            )

        # Should stop after first failure
        assert mock_deploy.call_count == 1
        assert result.failed_count >= 1

    def test_deploy_parallel(self, service, sample_services, mock_deployment):
        """Test parallel deployment strategy."""
        with patch.object(
            service, "_deploy_single_service", return_value=mock_deployment
        ):
            result = service.deploy_multiple(
                services=sample_services,
                strategy=DeploymentStrategy.PARALLEL,
                deployed_by="test",
            )

        assert result.success is True
        assert result.strategy == "parallel"

    def test_deploy_rolling(self, service, mock_deployment):
        """Test rolling deployment strategy."""
        services = [
            ServiceDeployment(
                target_id=i, compose_file_path=f"/path/{i}.yml", project_name=f"svc-{i}"
            )
            for i in range(5)
        ]

        with patch.object(
            service, "_deploy_single_service", return_value=mock_deployment
        ):
            result = service.deploy_multiple(
                services=services,
                strategy=DeploymentStrategy.ROLLING,
                deployed_by="test",
                batch_size=2,
            )

        assert result.success is True
        assert result.strategy == "rolling"

    def test_deploy_canary(self, service, sample_services, mock_deployment):
        """Test canary deployment strategy."""
        # Add canary target service
        canary_service = ServiceDeployment(
            target_id=99,
            compose_file_path="/path/canary.yml",
            project_name="canary-service",
        )
        all_services = [canary_service] + sample_services

        with patch.object(
            service, "_deploy_single_service", return_value=mock_deployment
        ):
            result = service.deploy_multiple(
                services=all_services,
                strategy=DeploymentStrategy.CANARY,
                deployed_by="test",
                canary_target_id=99,
            )

        assert result.success is True
        assert result.strategy == "canary"

    def test_deploy_canary_requires_target(self, service, sample_services):
        """Test that canary deployment requires canary_target_id."""
        result = service.deploy_multiple(
            services=sample_services,
            strategy=DeploymentStrategy.CANARY,
            deployed_by="test",
        )

        # Should fail with error about canary_target_id
        assert result.success is False
        assert any("canary_target_id" in str(e.get("error", "")) for e in result.errors)

    def test_create_deployment_plan(self, service, sample_services):
        """Test creating deployment plan without executing."""
        with patch.object(DeploymentTarget.objects, "get") as mock_get:
            mock_target = MagicMock()
            mock_target.name = "test-target"
            mock_get.return_value = mock_target

            plan = service.create_deployment_plan(
                services=sample_services,
                strategy=DeploymentStrategy.SEQUENTIAL,
            )

        assert plan["strategy"] == "sequential"
        assert plan["total_services"] == 2
        assert len(plan["deployment_order"]) == 2

    def test_deploy_to_target_group(self, service, mock_deployment):
        """Test deploying same service to multiple targets."""
        with patch.object(service, "deploy_multiple") as mock_deploy:
            mock_deploy.return_value = MagicMock(success=True)

            service.deploy_to_target_group(
                target_ids=[1, 2, 3],
                compose_file_path="/path/compose.yml",
                project_name="myservice",
                deployed_by="test",
            )

        # Should create 3 ServiceDeployment objects
        call_args = mock_deploy.call_args
        services = call_args[1]["services"]
        assert len(services) == 3


class TestOrchestrationResult:
    """Tests for OrchestrationResult dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        deployments = [MagicMock(id=1), MagicMock(id=2)]
        result = OrchestrationResult(
            success=True,
            total_services=2,
            successful_count=2,
            failed_count=0,
            deployments=deployments,
            errors=[],
            duration_seconds=10.5,
            strategy="sequential",
        )

        data = result.to_dict()

        assert data["success"] is True
        assert data["deployment_ids"] == [1, 2]
        assert data["duration_seconds"] == 10.5
