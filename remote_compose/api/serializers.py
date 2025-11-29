"""
DRF Serializers for remote_compose models.

Provides serializers for all models with consistent patterns for metadata
handling, nested relationships, and read/write separation.
"""

from rest_framework import serializers
from ..models import (
    DeploymentTarget,
    DockerContext,
    Deployment,
    DeploymentLog,
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


# =============================================================================
# Base Serializers and Mixins
# =============================================================================

class MetadataSerializerMixin:
    """
    Mixin for handling metadata fields consistently.

    Provides standardized handling of the JSONField 'metadata' present on
    most models, with validation and default behavior.
    """

    def validate_metadata(self, value):
        """Ensure metadata is a dictionary."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be a JSON object")
        return value


class TimestampedModelSerializer(serializers.ModelSerializer):
    """
    Base serializer for models with created_at and updated_at fields.

    Makes timestamp fields read-only by default.
    """
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class BaseModelSerializer(TimestampedModelSerializer, MetadataSerializerMixin):
    """
    Base serializer combining timestamps and metadata handling.

    Use this as the base class for most model serializers.
    """
    pass


# =============================================================================
# Credential Serializers
# =============================================================================

class SecureCredentialListSerializer(BaseModelSerializer):
    """
    Serializer for listing credentials (without sensitive data).
    """

    class Meta:
        model = SecureCredential
        fields = [
            'id',
            'name',
            'credential_type',
            'description',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class SecureCredentialDetailSerializer(BaseModelSerializer):
    """
    Serializer for credential details (still no sensitive data in response).
    """

    class Meta:
        model = SecureCredential
        fields = [
            'id',
            'name',
            'credential_type',
            'description',
            'is_active',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class SecureCredentialCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating new credentials.

    Accepts raw credential data which will be encrypted before storage.
    """
    # Write-only field for the raw credential value
    credential_value = serializers.JSONField(write_only=True)

    class Meta:
        model = SecureCredential
        fields = [
            'name',
            'credential_type',
            'description',
            'credential_value',
            'metadata',
        ]

    def create(self, validated_data):
        credential_value = validated_data.pop('credential_value')
        from ..services import CredentialService
        credential_service = CredentialService()

        # Create based on type
        if validated_data.get('credential_type') == SecureCredential.CredentialType.AWS_ACCESS_KEY:
            return credential_service.create_aws_credential(
                name=validated_data['name'],
                access_key_id=credential_value.get('access_key_id'),
                secret_access_key=credential_value.get('secret_access_key'),
                description=validated_data.get('description', ''),
            )
        elif validated_data.get('credential_type') == SecureCredential.CredentialType.SSH_KEY:
            return credential_service.create_ssh_credential(
                name=validated_data['name'],
                private_key=credential_value.get('private_key'),
                username=credential_value.get('username', 'ubuntu'),
                description=validated_data.get('description', ''),
            )
        else:
            raise serializers.ValidationError(f"Unsupported credential type")


# =============================================================================
# Deployment Target Serializers
# =============================================================================

class DeploymentTargetSerializer(BaseModelSerializer):
    """
    Serializer for deployment targets.
    """
    credential_name = serializers.CharField(source='credential.name', read_only=True)

    class Meta:
        model = DeploymentTarget
        fields = [
            'id',
            'name',
            'target_type',
            'host',
            'port',
            'description',
            'is_active',
            'credential',
            'credential_name',
            'tags',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Docker Context Serializers
# =============================================================================

class DockerContextSerializer(BaseModelSerializer):
    """
    Serializer for Docker contexts.
    """

    class Meta:
        model = DockerContext
        fields = [
            'id',
            'name',
            'context_type',
            'host',
            'description',
            'is_default',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# ECS Cluster Serializers
# =============================================================================

class ECSClusterListSerializer(BaseModelSerializer):
    """
    Serializer for listing ECS clusters.
    """

    class Meta:
        model = ECSCluster
        fields = [
            'id',
            'name',
            'aws_cluster_name',
            'aws_region',
            'launch_type',
            'status',
            'is_managed',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ECSClusterDetailSerializer(BaseModelSerializer):
    """
    Serializer for ECS cluster details.
    """
    aws_credential_name = serializers.CharField(
        source='aws_credential.name',
        read_only=True,
        allow_null=True
    )
    service_count = serializers.SerializerMethodField()
    task_definition_count = serializers.SerializerMethodField()

    class Meta:
        model = ECSCluster
        fields = [
            'id',
            'name',
            'description',
            'aws_cluster_arn',
            'aws_cluster_name',
            'aws_region',
            'launch_type',
            'is_managed',
            'vpc_id',
            'subnet_ids',
            'security_group_ids',
            'status',
            'task_execution_role_arn',
            'task_role_arn',
            'aws_credential',
            'aws_credential_name',
            'metadata',
            'service_count',
            'task_definition_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'aws_cluster_arn', 'status', 'created_at', 'updated_at']

    def get_service_count(self, obj):
        return obj.services.count()

    def get_task_definition_count(self, obj):
        return obj.task_definitions.count()


class ECSClusterCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating ECS clusters.
    """

    class Meta:
        model = ECSCluster
        fields = [
            'name',
            'description',
            'aws_cluster_name',
            'aws_region',
            'launch_type',
            'vpc_id',
            'subnet_ids',
            'security_group_ids',
            'task_execution_role_arn',
            'task_role_arn',
            'aws_credential',
            'metadata',
        ]


# =============================================================================
# ECS Task Definition Serializers
# =============================================================================

class ECSTaskDefinitionListSerializer(BaseModelSerializer):
    """
    Serializer for listing task definitions.
    """
    cluster_name = serializers.CharField(source='cluster.name', read_only=True)

    class Meta:
        model = ECSTaskDefinition
        fields = [
            'id',
            'name',
            'cluster',
            'cluster_name',
            'revision',
            'cpu',
            'memory',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'revision', 'status', 'created_at', 'updated_at']


class ECSTaskDefinitionDetailSerializer(BaseModelSerializer):
    """
    Serializer for task definition details.
    """
    cluster_name = serializers.CharField(source='cluster.name', read_only=True)
    container_count = serializers.SerializerMethodField()

    class Meta:
        model = ECSTaskDefinition
        fields = [
            'id',
            'name',
            'cluster',
            'cluster_name',
            'aws_task_definition_arn',
            'revision',
            'source_compose_file',
            'source_compose_hash',
            'container_definitions',
            'cpu',
            'memory',
            'requires_compatibilities',
            'network_mode',
            'volumes',
            'status',
            'metadata',
            'container_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'aws_task_definition_arn', 'revision',
            'status', 'created_at', 'updated_at'
        ]

    def get_container_count(self, obj):
        return len(obj.container_definitions) if obj.container_definitions else 0


# =============================================================================
# ECS Service Serializers
# =============================================================================

class ECSServiceListSerializer(BaseModelSerializer):
    """
    Serializer for listing ECS services.
    """
    cluster_name = serializers.CharField(source='cluster.name', read_only=True)
    task_definition_name = serializers.CharField(
        source='task_definition.name',
        read_only=True
    )

    class Meta:
        model = ECSService
        fields = [
            'id',
            'name',
            'cluster',
            'cluster_name',
            'task_definition_name',
            'desired_count',
            'running_count',
            'pending_count',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'running_count', 'pending_count',
            'status', 'created_at', 'updated_at'
        ]


class ECSServiceDetailSerializer(BaseModelSerializer):
    """
    Serializer for ECS service details.
    """
    cluster_name = serializers.CharField(source='cluster.name', read_only=True)
    task_definition_name = serializers.CharField(
        source='task_definition.name',
        read_only=True
    )
    is_healthy = serializers.BooleanField(read_only=True)

    class Meta:
        model = ECSService
        fields = [
            'id',
            'name',
            'cluster',
            'cluster_name',
            'task_definition',
            'task_definition_name',
            'aws_service_arn',
            'desired_count',
            'min_count',
            'max_count',
            'running_count',
            'pending_count',
            'deployment_controller',
            'deployment_minimum_percent',
            'deployment_maximum_percent',
            'load_balancer_arn',
            'container_name_for_lb',
            'container_port_for_lb',
            'status',
            'is_healthy',
            'last_deployment_at',
            'last_error',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'aws_service_arn', 'running_count', 'pending_count',
            'status', 'is_healthy', 'last_deployment_at', 'last_error',
            'created_at', 'updated_at'
        ]


# =============================================================================
# ECR Repository Serializers
# =============================================================================

class ECRRepositorySerializer(BaseModelSerializer):
    """
    Serializer for ECR repositories.
    """
    cluster_name = serializers.CharField(source='cluster.name', read_only=True)

    class Meta:
        model = ECRRepository
        fields = [
            'id',
            'name',
            'cluster',
            'cluster_name',
            'aws_repository_arn',
            'aws_repository_uri',
            'aws_region',
            'is_managed',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'aws_repository_arn', 'aws_repository_uri',
            'created_at', 'updated_at'
        ]


# =============================================================================
# EFS File System Serializers
# =============================================================================

class EFSFileSystemSerializer(BaseModelSerializer):
    """
    Serializer for EFS file systems.
    """
    cluster_name = serializers.CharField(source='cluster.name', read_only=True)
    is_ready = serializers.BooleanField(read_only=True)
    access_point_count = serializers.SerializerMethodField()

    class Meta:
        model = EFSFileSystem
        fields = [
            'id',
            'name',
            'cluster',
            'cluster_name',
            'aws_file_system_id',
            'aws_region',
            'vpc_id',
            'security_group_id',
            'is_managed',
            'is_ready',
            'access_points',
            'mount_targets',
            'access_point_count',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'aws_file_system_id', 'is_ready',
            'mount_targets', 'created_at', 'updated_at'
        ]

    def get_access_point_count(self, obj):
        return len(obj.access_points) if obj.access_points else 0


# =============================================================================
# Deployment Serializers
# =============================================================================

class DeploymentLogSerializer(serializers.ModelSerializer):
    """
    Serializer for deployment logs.
    """

    class Meta:
        model = DeploymentLog
        fields = [
            'id',
            'log_level',
            'message',
            'command',
            'output',
            'service_name',
            'timestamp',
        ]
        read_only_fields = ['id', 'timestamp']


class DeploymentListSerializer(BaseModelSerializer):
    """
    Serializer for listing deployments.
    """
    context_name = serializers.CharField(source='context.name', read_only=True)
    target_name = serializers.CharField(source='target.name', read_only=True)
    duration = serializers.FloatField(read_only=True)

    class Meta:
        model = Deployment
        fields = [
            'id',
            'context_name',
            'target_name',
            'project_name',
            'status',
            'deployment_type',
            'version',
            'deployed_by',
            'duration',
            'started_at',
            'completed_at',
            'created_at',
        ]
        read_only_fields = ['id', 'duration', 'started_at', 'completed_at', 'created_at']


class DeploymentDetailSerializer(BaseModelSerializer):
    """
    Serializer for deployment details.
    """
    context_name = serializers.CharField(source='context.name', read_only=True)
    target_name = serializers.CharField(source='target.name', read_only=True)
    duration = serializers.FloatField(read_only=True)
    is_terminal = serializers.BooleanField(read_only=True)
    logs = DeploymentLogSerializer(many=True, read_only=True)
    log_count = serializers.SerializerMethodField()

    class Meta:
        model = Deployment
        fields = [
            'id',
            'context',
            'context_name',
            'target',
            'target_name',
            'compose_file_path',
            'compose_content',
            'project_name',
            'environment',
            'status',
            'deployment_type',
            'started_at',
            'completed_at',
            'duration',
            'is_terminal',
            'error_message',
            'exit_code',
            'deployed_by',
            'parent_deployment',
            'version',
            'container_ids',
            'service_status',
            'metadata',
            'logs',
            'log_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'duration', 'is_terminal', 'started_at', 'completed_at',
            'error_message', 'exit_code', 'container_ids', 'service_status',
            'created_at', 'updated_at'
        ]

    def get_log_count(self, obj):
        return obj.logs.count()


# =============================================================================
# Build Record Serializers
# =============================================================================

class BuildRecordListSerializer(BaseModelSerializer):
    """
    Serializer for listing build records.
    """

    class Meta:
        model = BuildRecord
        fields = [
            'id',
            'deployment',
            'service_name',
            'image_uri',
            'status',
            'build_duration',
            'cache_hit',
            'created_at',
        ]
        read_only_fields = ['id', 'build_duration', 'created_at']


class BuildRecordDetailSerializer(BaseModelSerializer):
    """
    Serializer for build record details.
    """
    build_duration = serializers.FloatField(read_only=True)
    push_duration = serializers.FloatField(read_only=True)
    total_duration = serializers.FloatField(read_only=True)

    class Meta:
        model = BuildRecord
        fields = [
            'id',
            'deployment',
            'ecr_repository',
            'service_name',
            'image_uri',
            'image_tag',
            'context_path',
            'dockerfile_path',
            'context_hash',
            'status',
            'build_started_at',
            'build_completed_at',
            'push_started_at',
            'push_completed_at',
            'build_duration',
            'push_duration',
            'total_duration',
            'error_message',
            'build_log',
            'image_digest',
            'image_size_bytes',
            'build_args',
            'platform',
            'cache_hit',
            'previous_build',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'build_duration', 'push_duration', 'total_duration',
            'image_digest', 'created_at', 'updated_at'
        ]


# =============================================================================
# Deployment Event Serializers
# =============================================================================

class DeploymentEventSerializer(BaseModelSerializer):
    """
    Serializer for deployment events.
    """

    class Meta:
        model = DeploymentEvent
        fields = [
            'id',
            'deployment',
            'event_type',
            'severity',
            'message',
            'service_name',
            'resource_type',
            'resource_id',
            'previous_state',
            'new_state',
            'duration_ms',
            'error_code',
            'stack_trace',
            'metadata',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


# =============================================================================
# Resource Metric Serializers
# =============================================================================

class ResourceMetricSerializer(BaseModelSerializer):
    """
    Serializer for resource metrics.
    """

    class Meta:
        model = ResourceMetric
        fields = [
            'id',
            'ecs_service',
            'efs_file_system',
            'cluster',
            'metric_type',
            'aggregation',
            'value',
            'unit',
            'period_start',
            'period_end',
            'period_seconds',
            'sample_count',
            'dimensions',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


# =============================================================================
# Audit Log Serializer
# =============================================================================

class AuditLogSerializer(serializers.ModelSerializer):
    """
    Serializer for audit logs (read-only).
    """

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'action',
            'resource_type',
            'resource_id',
            'actor',
            'actor_type',
            'source_ip',
            'details',
            'timestamp',
        ]
        read_only_fields = fields
