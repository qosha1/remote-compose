"""
AWS ECS models for remote_compose.

These models represent ECS clusters, task definitions, and services,
enabling managed container deployments without SSH access.
"""

from django.db import models
from .base import TimestampedModel


class ECSCluster(TimestampedModel):
    """
    Represents an AWS ECS cluster (either managed by this library or external).

    Clusters can be created and managed by remote-compose or can reference
    existing clusters in AWS for deployment targets.
    """

    class LaunchType(models.TextChoices):
        FARGATE = 'fargate', 'AWS Fargate'
        EC2 = 'ec2', 'EC2 Instances'

    class ClusterStatus(models.TextChoices):
        PENDING = 'pending', 'Pending Creation'
        ACTIVE = 'active', 'Active'
        PROVISIONING = 'provisioning', 'Provisioning'
        DEPROVISIONING = 'deprovisioning', 'Deprovisioning'
        FAILED = 'failed', 'Failed'
        INACTIVE = 'inactive', 'Inactive'

    # Identification
    name = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique name for this cluster in remote-compose"
    )
    description = models.TextField(blank=True)

    # AWS Details
    aws_cluster_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text="AWS ECS Cluster ARN"
    )
    aws_cluster_name = models.CharField(
        max_length=255,
        help_text="Cluster name in AWS ECS"
    )
    aws_region = models.CharField(
        max_length=50,
        default='us-east-1',
        help_text="AWS region where cluster is deployed"
    )

    # Configuration
    launch_type = models.CharField(
        max_length=20,
        choices=LaunchType.choices,
        default=LaunchType.FARGATE,
        help_text="Default launch type for services in this cluster"
    )
    is_managed = models.BooleanField(
        default=True,
        help_text="Whether this cluster is managed by remote-compose"
    )

    # Networking (for Fargate)
    vpc_id = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="VPC ID for Fargate tasks"
    )
    subnet_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="List of subnet IDs for task placement"
    )
    security_group_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="List of security group IDs for tasks"
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=ClusterStatus.choices,
        default=ClusterStatus.PENDING
    )

    # IAM
    task_execution_role_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="IAM role ARN for ECS task execution"
    )
    task_role_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="IAM role ARN for tasks to access AWS services"
    )

    # AWS Credentials reference
    aws_credential = models.ForeignKey(
        'SecureCredential',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ecs_clusters',
        help_text="AWS credentials for managing this cluster"
    )

    # Extensible metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'remote_compose_ecs_clusters'
        ordering = ['-created_at']
        verbose_name = 'ECS Cluster'
        verbose_name_plural = 'ECS Clusters'
        indexes = [
            models.Index(fields=['aws_region', 'status']),
            models.Index(fields=['launch_type', 'status']),
        ]

    def __str__(self):
        return f"{self.name} ({self.aws_region})"

    @property
    def is_active(self):
        return self.status == self.ClusterStatus.ACTIVE

    def mark_active(self):
        """Mark cluster as active."""
        self.status = self.ClusterStatus.ACTIVE
        self.save(update_fields=['status', 'updated_at'])

    def mark_failed(self, error_message: str = None):
        """Mark cluster as failed."""
        self.status = self.ClusterStatus.FAILED
        if error_message:
            self.metadata['last_error'] = error_message
        self.save(update_fields=['status', 'metadata', 'updated_at'])


class ECSTaskDefinition(TimestampedModel):
    """
    Represents an ECS Task Definition, typically converted from docker-compose.

    Task definitions describe the containers, resources, and configuration
    needed to run a set of containers as a unit.
    """

    class CompatibilityType(models.TextChoices):
        FARGATE = 'FARGATE', 'Fargate'
        EC2 = 'EC2', 'EC2'
        EXTERNAL = 'EXTERNAL', 'External'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        REGISTERED = 'registered', 'Registered in AWS'
        ACTIVE = 'active', 'Active (in use)'
        INACTIVE = 'inactive', 'Inactive'
        DEREGISTERED = 'deregistered', 'Deregistered'

    # Identification
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Task definition family name"
    )
    cluster = models.ForeignKey(
        ECSCluster,
        on_delete=models.CASCADE,
        related_name='task_definitions',
        help_text="ECS cluster this task definition belongs to"
    )

    # AWS Details
    aws_task_definition_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text="AWS Task Definition ARN"
    )
    revision = models.IntegerField(
        default=1,
        help_text="Task definition revision number"
    )

    # Source
    source_compose_file = models.TextField(
        blank=True,
        help_text="Original docker-compose.yml content"
    )
    source_compose_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="SHA256 hash of source compose file for change detection"
    )

    # Container Definitions (ECS format)
    container_definitions = models.JSONField(
        default=list,
        help_text="ECS container definitions (converted from compose)"
    )

    # Resource Configuration
    cpu = models.CharField(
        max_length=20,
        default='256',
        help_text="CPU units (256 = 0.25 vCPU for Fargate)"
    )
    memory = models.CharField(
        max_length=20,
        default='512',
        help_text="Memory in MB (minimum 512 for Fargate)"
    )

    # Compatibility
    requires_compatibilities = models.JSONField(
        default=list,
        help_text="Required compatibility types (FARGATE, EC2)"
    )
    network_mode = models.CharField(
        max_length=20,
        default='awsvpc',
        help_text="Docker network mode (awsvpc required for Fargate)"
    )

    # Volumes
    volumes = models.JSONField(
        default=list,
        blank=True,
        help_text="ECS volume definitions"
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )

    # Extensible metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'remote_compose_ecs_task_definitions'
        ordering = ['-created_at']
        verbose_name = 'ECS Task Definition'
        verbose_name_plural = 'ECS Task Definitions'
        unique_together = [['cluster', 'name', 'revision']]
        indexes = [
            models.Index(fields=['cluster', 'name']),
            models.Index(fields=['status']),
            models.Index(fields=['source_compose_hash']),
        ]

    def __str__(self):
        return f"{self.name}:{self.revision}"

    @property
    def family(self):
        """AWS uses 'family' to group task definition revisions."""
        return self.name

    @property
    def full_arn(self):
        """Return full ARN or constructed identifier."""
        if self.aws_task_definition_arn:
            return self.aws_task_definition_arn
        return f"{self.name}:{self.revision}"

    def to_aws_format(self) -> dict:
        """
        Convert to AWS RegisterTaskDefinition API format.

        Returns dict suitable for boto3 ecs.register_task_definition()
        """
        task_def = {
            'family': self.name,
            'containerDefinitions': self.container_definitions,
            'cpu': self.cpu,
            'memory': self.memory,
            'networkMode': self.network_mode,
            'requiresCompatibilities': self.requires_compatibilities if self.requires_compatibilities is not None else ['FARGATE'],
        }

        if self.volumes:
            task_def['volumes'] = self.volumes

        # Add execution role if cluster has one
        if self.cluster.task_execution_role_arn:
            task_def['executionRoleArn'] = self.cluster.task_execution_role_arn

        if self.cluster.task_role_arn:
            task_def['taskRoleArn'] = self.cluster.task_role_arn

        return task_def


class ECSService(TimestampedModel):
    """
    Represents an ECS Service running a task definition.

    Services maintain desired count of running tasks and handle
    load balancing, deployments, and auto-scaling.
    """

    class DeploymentController(models.TextChoices):
        ECS = 'ECS', 'ECS Rolling Update'
        CODE_DEPLOY = 'CODE_DEPLOY', 'AWS CodeDeploy'
        EXTERNAL = 'EXTERNAL', 'External Controller'

    class ServiceStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CREATING = 'creating', 'Creating'
        ACTIVE = 'active', 'Active'
        DRAINING = 'draining', 'Draining'
        UPDATING = 'updating', 'Updating'
        DELETING = 'deleting', 'Deleting'
        INACTIVE = 'inactive', 'Inactive'
        FAILED = 'failed', 'Failed'

    # Identification
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Service name"
    )
    cluster = models.ForeignKey(
        ECSCluster,
        on_delete=models.CASCADE,
        related_name='services'
    )
    task_definition = models.ForeignKey(
        ECSTaskDefinition,
        on_delete=models.PROTECT,
        related_name='services',
        help_text="Active task definition for this service"
    )

    # AWS Details
    aws_service_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text="AWS ECS Service ARN"
    )

    # Scaling
    desired_count = models.IntegerField(
        default=1,
        help_text="Desired number of running tasks"
    )
    min_count = models.IntegerField(
        default=1,
        help_text="Minimum number of tasks (for auto-scaling)"
    )
    max_count = models.IntegerField(
        default=10,
        help_text="Maximum number of tasks (for auto-scaling)"
    )

    # Running state (synced from AWS)
    running_count = models.IntegerField(
        default=0,
        help_text="Current number of running tasks"
    )
    pending_count = models.IntegerField(
        default=0,
        help_text="Current number of pending tasks"
    )

    # Deployment Configuration
    deployment_controller = models.CharField(
        max_length=20,
        choices=DeploymentController.choices,
        default=DeploymentController.ECS
    )
    deployment_minimum_percent = models.IntegerField(
        default=50,
        help_text="Minimum healthy percent during deployment"
    )
    deployment_maximum_percent = models.IntegerField(
        default=200,
        help_text="Maximum percent during deployment"
    )

    # Load Balancer (optional)
    load_balancer_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Target group ARN for load balancing"
    )
    container_name_for_lb = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Container name that receives load balancer traffic"
    )
    container_port_for_lb = models.IntegerField(
        null=True,
        blank=True,
        help_text="Container port for load balancer"
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=ServiceStatus.choices,
        default=ServiceStatus.PENDING
    )
    last_deployment_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last successful deployment time"
    )
    last_error = models.TextField(
        blank=True,
        help_text="Last error message if any"
    )

    # Link back to remote_compose Deployment for tracking
    deployments = models.ManyToManyField(
        'Deployment',
        blank=True,
        related_name='ecs_services',
        help_text="Deployment records associated with this service"
    )

    # Service Connect
    service_connect_enabled = models.BooleanField(
        default=False,
        help_text="Whether Service Connect is enabled for this service"
    )
    service_connect_namespace = models.CharField(
        max_length=255,
        blank=True,
        help_text="Service Connect namespace name"
    )
    service_connect_port_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Port name for Service Connect discovery"
    )

    # Service type classification
    class ServiceType(models.TextChoices):
        INFRASTRUCTURE = 'infrastructure', 'Infrastructure (DB, Cache)'
        APPLICATION = 'application', 'Application (Backend API)'
        FRONTEND = 'frontend', 'Frontend (Web UI)'
        WORKER = 'worker', 'Worker (Background Jobs)'
        PROXY = 'proxy', 'Proxy (Nginx, Load Balancer)'

    service_type = models.CharField(
        max_length=20,
        choices=ServiceType.choices,
        default=ServiceType.APPLICATION,
        help_text="Classification of this service's role"
    )

    # Extensible metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'remote_compose_ecs_services'
        ordering = ['-created_at']
        verbose_name = 'ECS Service'
        verbose_name_plural = 'ECS Services'
        unique_together = [['cluster', 'name']]
        indexes = [
            models.Index(fields=['cluster', 'status']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.name} ({self.cluster.name})"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.min_count is not None and self.max_count is not None:
            if self.min_count > self.max_count:
                raise ValidationError('min_count cannot exceed max_count')
        if self.desired_count is not None:
            if self.min_count is not None and self.desired_count < self.min_count:
                raise ValidationError('desired_count cannot be less than min_count')
            if self.max_count is not None and self.desired_count > self.max_count:
                raise ValidationError('desired_count cannot exceed max_count')

    @property
    def is_healthy(self):
        """Check if service is running at desired capacity."""
        return (
            self.status == self.ServiceStatus.ACTIVE and
            self.running_count >= self.desired_count
        )

    def update_from_aws(self, aws_service: dict):
        """
        Update local state from AWS describe_services response.

        Args:
            aws_service: Service dict from boto3 describe_services
        """
        from django.utils import timezone

        self.aws_service_arn = aws_service.get('serviceArn')
        self.running_count = aws_service.get('runningCount', 0)
        self.pending_count = aws_service.get('pendingCount', 0)
        self.desired_count = aws_service.get('desiredCount', self.desired_count)

        # Map AWS status to our status
        aws_status = aws_service.get('status', '').upper()
        status_map = {
            'ACTIVE': self.ServiceStatus.ACTIVE,
            'DRAINING': self.ServiceStatus.DRAINING,
            'INACTIVE': self.ServiceStatus.INACTIVE,
        }
        if aws_status in status_map:
            self.status = status_map[aws_status]

        # Check deployments
        deployments = aws_service.get('deployments', [])
        if deployments:
            primary = next((d for d in deployments if d.get('status') == 'PRIMARY'), None)
            if primary and primary.get('rolloutState') == 'IN_PROGRESS':
                self.status = self.ServiceStatus.UPDATING

        self.save(update_fields=[
            'aws_service_arn', 'running_count', 'pending_count',
            'desired_count', 'status', 'updated_at'
        ])

    def to_aws_create_format(self) -> dict:
        """
        Convert to AWS CreateService API format.

        Returns dict suitable for boto3 ecs.create_service()
        """
        service_def = {
            'cluster': self.cluster.aws_cluster_arn or self.cluster.aws_cluster_name,
            'serviceName': self.name,
            'taskDefinition': self.task_definition.aws_task_definition_arn or self.task_definition.full_arn,
            'desiredCount': self.desired_count,
            'launchType': self.cluster.launch_type.upper(),
            'deploymentController': {
                'type': self.deployment_controller
            },
            'deploymentConfiguration': {
                'minimumHealthyPercent': self.deployment_minimum_percent,
                'maximumPercent': self.deployment_maximum_percent,
            },
        }

        # Add network configuration for Fargate
        if self.cluster.launch_type == ECSCluster.LaunchType.FARGATE:
            service_def['networkConfiguration'] = {
                'awsvpcConfiguration': {
                    'subnets': self.cluster.subnet_ids,
                    'securityGroups': self.cluster.security_group_ids,
                    'assignPublicIp': 'ENABLED',  # Can be configurable
                }
            }

        # Add load balancer if configured
        if self.load_balancer_arn and self.container_name_for_lb:
            service_def['loadBalancers'] = [{
                'targetGroupArn': self.load_balancer_arn,
                'containerName': self.container_name_for_lb,
                'containerPort': self.container_port_for_lb,
            }]

        # Add Service Connect if enabled
        if self.service_connect_enabled and self.service_connect_namespace:
            sc_config = {
                'enabled': True,
                'namespace': self.service_connect_namespace,
            }
            if self.service_connect_port_name and self.container_port_for_lb:
                sc_config['services'] = [{
                    'portName': self.service_connect_port_name,
                    'discoveryName': self.name,
                    'clientAliases': [{
                        'port': self.container_port_for_lb,
                        'dnsName': self.name,
                    }],
                }]
            service_def['serviceConnectConfiguration'] = sc_config

        return service_def


class ECRRepository(TimestampedModel):
    """
    Tracks ECR repositories created by remote-compose.

    ECR repositories store Docker container images that are deployed to ECS.
    This model tracks both managed repositories (created by remote-compose)
    and external repositories that are referenced for deployments.
    """

    # Identification
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Repository name in ECR"
    )

    # AWS Details
    aws_repository_arn = models.CharField(
        max_length=512,
        unique=True,
        db_index=True,
        help_text="AWS ECR Repository ARN"
    )
    aws_repository_uri = models.CharField(
        max_length=512,
        help_text="Full URI for pushing/pulling images"
    )
    aws_region = models.CharField(
        max_length=50,
        help_text="AWS region where repository is located"
    )

    # Relationships
    cluster = models.ForeignKey(
        ECSCluster,
        on_delete=models.CASCADE,
        related_name='ecr_repositories',
        help_text="ECS cluster this repository is associated with"
    )
    aws_credential = models.ForeignKey(
        'SecureCredential',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ecr_repositories',
        help_text="AWS credentials for accessing this repository"
    )

    # Management
    is_managed = models.BooleanField(
        default=True,
        help_text="Whether this repository was created by remote-compose"
    )

    # Extensible metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional repository configuration and state"
    )

    class Meta:
        db_table = 'remote_compose_ecr_repositories'
        ordering = ['-created_at']
        verbose_name = 'ECR Repository'
        verbose_name_plural = 'ECR Repositories'
        unique_together = [['cluster', 'name']]
        indexes = [
            models.Index(fields=['cluster', 'name']),
            models.Index(fields=['aws_region']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.aws_region})"

    def __repr__(self) -> str:
        return f"<ECRRepository: {self.name} cluster={self.cluster.name}>"

    @property
    def image_uri(self) -> str:
        """Return the base URI for tagging images."""
        return self.aws_repository_uri

    def update_from_aws(self, aws_response: dict) -> None:
        """
        Update local state from AWS ECR describe_repositories response.

        Args:
            aws_response: Repository dict from boto3 ecr.describe_repositories
        """
        self.aws_repository_arn = aws_response.get('repositoryArn', self.aws_repository_arn)
        self.aws_repository_uri = aws_response.get('repositoryUri', self.aws_repository_uri)
        self.name = aws_response.get('repositoryName', self.name)

        # Store additional metadata from AWS
        self.metadata.update({
            'registry_id': aws_response.get('registryId'),
            'image_tag_mutability': aws_response.get('imageTagMutability'),
            'image_scanning_configuration': aws_response.get('imageScanningConfiguration'),
            'encryption_configuration': aws_response.get('encryptionConfiguration'),
        })

        self.save(update_fields=[
            'aws_repository_arn', 'aws_repository_uri', 'name',
            'metadata', 'updated_at'
        ])

    def get_image_tag_uri(self, tag: str = 'latest') -> str:
        """
        Get the full URI for a specific image tag.

        Args:
            tag: Image tag (default: 'latest')

        Returns:
            Full image URI with tag (e.g., '123456789.dkr.ecr.us-east-1.amazonaws.com/my-repo:latest')
        """
        return f"{self.aws_repository_uri}:{tag}"


class EFSFileSystem(TimestampedModel):
    """
    Tracks EFS file systems created for ECS persistent volumes.

    EFS provides shared, persistent storage that can be mounted by multiple
    ECS tasks across availability zones. This is essential for stateful
    applications that need persistent storage in Fargate.
    """

    # Identification
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Friendly name for this file system"
    )

    # AWS Details
    aws_file_system_id = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="AWS EFS File System ID (e.g., fs-12345678)"
    )
    aws_region = models.CharField(
        max_length=50,
        help_text="AWS region where file system is located"
    )

    # Relationships
    cluster = models.ForeignKey(
        ECSCluster,
        on_delete=models.CASCADE,
        related_name='efs_file_systems',
        help_text="ECS cluster this file system is associated with"
    )
    aws_credential = models.ForeignKey(
        'SecureCredential',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='efs_file_systems',
        help_text="AWS credentials for managing this file system"
    )

    # Networking
    vpc_id = models.CharField(
        max_length=50,
        help_text="VPC ID where the file system is deployed"
    )
    security_group_id = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Security group ID for mount target access"
    )

    # Management
    is_managed = models.BooleanField(
        default=True,
        help_text="Whether this file system was created by remote-compose"
    )

    # EFS Access Points and Mount Targets
    access_points = models.JSONField(
        default=dict,
        blank=True,
        help_text="Mapping of volume names to EFS access point IDs"
    )
    mount_targets = models.JSONField(
        default=list,
        blank=True,
        help_text="List of mount target IDs for this file system"
    )

    # Extensible metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional file system configuration and state"
    )

    class Meta:
        db_table = 'remote_compose_efs_file_systems'
        ordering = ['-created_at']
        verbose_name = 'EFS File System'
        verbose_name_plural = 'EFS File Systems'
        indexes = [
            models.Index(fields=['cluster', 'name']),
            models.Index(fields=['aws_region']),
            models.Index(fields=['vpc_id']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.aws_file_system_id})"

    def __repr__(self) -> str:
        return f"<EFSFileSystem: {self.name} id={self.aws_file_system_id}>"

    @property
    def is_ready(self) -> bool:
        """Check if file system has mount targets configured."""
        return bool(self.mount_targets)

    def update_from_aws(self, aws_response: dict) -> None:
        """
        Update local state from AWS EFS describe_file_systems response.

        Args:
            aws_response: FileSystem dict from boto3 efs.describe_file_systems
        """
        self.aws_file_system_id = aws_response.get('FileSystemId', self.aws_file_system_id)
        self.name = aws_response.get('Name', self.name) or self.name

        # Store additional metadata from AWS
        self.metadata.update({
            'life_cycle_state': aws_response.get('LifeCycleState'),
            'size_in_bytes': aws_response.get('SizeInBytes'),
            'performance_mode': aws_response.get('PerformanceMode'),
            'throughput_mode': aws_response.get('ThroughputMode'),
            'encrypted': aws_response.get('Encrypted'),
            'creation_token': aws_response.get('CreationToken'),
            'owner_id': aws_response.get('OwnerId'),
        })

        self.save(update_fields=[
            'aws_file_system_id', 'name', 'metadata', 'updated_at'
        ])

    def update_mount_targets(self, mount_target_ids: list) -> None:
        """
        Update the list of mount target IDs.

        Args:
            mount_target_ids: List of mount target IDs from AWS
        """
        self.mount_targets = mount_target_ids
        self.save(update_fields=['mount_targets', 'updated_at'])

    def add_access_point(self, volume_name: str, access_point_id: str) -> None:
        """
        Add or update an access point mapping.

        Args:
            volume_name: Name of the volume (from docker-compose)
            access_point_id: AWS EFS Access Point ID
        """
        self.access_points[volume_name] = access_point_id
        self.save(update_fields=['access_points', 'updated_at'])

    def get_access_point_id(self, volume_name: str) -> str | None:
        """
        Get the access point ID for a volume.

        Args:
            volume_name: Name of the volume

        Returns:
            Access point ID or None if not found
        """
        return self.access_points.get(volume_name)

    def to_ecs_volume_config(self, volume_name: str) -> dict:
        """
        Generate ECS volume configuration for a task definition.

        Args:
            volume_name: Name of the volume

        Returns:
            Dict suitable for ECS task definition volume configuration
        """
        config = {
            'name': volume_name,
            'efsVolumeConfiguration': {
                'fileSystemId': self.aws_file_system_id,
                'transitEncryption': 'ENABLED',
            }
        }

        access_point_id = self.get_access_point_id(volume_name)
        if access_point_id:
            config['efsVolumeConfiguration']['authorizationConfig'] = {
                'accessPointId': access_point_id,
                'iam': 'ENABLED'
            }

        return config
