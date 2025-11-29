"""
Deployment target models.
"""

from django.db import models
from .base import TimestampedModel


class DeploymentTarget(TimestampedModel):
    """
    Represents a remote deployment target (EC2 instance, remote server, etc.)
    """

    class TargetType(models.TextChoices):
        SSH = 'ssh', 'SSH Connection'
        TCP = 'tcp', 'TCP Connection'
        UNIX = 'unix', 'Unix Socket'
        ECS = 'ecs', 'AWS ECS'

    class Environment(models.TextChoices):
        DEVELOPMENT = 'development', 'Development'
        STAGING = 'staging', 'Staging'
        PRODUCTION = 'production', 'Production'

    class HealthStatus(models.TextChoices):
        HEALTHY = 'healthy', 'Healthy'
        UNHEALTHY = 'unhealthy', 'Unhealthy'
        UNKNOWN = 'unknown', 'Unknown'

    # Identification
    name = models.CharField(max_length=255, unique=True, db_index=True)
    description = models.TextField(blank=True)

    # Connection Details
    target_type = models.CharField(
        max_length=20,
        choices=TargetType.choices,
        default=TargetType.SSH
    )
    host = models.CharField(max_length=255)
    port = models.IntegerField(default=22)
    username = models.CharField(max_length=255, default='ubuntu')

    # SSH Key (reference to SecureCredential)
    ssh_key = models.ForeignKey(
        'SecureCredential',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='targets'
    )

    # AWS Integration (optional)
    aws_instance_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True
    )
    aws_region = models.CharField(max_length=50, null=True, blank=True)

    # ECS Integration (for ECS target type)
    ecs_cluster = models.ForeignKey(
        'ECSCluster',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deployment_targets',
        help_text="ECS cluster for ECS target type"
    )
    ecs_service = models.ForeignKey(
        'ECSService',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deployment_targets',
        help_text="Default ECS service for deployments"
    )

    # Environment and Status
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.DEVELOPMENT
    )
    is_active = models.BooleanField(default=True, db_index=True)
    health_status = models.CharField(
        max_length=20,
        choices=HealthStatus.choices,
        default=HealthStatus.UNKNOWN
    )
    last_health_check = models.DateTimeField(null=True, blank=True)

    # Extensible metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'remote_compose_deployment_targets'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['target_type', 'is_active']),
            models.Index(fields=['environment', 'is_active']),
            models.Index(fields=['aws_instance_id']),
        ]

    def __str__(self):
        return f"{self.name} ({self.host})"

    @property
    def connection_string(self):
        """Return connection string for this target."""
        if self.target_type == self.TargetType.SSH:
            return f"ssh://{self.username}@{self.host}:{self.port}"
        elif self.target_type == self.TargetType.TCP:
            return f"tcp://{self.host}:{self.port}"
        elif self.target_type == self.TargetType.UNIX:
            return f"unix://{self.host}"
        elif self.target_type == self.TargetType.ECS:
            cluster_name = self.ecs_cluster.aws_cluster_name if self.ecs_cluster else 'unknown'
            return f"ecs://{self.aws_region or 'us-east-1'}/{cluster_name}"
        return self.host

    @property
    def is_ecs_target(self):
        """Check if this is an ECS target type."""
        return self.target_type == self.TargetType.ECS

    def mark_healthy(self):
        """Mark target as healthy."""
        from django.utils import timezone
        self.health_status = self.HealthStatus.HEALTHY
        self.last_health_check = timezone.now()
        self.save(update_fields=['health_status', 'last_health_check', 'updated_at'])

    def mark_unhealthy(self):
        """Mark target as unhealthy."""
        from django.utils import timezone
        self.health_status = self.HealthStatus.UNHEALTHY
        self.last_health_check = timezone.now()
        self.save(update_fields=['health_status', 'last_health_check', 'updated_at'])
