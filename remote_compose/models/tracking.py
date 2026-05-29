"""
ECS deployment tracking models.

These models provide detailed tracking of build records, deployment events,
and resource metrics for ECS deployments.
"""

from django.db import models
from .base import TimestampedModel


class BuildRecord(TimestampedModel):
    """
    Tracks container image builds during ECS deployments.

    Each build record represents a single container image build for a service,
    tracking the build context, image URI, and timing information.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        BUILDING = "building", "Building"
        PUSHING = "pushing", "Pushing to ECR"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (cached)"

    # Relationships
    deployment = models.ForeignKey(
        "Deployment",
        on_delete=models.CASCADE,
        related_name="build_records",
        help_text="Deployment this build is part of",
    )
    ecr_repository = models.ForeignKey(
        "ECRRepository",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="build_records",
        help_text="ECR repository the image was pushed to",
    )

    # Build identification
    service_name = models.CharField(
        max_length=255, db_index=True, help_text="Docker Compose service name"
    )
    image_uri = models.CharField(
        max_length=512,
        blank=True,
        help_text="Full image URI with tag (e.g., account.dkr.ecr.region.amazonaws.com/repo:tag)",
    )
    image_tag = models.CharField(max_length=128, blank=True, help_text="Image tag")

    # Build context tracking
    context_path = models.CharField(
        max_length=1024, blank=True, help_text="Docker build context path"
    )
    dockerfile_path = models.CharField(
        max_length=1024, blank=True, help_text="Dockerfile path relative to context"
    )
    context_hash = models.CharField(
        max_length=64,
        db_index=True,
        blank=True,
        help_text="SHA256 hash of build context for cache detection",
    )

    # Status and timing
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    build_started_at = models.DateTimeField(
        null=True, blank=True, help_text="When the Docker build started"
    )
    build_completed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the Docker build completed"
    )
    push_started_at = models.DateTimeField(
        null=True, blank=True, help_text="When ECR push started"
    )
    push_completed_at = models.DateTimeField(
        null=True, blank=True, help_text="When ECR push completed"
    )

    # Results
    error_message = models.TextField(
        blank=True, help_text="Error message if build failed"
    )
    build_log = models.TextField(blank=True, help_text="Docker build output log")
    image_digest = models.CharField(
        max_length=128, blank=True, help_text="Image digest from ECR after push"
    )
    image_size_bytes = models.BigIntegerField(
        null=True, blank=True, help_text="Image size in bytes"
    )

    # Build configuration
    build_args = models.JSONField(
        default=dict, blank=True, help_text="Docker build arguments"
    )
    platform = models.CharField(
        max_length=50, default="linux/amd64", help_text="Target platform for the build"
    )

    # Cache information
    cache_hit = models.BooleanField(
        default=False, help_text="Whether this build used cached layers"
    )
    previous_build = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subsequent_builds",
        help_text="Previous build record for this service",
    )

    # Extensible metadata
    metadata = models.JSONField(
        default=dict, blank=True, help_text="Additional build metadata"
    )

    class Meta:
        db_table = "remote_compose_build_records"
        ordering = ["-created_at"]
        verbose_name = "Build Record"
        verbose_name_plural = "Build Records"
        indexes = [
            models.Index(fields=["deployment", "service_name"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["context_hash"]),
            models.Index(fields=["ecr_repository", "image_tag"]),
        ]

    def __str__(self) -> str:
        return f"{self.service_name} ({self.status})"

    @property
    def build_duration(self) -> float | None:
        """Return build duration in seconds."""
        if self.build_started_at and self.build_completed_at:
            return (self.build_completed_at - self.build_started_at).total_seconds()
        return None

    @property
    def push_duration(self) -> float | None:
        """Return push duration in seconds."""
        if self.push_started_at and self.push_completed_at:
            return (self.push_completed_at - self.push_started_at).total_seconds()
        return None

    @property
    def total_duration(self) -> float | None:
        """Return total duration from build start to push complete."""
        if self.build_started_at and self.push_completed_at:
            return (self.push_completed_at - self.build_started_at).total_seconds()
        return None

    def start_build(self):
        """Mark build as started."""
        from django.utils import timezone

        self.status = self.Status.BUILDING
        self.build_started_at = timezone.now()
        self.save(update_fields=["status", "build_started_at", "updated_at"])

    def complete_build(self):
        """Mark build as completed (ready for push)."""
        from django.utils import timezone

        self.status = self.Status.PUSHING
        self.build_completed_at = timezone.now()
        self.push_started_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "build_completed_at",
                "push_started_at",
                "updated_at",
            ]
        )

    def succeed(
        self, image_uri: str, image_digest: str = "", image_size_bytes: int = None
    ):
        """Mark build as successful."""
        from django.utils import timezone

        self.status = self.Status.SUCCESS
        self.push_completed_at = timezone.now()
        self.image_uri = image_uri
        self.image_digest = image_digest
        if image_size_bytes:
            self.image_size_bytes = image_size_bytes
        self.save(
            update_fields=[
                "status",
                "push_completed_at",
                "image_uri",
                "image_digest",
                "image_size_bytes",
                "updated_at",
            ]
        )

    def fail(self, error_message: str, build_log: str = ""):
        """Mark build as failed."""
        from django.utils import timezone

        self.status = self.Status.FAILED
        self.error_message = error_message
        if build_log:
            self.build_log = build_log
        if not self.build_completed_at:
            self.build_completed_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "error_message",
                "build_log",
                "build_completed_at",
                "updated_at",
            ]
        )

    def mark_skipped(self, image_uri: str, reason: str = "Using cached image"):
        """Mark build as skipped (using cache)."""
        self.status = self.Status.SKIPPED
        self.image_uri = image_uri
        self.cache_hit = True
        if reason:
            self.metadata["skip_reason"] = reason
        self.save(
            update_fields=["status", "image_uri", "cache_hit", "metadata", "updated_at"]
        )


class DeploymentEvent(TimestampedModel):
    """
    Tracks state transitions and significant events during deployments.

    Provides a structured event log for deployment lifecycle tracking,
    enabling detailed auditing and debugging of deployment processes.
    """

    class EventType(models.TextChoices):
        # Deployment lifecycle
        DEPLOYMENT_STARTED = "deployment_started", "Deployment Started"
        DEPLOYMENT_COMPLETED = "deployment_completed", "Deployment Completed"
        DEPLOYMENT_FAILED = "deployment_failed", "Deployment Failed"

        # Build phase
        BUILD_STARTED = "build_started", "Build Started"
        BUILD_COMPLETED = "build_completed", "Build Completed"
        BUILD_FAILED = "build_failed", "Build Failed"
        IMAGE_PUSHED = "image_pushed", "Image Pushed to ECR"

        # Infrastructure setup
        EFS_CREATED = "efs_created", "EFS File System Created"
        EFS_CONFIGURED = "efs_configured", "EFS Configured"
        ACCESS_POINT_CREATED = "access_point_created", "Access Point Created"

        # ECS deployment
        TASK_DEF_REGISTERED = "task_def_registered", "Task Definition Registered"
        SERVICE_CREATED = "service_created", "ECS Service Created"
        SERVICE_UPDATED = "service_updated", "ECS Service Updated"
        SERVICE_STABLE = "service_stable", "Service Reached Stable State"
        TASK_STARTED = "task_started", "Task Started"
        TASK_STOPPED = "task_stopped", "Task Stopped"

        # State changes
        STATE_CHANGE = "state_change", "State Change"

        # Errors and warnings
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"

        # Rollback
        ROLLBACK_STARTED = "rollback_started", "Rollback Started"
        ROLLBACK_COMPLETED = "rollback_completed", "Rollback Completed"

        # Resource operations
        RESOURCE_CREATED = "resource_created", "Resource Created"
        RESOURCE_DELETED = "resource_deleted", "Resource Deleted"
        CLEANUP_STARTED = "cleanup_started", "Cleanup Started"
        CLEANUP_COMPLETED = "cleanup_completed", "Cleanup Completed"

    class Severity(models.TextChoices):
        DEBUG = "debug", "Debug"
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"
        CRITICAL = "critical", "Critical"

    # Relationships
    deployment = models.ForeignKey(
        "Deployment",
        on_delete=models.CASCADE,
        related_name="events",
        help_text="Deployment this event belongs to",
    )

    # Event identification
    event_type = models.CharField(
        max_length=30,
        choices=EventType.choices,
        db_index=True,
        help_text="Type of deployment event",
    )
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.INFO, db_index=True
    )

    # State tracking
    previous_state = models.CharField(
        max_length=50, blank=True, help_text="Previous state before this event"
    )
    new_state = models.CharField(
        max_length=50, blank=True, help_text="New state after this event"
    )

    # Event details
    message = models.TextField(help_text="Human-readable event description")
    service_name = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text="Docker Compose service name if applicable",
    )
    resource_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Type of AWS resource (ecs_service, efs, task_definition, etc.)",
    )
    resource_id = models.CharField(
        max_length=512, blank=True, help_text="AWS resource ID or ARN"
    )

    # Timing
    duration_ms = models.IntegerField(
        null=True, blank=True, help_text="Duration of the operation in milliseconds"
    )

    # Error details
    error_code = models.CharField(
        max_length=100, blank=True, help_text="Error code if this is an error event"
    )
    stack_trace = models.TextField(blank=True, help_text="Stack trace for error events")

    # Structured event data
    metadata = models.JSONField(
        default=dict, blank=True, help_text="Additional event-specific data"
    )

    class Meta:
        db_table = "remote_compose_deployment_events"
        ordering = ["created_at"]
        verbose_name = "Deployment Event"
        verbose_name_plural = "Deployment Events"
        indexes = [
            models.Index(fields=["deployment", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
            models.Index(fields=["severity", "created_at"]),
            models.Index(fields=["service_name", "event_type"]),
            models.Index(fields=["resource_type", "resource_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type}: {self.message[:50]}"

    @classmethod
    def log_event(
        cls,
        deployment,
        event_type: str,
        message: str,
        severity: str = "info",
        service_name: str = "",
        resource_type: str = "",
        resource_id: str = "",
        previous_state: str = "",
        new_state: str = "",
        duration_ms: int = None,
        error_code: str = "",
        stack_trace: str = "",
        metadata: dict = None,
    ):
        """
        Create a deployment event record.

        This is the primary factory method for creating deployment events.
        """
        return cls.objects.create(
            deployment=deployment,
            event_type=event_type,
            message=message,
            severity=severity,
            service_name=service_name,
            resource_type=resource_type,
            resource_id=resource_id,
            previous_state=previous_state,
            new_state=new_state,
            duration_ms=duration_ms,
            error_code=error_code,
            stack_trace=stack_trace,
            metadata=metadata or {},
        )


class ResourceMetric(TimestampedModel):
    """
    Tracks resource usage metrics for ECS services.

    Stores time-series data for CPU, memory, and other metrics
    that can be used for monitoring, alerting, and capacity planning.
    """

    class MetricType(models.TextChoices):
        CPU_UTILIZATION = "cpu_utilization", "CPU Utilization (%)"
        MEMORY_UTILIZATION = "memory_utilization", "Memory Utilization (%)"
        MEMORY_USED = "memory_used", "Memory Used (MB)"
        NETWORK_IN = "network_in", "Network In (bytes)"
        NETWORK_OUT = "network_out", "Network Out (bytes)"
        RUNNING_TASKS = "running_tasks", "Running Tasks"
        PENDING_TASKS = "pending_tasks", "Pending Tasks"
        DESIRED_TASKS = "desired_tasks", "Desired Tasks"
        DEPLOYMENT_COUNT = "deployment_count", "Active Deployments"
        REQUEST_COUNT = "request_count", "Request Count"
        ERROR_COUNT = "error_count", "Error Count"
        RESPONSE_TIME = "response_time", "Response Time (ms)"
        EFS_CONNECTIONS = "efs_connections", "EFS Client Connections"
        EFS_THROUGHPUT = "efs_throughput", "EFS Throughput (bytes/s)"
        EFS_SIZE = "efs_size", "EFS Size (bytes)"

    class AggregationType(models.TextChoices):
        AVERAGE = "avg", "Average"
        SUM = "sum", "Sum"
        MIN = "min", "Minimum"
        MAX = "max", "Maximum"
        COUNT = "count", "Count"
        P50 = "p50", "50th Percentile"
        P90 = "p90", "90th Percentile"
        P99 = "p99", "99th Percentile"

    # Relationships - supports multiple resource types
    ecs_service = models.ForeignKey(
        "ECSService",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="metrics",
        help_text="ECS service this metric is for",
    )
    efs_file_system = models.ForeignKey(
        "EFSFileSystem",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="metrics",
        help_text="EFS file system this metric is for",
    )
    cluster = models.ForeignKey(
        "ECSCluster",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="metrics",
        help_text="ECS cluster this metric is for",
    )

    # Metric identification
    metric_type = models.CharField(
        max_length=30,
        choices=MetricType.choices,
        db_index=True,
        help_text="Type of metric being recorded",
    )
    aggregation = models.CharField(
        max_length=10,
        choices=AggregationType.choices,
        default=AggregationType.AVERAGE,
        help_text="How this metric was aggregated",
    )

    # Metric value
    value = models.FloatField(help_text="Metric value")
    unit = models.CharField(
        max_length=20, blank=True, help_text="Unit of measurement (%, bytes, ms, count)"
    )

    # Time period
    period_start = models.DateTimeField(
        db_index=True, help_text="Start of the measurement period"
    )
    period_end = models.DateTimeField(help_text="End of the measurement period")
    period_seconds = models.IntegerField(
        default=60, help_text="Duration of the measurement period in seconds"
    )

    # Sample information
    sample_count = models.IntegerField(
        default=1, help_text="Number of data points in this aggregation"
    )

    # Additional context
    dimensions = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional dimensions for metric filtering (e.g., task_id, container_name)",
    )

    class Meta:
        db_table = "remote_compose_resource_metrics"
        ordering = ["-period_start"]
        verbose_name = "Resource Metric"
        verbose_name_plural = "Resource Metrics"
        indexes = [
            models.Index(fields=["ecs_service", "metric_type", "period_start"]),
            models.Index(fields=["efs_file_system", "metric_type", "period_start"]),
            models.Index(fields=["cluster", "metric_type", "period_start"]),
            models.Index(fields=["metric_type", "period_start"]),
        ]

    def __str__(self) -> str:
        target = self.ecs_service or self.efs_file_system or self.cluster
        return f"{self.metric_type}: {self.value} ({target})"

    @property
    def period_duration(self) -> float:
        """Return period duration in seconds."""
        return (self.period_end - self.period_start).total_seconds()

    @classmethod
    def _record_metric(
        cls,
        target_field: str,
        target,
        metric_type: str,
        value: float,
        period_start,
        period_end,
        aggregation: str = "avg",
        unit: str = "",
        sample_count: int = 1,
        dimensions: dict = None,
    ):
        """Shared helper for recording a metric against any target type."""
        kwargs = {
            target_field: target,
            "metric_type": metric_type,
            "value": value,
            "period_start": period_start,
            "period_end": period_end,
            "period_seconds": int((period_end - period_start).total_seconds()),
            "aggregation": aggregation,
            "unit": unit,
            "sample_count": sample_count,
            "dimensions": dimensions or {},
        }
        # Automatically set cluster from the target when applicable
        if target_field != "cluster" and hasattr(target, "cluster"):
            kwargs["cluster"] = target.cluster
        return cls.objects.create(**kwargs)

    @classmethod
    def record_ecs_metric(
        cls, ecs_service, metric_type, value, period_start, period_end, **kwargs
    ):
        """Record a metric for an ECS service."""
        return cls._record_metric(
            "ecs_service",
            ecs_service,
            metric_type,
            value,
            period_start,
            period_end,
            **kwargs,
        )

    @classmethod
    def record_efs_metric(
        cls, efs_file_system, metric_type, value, period_start, period_end, **kwargs
    ):
        """Record a metric for an EFS file system."""
        return cls._record_metric(
            "efs_file_system",
            efs_file_system,
            metric_type,
            value,
            period_start,
            period_end,
            **kwargs,
        )

    @classmethod
    def record_cluster_metric(
        cls, cluster, metric_type, value, period_start, period_end, **kwargs
    ):
        """Record a metric for an ECS cluster."""
        return cls._record_metric(
            "cluster", cluster, metric_type, value, period_start, period_end, **kwargs
        )
