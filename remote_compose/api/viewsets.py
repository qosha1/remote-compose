"""
DRF ViewSets for remote_compose API.

Provides ViewSets for all resources with consistent patterns for
CRUD operations, filtering, and custom actions.
"""

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from ..models import (
    DeploymentTarget,
    DockerContext,
    Deployment,
    SecureCredential,
    AuditLog,
    ECSCluster,
    ECSTaskDefinition,
    ECSService,
    ECRRepository,
    EFSFileSystem,
    BuildRecord,
    DeploymentEvent,
    ResourceMetric,
)
from .serializers import (
    DeploymentTargetSerializer,
    DockerContextSerializer,
    DeploymentListSerializer,
    DeploymentDetailSerializer,
    DeploymentLogSerializer,
    SecureCredentialListSerializer,
    SecureCredentialDetailSerializer,
    SecureCredentialCreateSerializer,
    AuditLogSerializer,
    ECSClusterListSerializer,
    ECSClusterDetailSerializer,
    ECSClusterCreateSerializer,
    ECSTaskDefinitionListSerializer,
    ECSTaskDefinitionDetailSerializer,
    ECSServiceListSerializer,
    ECSServiceDetailSerializer,
    ECRRepositorySerializer,
    EFSFileSystemSerializer,
    BuildRecordListSerializer,
    BuildRecordDetailSerializer,
    DeploymentEventSerializer,
    ResourceMetricSerializer,
)

# =============================================================================
# Base ViewSet Mixins
# =============================================================================


class ListDetailSerializerMixin:
    """
    Mixin for ViewSets that use different serializers for list vs detail views.

    Define list_serializer_class and detail_serializer_class on the ViewSet.
    """

    list_serializer_class = None
    detail_serializer_class = None

    def get_serializer_class(self):
        if self.action == "list" and self.list_serializer_class:
            return self.list_serializer_class
        if self.action in ["retrieve", "create", "update", "partial_update"]:
            if self.detail_serializer_class:
                return self.detail_serializer_class
        return super().get_serializer_class()


# =============================================================================
# Credential ViewSet
# =============================================================================


class SecureCredentialViewSet(ListDetailSerializerMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing secure credentials.

    Credentials are encrypted at rest and never returned in plaintext.
    """

    queryset = SecureCredential.objects.all()
    serializer_class = SecureCredentialDetailSerializer
    list_serializer_class = SecureCredentialListSerializer
    detail_serializer_class = SecureCredentialDetailSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["credential_type", "is_active"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.action == "create":
            return SecureCredentialCreateSerializer
        return super().get_serializer_class()

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        """
        Validate a credential by testing connectivity.
        """
        credential = self.get_object()

        try:
            if (
                credential.credential_type
                == SecureCredential.CredentialType.AWS_ACCESS_KEY
            ):
                from ..services import get_aws_client_factory

                factory = get_aws_client_factory()
                result = factory.validate_credentials(credential=credential)
                return Response(
                    {
                        "valid": True,
                        "details": result,
                    }
                )
            else:
                return Response(
                    {
                        "valid": True,
                        "message": "Credential exists (validation not implemented for this type)",
                    }
                )
        except Exception as e:
            return Response(
                {
                    "valid": False,
                    "error": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# =============================================================================
# Deployment Target ViewSet
# =============================================================================


class DeploymentTargetViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing deployment targets.
    """

    queryset = DeploymentTarget.objects.all()
    serializer_class = DeploymentTargetSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["target_type", "is_active"]
    search_fields = ["name", "host", "description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]


# =============================================================================
# Docker Context ViewSet
# =============================================================================


class DockerContextViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing Docker contexts.
    """

    queryset = DockerContext.objects.all()
    serializer_class = DockerContextSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["context_type", "is_default"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]


# =============================================================================
# ECS Cluster ViewSet
# =============================================================================


class ECSClusterViewSet(ListDetailSerializerMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing ECS clusters.
    """

    queryset = ECSCluster.objects.all()
    serializer_class = ECSClusterDetailSerializer
    list_serializer_class = ECSClusterListSerializer
    detail_serializer_class = ECSClusterDetailSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["aws_region", "launch_type", "status", "is_managed"]
    search_fields = ["name", "description", "aws_cluster_name"]
    ordering_fields = ["name", "created_at", "status"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.action == "create":
            return ECSClusterCreateSerializer
        return super().get_serializer_class()

    @action(detail=True, methods=["get"])
    def services(self, request, pk=None):
        """
        List all services in this cluster.
        """
        cluster = self.get_object()
        services = cluster.services.all()
        serializer = ECSServiceListSerializer(services, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def task_definitions(self, request, pk=None):
        """
        List all task definitions in this cluster.
        """
        cluster = self.get_object()
        task_defs = cluster.task_definitions.all()
        serializer = ECSTaskDefinitionListSerializer(task_defs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def sync_from_aws(self, request, pk=None):
        """
        Sync cluster state from AWS.
        """
        cluster = self.get_object()

        from ..services import ECSService

        ecs_service = ECSService()

        try:
            updated = ecs_service.sync_cluster_from_aws(cluster)
            serializer = self.get_serializer(updated)
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {
                    "error": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# =============================================================================
# ECS Task Definition ViewSet
# =============================================================================


class ECSTaskDefinitionViewSet(ListDetailSerializerMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing ECS task definitions.
    """

    queryset = ECSTaskDefinition.objects.all()
    serializer_class = ECSTaskDefinitionDetailSerializer
    list_serializer_class = ECSTaskDefinitionListSerializer
    detail_serializer_class = ECSTaskDefinitionDetailSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["cluster", "status", "network_mode"]
    search_fields = ["name"]
    ordering_fields = ["name", "revision", "created_at"]
    ordering = ["-created_at"]


# =============================================================================
# ECS Service ViewSet
# =============================================================================


class ECSServiceViewSet(ListDetailSerializerMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing ECS services.
    """

    queryset = ECSService.objects.all()
    serializer_class = ECSServiceDetailSerializer
    list_serializer_class = ECSServiceListSerializer
    detail_serializer_class = ECSServiceDetailSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["cluster", "status"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at", "status"]
    ordering = ["-created_at"]

    @action(detail=True, methods=["post"])
    def scale(self, request, pk=None):
        """
        Scale service to desired count.
        """
        service = self.get_object()
        desired_count = request.data.get("desired_count")

        if desired_count is None:
            return Response(
                {
                    "error": "desired_count is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            desired_count = int(desired_count)
            if desired_count < 0:
                raise ValueError("desired_count must be non-negative")
        except ValueError as e:
            return Response(
                {
                    "error": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from ..services import ECSService as ECSServiceService

        ecs_service = ECSServiceService()

        try:
            ecs_service.scale_service(
                cluster=service.cluster,
                service_name=service.name,
                desired_count=desired_count,
            )
            service.desired_count = desired_count
            service.save(update_fields=["desired_count", "updated_at"])
            serializer = self.get_serializer(service)
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {
                    "error": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# =============================================================================
# ECR Repository ViewSet
# =============================================================================


class ECRRepositoryViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing ECR repositories.
    """

    queryset = ECRRepository.objects.all()
    serializer_class = ECRRepositorySerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["cluster", "aws_region", "is_managed"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]


# =============================================================================
# EFS File System ViewSet
# =============================================================================


class EFSFileSystemViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing EFS file systems.
    """

    queryset = EFSFileSystem.objects.all()
    serializer_class = EFSFileSystemSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["cluster", "aws_region", "is_managed"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]


# =============================================================================
# Deployment ViewSet
# =============================================================================


class DeploymentViewSet(ListDetailSerializerMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing deployments.
    """

    queryset = Deployment.objects.all()
    serializer_class = DeploymentDetailSerializer
    list_serializer_class = DeploymentListSerializer
    detail_serializer_class = DeploymentDetailSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["context", "target", "status", "deployment_type"]
    search_fields = ["project_name", "version", "deployed_by"]
    ordering_fields = ["created_at", "started_at", "completed_at"]
    ordering = ["-created_at"]

    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        """
        Get deployment logs.
        """
        deployment = self.get_object()
        logs = deployment.logs.all()

        # Filter by level if provided
        level = request.query_params.get("level")
        if level:
            logs = logs.filter(log_level=level)

        serializer = DeploymentLogSerializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        """
        Get deployment events.
        """
        deployment = self.get_object()
        events = deployment.events.all()

        # Filter by event_type if provided
        event_type = request.query_params.get("event_type")
        if event_type:
            events = events.filter(event_type=event_type)

        # Filter by severity if provided
        severity = request.query_params.get("severity")
        if severity:
            events = events.filter(severity=severity)

        serializer = DeploymentEventSerializer(events, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def build_records(self, request, pk=None):
        """
        Get build records for this deployment.
        """
        deployment = self.get_object()
        builds = deployment.build_records.all()
        serializer = BuildRecordListSerializer(builds, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """
        Cancel a running deployment.
        """
        deployment = self.get_object()

        if deployment.is_terminal:
            return Response(
                {
                    "error": f"Deployment is already in terminal state: {deployment.status}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        deployment.cancel()
        serializer = self.get_serializer(deployment)
        return Response(serializer.data)


# =============================================================================
# Build Record ViewSet
# =============================================================================


class BuildRecordViewSet(ListDetailSerializerMixin, viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing build records (read-only).
    """

    queryset = BuildRecord.objects.all()
    serializer_class = BuildRecordDetailSerializer
    list_serializer_class = BuildRecordListSerializer
    detail_serializer_class = BuildRecordDetailSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["deployment", "status", "cache_hit", "ecr_repository"]
    search_fields = ["service_name", "image_uri"]
    ordering_fields = ["created_at", "build_started_at"]
    ordering = ["-created_at"]


# =============================================================================
# Deployment Event ViewSet
# =============================================================================


class DeploymentEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing deployment events (read-only).
    """

    queryset = DeploymentEvent.objects.all()
    serializer_class = DeploymentEventSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["deployment", "event_type", "severity", "service_name"]
    search_fields = ["message", "resource_id"]
    ordering_fields = ["created_at"]
    ordering = ["created_at"]


# =============================================================================
# Resource Metric ViewSet
# =============================================================================


class ResourceMetricViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing resource metrics (read-only).
    """

    queryset = ResourceMetric.objects.all()
    serializer_class = ResourceMetricSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = [
        "ecs_service",
        "efs_file_system",
        "cluster",
        "metric_type",
        "aggregation",
    ]
    ordering_fields = ["period_start", "created_at"]
    ordering = ["-period_start"]


# =============================================================================
# Audit Log ViewSet
# =============================================================================


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing audit logs (read-only).
    """

    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["action", "resource_type", "actor_type"]
    search_fields = ["actor", "resource_id", "details"]
    ordering_fields = ["timestamp"]
    ordering = ["-timestamp"]
