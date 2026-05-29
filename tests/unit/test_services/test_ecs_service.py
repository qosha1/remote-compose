"""
Tests for ECS service.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from botocore.exceptions import ClientError

from remote_compose.models import (
    ECSCluster,
    ECSTaskDefinition,
    ECSService as ECSServiceModel,
)
from remote_compose.services import ECSService, ComposeToECSConverter
from remote_compose.exceptions import (
    ECSClusterNotFoundError,
    ECSTaskDefinitionError,
    ComposeConversionError,
)


@pytest.fixture
def ecs_service():
    return ECSService()


@pytest.fixture
def mock_boto_client():
    with patch("boto3.client") as mock:
        yield mock


@pytest.fixture
def cluster(db):
    return ECSCluster.objects.create(
        name="test-cluster",
        aws_cluster_name="test-cluster",
        aws_cluster_arn="arn:aws:ecs:us-east-1:123456789:cluster/test-cluster",
        aws_region="us-east-1",
        launch_type=ECSCluster.LaunchType.FARGATE,
        status=ECSCluster.ClusterStatus.ACTIVE,
        subnet_ids=["subnet-123", "subnet-456"],
        security_group_ids=["sg-123"],
    )


@pytest.fixture
def task_definition(db, cluster):
    return ECSTaskDefinition.objects.create(
        name="test-app",
        cluster=cluster,
        revision=1,
        container_definitions=[
            {
                "name": "web",
                "image": "nginx:alpine",
                "cpu": 256,
                "memory": 512,
                "portMappings": [{"containerPort": 80}],
            }
        ],
        cpu="256",
        memory="512",
        requires_compatibilities=["FARGATE"],
        network_mode="awsvpc",
        status=ECSTaskDefinition.Status.REGISTERED,
        aws_task_definition_arn="arn:aws:ecs:us-east-1:123456789:task-definition/test-app:1",
    )


class TestECSServiceClusterOperations:
    """Tests for ECS cluster operations."""

    def test_list_clusters(self, ecs_service, mock_boto_client):
        """Test listing ECS clusters."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        mock_client.get_paginator.return_value.paginate.return_value = [
            {"clusterArns": ["arn:aws:ecs:us-east-1:123:cluster/test"]}
        ]
        mock_client.describe_clusters.return_value = {
            "clusters": [
                {
                    "clusterArn": "arn:aws:ecs:us-east-1:123:cluster/test",
                    "clusterName": "test",
                    "status": "ACTIVE",
                    "runningTasksCount": 5,
                    "pendingTasksCount": 0,
                    "activeServicesCount": 2,
                }
            ]
        }

        clusters = ecs_service.list_clusters()

        assert len(clusters) == 1
        assert clusters[0]["name"] == "test"
        assert clusters[0]["status"] == "ACTIVE"
        assert clusters[0]["running_tasks"] == 5

    def test_get_cluster(self, ecs_service, mock_boto_client):
        """Test getting a specific cluster."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        mock_client.describe_clusters.return_value = {
            "clusters": [
                {
                    "clusterArn": "arn:aws:ecs:us-east-1:123:cluster/test",
                    "clusterName": "test",
                    "status": "ACTIVE",
                }
            ]
        }

        cluster = ecs_service.get_cluster("test")

        assert cluster["name"] == "test"
        mock_client.describe_clusters.assert_called_once_with(clusters=["test"])

    def test_get_cluster_not_found(self, ecs_service, mock_boto_client):
        """Test getting a non-existent cluster."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        mock_client.describe_clusters.return_value = {
            "clusters": [],
            "failures": [{"reason": "MISSING", "arn": "test"}],
        }

        with pytest.raises(ECSClusterNotFoundError):
            ecs_service.get_cluster("non-existent")

    @pytest.mark.django_db
    def test_create_cluster(self, ecs_service, mock_boto_client):
        """Test creating a new ECS cluster."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        mock_client.create_cluster.return_value = {
            "cluster": {
                "clusterArn": "arn:aws:ecs:us-east-1:123:cluster/new-cluster",
                "clusterName": "new-cluster",
                "status": "ACTIVE",
            }
        }

        cluster = ecs_service.create_cluster("new-cluster", region="us-east-1")

        assert cluster.name == "new-cluster"
        assert (
            cluster.aws_cluster_arn == "arn:aws:ecs:us-east-1:123:cluster/new-cluster"
        )
        assert cluster.status == ECSCluster.ClusterStatus.ACTIVE
        assert cluster.is_managed is True

    @pytest.mark.django_db
    def test_import_cluster(self, ecs_service, mock_boto_client):
        """Test importing an existing cluster."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        mock_client.describe_clusters.return_value = {
            "clusters": [
                {
                    "clusterArn": "arn:aws:ecs:us-east-1:123:cluster/existing",
                    "clusterName": "existing",
                    "status": "ACTIVE",
                }
            ]
        }

        cluster = ecs_service.import_cluster("existing", region="us-east-1")

        assert cluster.name == "existing"
        assert cluster.is_managed is False


class TestECSServiceTaskDefinitions:
    """Tests for task definition operations."""

    @pytest.mark.django_db
    def test_register_task_definition(self, ecs_service, mock_boto_client, cluster):
        """Test registering a task definition."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        task_def = ECSTaskDefinition.objects.create(
            name="new-task",
            cluster=cluster,
            container_definitions=[{"name": "web", "image": "nginx"}],
            cpu="256",
            memory="512",
        )

        mock_client.register_task_definition.return_value = {
            "taskDefinition": {
                "taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/new-task:1",
                "revision": 1,
            }
        }

        result = ecs_service.register_task_definition(task_def)

        assert (
            result.aws_task_definition_arn
            == "arn:aws:ecs:us-east-1:123:task-definition/new-task:1"
        )
        assert result.revision == 1
        assert result.status == ECSTaskDefinition.Status.REGISTERED

    @pytest.mark.django_db
    def test_register_task_definition_error(
        self, ecs_service, mock_boto_client, cluster
    ):
        """Test task definition registration failure."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        task_def = ECSTaskDefinition.objects.create(
            name="fail-task",
            cluster=cluster,
            container_definitions=[{"name": "web", "image": "invalid"}],
            cpu="256",
            memory="512",
        )

        mock_client.register_task_definition.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterException",
                    "Message": "Invalid image",
                }
            },
            "RegisterTaskDefinition",
        )

        with pytest.raises(ECSTaskDefinitionError):
            ecs_service.register_task_definition(task_def)


class TestECSServiceOperations:
    """Tests for ECS service operations."""

    @pytest.mark.django_db
    def test_create_service(
        self, ecs_service, mock_boto_client, cluster, task_definition
    ):
        """Test creating an ECS service."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        ecs_svc = ECSServiceModel.objects.create(
            name="web-service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
        )

        mock_client.create_service.return_value = {
            "service": {
                "serviceArn": "arn:aws:ecs:us-east-1:123:service/test-cluster/web-service",
                "serviceName": "web-service",
                "status": "ACTIVE",
            }
        }

        result = ecs_service.create_service(ecs_svc)

        assert (
            result.aws_service_arn
            == "arn:aws:ecs:us-east-1:123:service/test-cluster/web-service"
        )
        assert result.status == ECSServiceModel.ServiceStatus.CREATING

    @pytest.mark.django_db
    def test_update_service(
        self, ecs_service, mock_boto_client, cluster, task_definition
    ):
        """Test updating an ECS service."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        ecs_svc = ECSServiceModel.objects.create(
            name="web-service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            aws_service_arn="arn:aws:ecs:us-east-1:123:service/test-cluster/web-service",
        )

        mock_client.update_service.return_value = {
            "service": {
                "serviceArn": ecs_svc.aws_service_arn,
                "serviceName": "web-service",
                "status": "ACTIVE",
                "runningCount": 2,
                "desiredCount": 3,
            }
        }

        result = ecs_service.update_service(ecs_svc, desired_count=3)

        assert result.desired_count == 3
        mock_client.update_service.assert_called_once()


class TestComposeToECSConverter:
    """Tests for Docker Compose to ECS conversion."""

    @pytest.fixture
    def converter(self):
        return ComposeToECSConverter()

    @pytest.mark.django_db
    def test_convert_simple_compose(self, converter, cluster):
        """Test converting a simple compose file."""
        compose = """
version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
"""
        result = converter.convert(compose, cluster, "test-app")

        assert result.name == "test-app"
        assert len(result.container_definitions) == 1
        assert result.container_definitions[0]["name"] == "web"
        assert result.container_definitions[0]["image"] == "nginx:alpine"

    @pytest.mark.django_db
    def test_convert_with_environment(self, converter, cluster):
        """Test converting compose with environment variables."""
        compose = """
version: '3.8'
services:
  app:
    image: myapp:latest
    environment:
      - DEBUG=true
      - API_KEY=secret
"""
        result = converter.convert(compose, cluster, "test-app")

        env_vars = result.container_definitions[0]["environment"]
        assert {"name": "DEBUG", "value": "true"} in env_vars
        assert {"name": "API_KEY", "value": "secret"} in env_vars

    @pytest.mark.django_db
    def test_convert_with_healthcheck(self, converter, cluster):
        """Test converting compose with healthcheck."""
        compose = """
version: '3.8'
services:
  web:
    image: nginx:alpine
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost/"]
      interval: 30s
      timeout: 10s
      retries: 3
"""
        result = converter.convert(compose, cluster, "test-app")

        health = result.container_definitions[0]["healthCheck"]
        assert health["command"] == ["CMD", "curl", "-f", "http://localhost/"]
        assert health["interval"] == 30
        assert health["timeout"] == 10
        assert health["retries"] == 3

    @pytest.mark.django_db
    def test_convert_with_resources(self, converter, cluster):
        """Test converting compose with resource limits."""
        compose = """
version: '3.8'
services:
  app:
    image: myapp:latest
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 1G
"""
        result = converter.convert(compose, cluster, "test-app")

        # Should round up to valid Fargate values
        assert int(result.cpu) >= 512
        assert int(result.memory) >= 1024

    @pytest.mark.django_db
    def test_convert_multiple_services(self, converter, cluster):
        """Test converting compose with multiple services."""
        compose = """
version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
  redis:
    image: redis:alpine
    ports:
      - "6379:6379"
"""
        result = converter.convert(compose, cluster, "test-app")

        assert len(result.container_definitions) == 2
        names = [c["name"] for c in result.container_definitions]
        assert "web" in names
        assert "redis" in names

    @pytest.mark.django_db
    def test_convert_with_depends_on(self, converter, cluster):
        """Test converting compose with depends_on."""
        compose = """
version: '3.8'
services:
  web:
    image: myapp
    depends_on:
      - db
  db:
    image: postgres
"""
        result = converter.convert(compose, cluster, "test-app")

        web_container = next(
            c for c in result.container_definitions if c["name"] == "web"
        )
        assert "dependsOn" in web_container
        assert web_container["dependsOn"][0]["containerName"] == "db"

    def test_convert_invalid_yaml(self, converter):
        """Test converting invalid YAML."""
        with pytest.raises(ComposeConversionError):
            converter.convert("invalid: yaml: content:", Mock(), "test")

    def test_convert_empty_compose(self, converter):
        """Test converting empty compose file."""
        with pytest.raises(ComposeConversionError):
            converter.convert("", Mock(), "test")

    def test_convert_no_services(self, converter):
        """Test converting compose with no services."""
        compose = """
version: '3.8'
networks:
  default:
"""
        with pytest.raises(ComposeConversionError):
            converter.convert(compose, Mock(), "test")

    @pytest.mark.django_db
    def test_convert_build_warning(self, converter, cluster):
        """Test that build configs generate warnings."""
        compose = """
version: '3.8'
services:
  app:
    build: ./app
"""
        converter.convert(compose, cluster, "test-app")

        assert len(converter.warnings) > 0
        assert any("build" in w.lower() for w in converter.warnings)

    @pytest.mark.django_db
    def test_convert_volume_warning_for_fargate(self, converter, cluster):
        """Test that host volumes generate warnings for Fargate."""
        cluster.launch_type = ECSCluster.LaunchType.FARGATE
        cluster.save()

        compose = """
version: '3.8'
services:
  app:
    image: myapp
    volumes:
      - /host/path:/container/path
"""
        converter.convert(compose, cluster, "test-app")

        assert len(converter.warnings) > 0
        assert any(
            "volume" in w.lower() or "fargate" in w.lower() for w in converter.warnings
        )


class TestECSModels:
    """Tests for ECS models."""

    @pytest.mark.django_db
    def test_cluster_is_active_property(self):
        """Test cluster is_active property."""
        cluster = ECSCluster.objects.create(
            name="test",
            aws_cluster_name="test",
            aws_region="us-east-1",
            status=ECSCluster.ClusterStatus.ACTIVE,
        )

        assert cluster.is_active is True

        cluster.status = ECSCluster.ClusterStatus.PENDING
        assert cluster.is_active is False

    @pytest.mark.django_db
    def test_task_definition_to_aws_format(self, cluster):
        """Test task definition to AWS format conversion."""
        cluster.task_execution_role_arn = "arn:aws:iam::123:role/ecsTaskExecutionRole"
        cluster.save()

        task_def = ECSTaskDefinition.objects.create(
            name="test-task",
            cluster=cluster,
            container_definitions=[{"name": "web", "image": "nginx"}],
            cpu="256",
            memory="512",
            requires_compatibilities=["FARGATE"],
        )

        aws_format = task_def.to_aws_format()

        assert aws_format["family"] == "test-task"
        assert aws_format["cpu"] == "256"
        assert aws_format["memory"] == "512"
        assert aws_format["executionRoleArn"] == cluster.task_execution_role_arn

    @pytest.mark.django_db
    def test_service_is_healthy(self, cluster, task_definition):
        """Test service is_healthy property."""
        service = ECSServiceModel.objects.create(
            name="test-service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            running_count=2,
            status=ECSServiceModel.ServiceStatus.ACTIVE,
        )

        assert service.is_healthy is True

        service.running_count = 1
        assert service.is_healthy is False

        service.running_count = 2
        service.status = ECSServiceModel.ServiceStatus.UPDATING
        assert service.is_healthy is False

    @pytest.mark.django_db
    def test_service_to_aws_create_format(self, cluster, task_definition):
        """Test service to AWS create format."""
        cluster.launch_type = ECSCluster.LaunchType.FARGATE
        cluster.subnet_ids = ["subnet-1", "subnet-2"]
        cluster.security_group_ids = ["sg-1"]
        cluster.save()

        service = ECSServiceModel.objects.create(
            name="test-service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
        )

        aws_format = service.to_aws_create_format()

        assert aws_format["serviceName"] == "test-service"
        assert aws_format["desiredCount"] == 2
        assert aws_format["launchType"] == "FARGATE"
        assert "networkConfiguration" in aws_format
        assert aws_format["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == [
            "subnet-1",
            "subnet-2",
        ]
