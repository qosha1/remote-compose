"""
Deployment tracking models.
"""

from django.db import models
from .base import TimestampedModel


class Deployment(TimestampedModel):
    """
    Tracks deployment executions and history.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        ROLLED_BACK = "rolled_back", "Rolled Back"
        CANCELLED = "cancelled", "Cancelled"

    class DeploymentType(models.TextChoices):
        DEPLOY = "deploy", "Deploy"
        ROLLBACK = "rollback", "Rollback"
        UPDATE = "update", "Update"
        RESTART = "restart", "Restart"

    # Relationships
    context = models.ForeignKey(
        "DockerContext", on_delete=models.PROTECT, related_name="deployments"
    )
    target = models.ForeignKey(
        "DeploymentTarget", on_delete=models.PROTECT, related_name="deployments"
    )

    # Compose file information
    compose_file_path = models.CharField(max_length=1024)
    compose_content = models.TextField(
        help_text="Snapshot of compose file at deployment time"
    )
    project_name = models.CharField(
        max_length=255, blank=True, help_text="Docker Compose project name"
    )

    # Environment and configuration
    environment = models.JSONField(
        default=dict, blank=True, help_text="Environment variables passed to deployment"
    )

    # Status tracking
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    deployment_type = models.CharField(
        max_length=20, choices=DeploymentType.choices, default=DeploymentType.DEPLOY
    )

    # Timestamps
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Results
    error_message = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)

    # User tracking
    deployed_by = models.CharField(max_length=255, blank=True)

    # Rollback support (self-reference)
    parent_deployment = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rollbacks",
        help_text="Original deployment if this is a rollback",
    )

    # Version tracking
    version = models.CharField(
        max_length=100,
        blank=True,
        help_text="Version tag (git commit, release tag, etc.)",
    )

    # Container information
    container_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="List of container IDs created by this deployment",
    )
    service_status = models.JSONField(
        default=dict, blank=True, help_text="Per-service status information"
    )

    # Extensible metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_deployments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["target", "status"]),
            models.Index(fields=["context", "created_at"]),
            models.Index(fields=["project_name", "target"]),
        ]

    def __str__(self):
        return f"Deployment {self.id} - {self.status}"

    @property
    def duration(self):
        """Return deployment duration in seconds, if completed."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def is_terminal(self):
        """Return True if deployment is in a terminal state."""
        return self.status in [
            self.Status.SUCCESS,
            self.Status.FAILED,
            self.Status.ROLLED_BACK,
            self.Status.CANCELLED,
        ]

    def start(self):
        """Mark deployment as started."""
        from django.utils import timezone

        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at", "updated_at"])

    def succeed(self, container_ids=None, service_status=None):
        """Mark deployment as successful."""
        from django.utils import timezone

        self.status = self.Status.SUCCESS
        self.completed_at = timezone.now()
        if container_ids:
            self.container_ids = container_ids
        if service_status:
            self.service_status = service_status
        self.save(
            update_fields=[
                "status",
                "completed_at",
                "container_ids",
                "service_status",
                "updated_at",
            ]
        )

    def fail(self, error_message, exit_code=None):
        """Mark deployment as failed."""
        from django.utils import timezone

        self.status = self.Status.FAILED
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.exit_code = exit_code
        self.save(
            update_fields=[
                "status",
                "completed_at",
                "error_message",
                "exit_code",
                "updated_at",
            ]
        )

    def cancel(self):
        """Mark deployment as cancelled."""
        from django.utils import timezone

        self.status = self.Status.CANCELLED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at", "updated_at"])

    def mark_rolled_back(self):
        """Mark deployment as rolled back."""
        self.status = self.Status.ROLLED_BACK
        self.save(update_fields=["status", "updated_at"])


class DeploymentLog(models.Model):
    """
    Stores detailed logs for each deployment.
    """

    class LogLevel(models.TextChoices):
        DEBUG = "debug", "Debug"
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    deployment = models.ForeignKey(
        Deployment, on_delete=models.CASCADE, related_name="logs"
    )
    log_level = models.CharField(
        max_length=20, choices=LogLevel.choices, default=LogLevel.INFO, db_index=True
    )
    message = models.TextField()
    command = models.TextField(blank=True, help_text="Command that was executed")
    output = models.TextField(blank=True, help_text="Command output")
    service_name = models.CharField(
        max_length=255, blank=True, help_text="Specific service if applicable"
    )
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "remote_compose_deployment_logs"
        ordering = ["timestamp"]
        indexes = [
            models.Index(fields=["deployment", "timestamp"]),
            models.Index(fields=["log_level", "timestamp"]),
        ]

    def __str__(self):
        return f"[{self.log_level}] {self.message[:50]}"

    @classmethod
    def log(
        cls, deployment, message, level="info", command="", output="", service_name=""
    ):
        """Create a new log entry."""
        return cls.objects.create(
            deployment=deployment,
            log_level=level,
            message=message,
            command=command,
            output=output,
            service_name=service_name,
        )
