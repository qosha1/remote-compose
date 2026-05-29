"""
Docker context models.
"""

from django.db import models
from .base import TimestampedModel


class DockerContext(TimestampedModel):
    """
    Represents a Docker context configuration for a deployment target.
    """

    class ContextType(models.TextChoices):
        SSH = "ssh", "SSH"
        TCP = "tcp", "TCP"
        UNIX = "unix", "Unix Socket"

    # Identification
    name = models.CharField(max_length=255, unique=True, db_index=True)
    description = models.TextField(blank=True)

    # Link to deployment target
    target = models.ForeignKey(
        "DeploymentTarget", on_delete=models.CASCADE, related_name="contexts"
    )

    # Context configuration
    context_type = models.CharField(max_length=20, choices=ContextType.choices)
    endpoint = models.CharField(max_length=512)  # Full connection string
    tls_verify = models.BooleanField(default=True)

    # State
    is_default = models.BooleanField(default=False)
    is_synced = models.BooleanField(
        default=False,
        help_text="Whether this context is synced with local Docker daemon",
    )
    last_used_at = models.DateTimeField(null=True, blank=True)

    # Extensible metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_docker_contexts"
        ordering = ["-created_at"]
        verbose_name_plural = "Docker Contexts"

    def __str__(self):
        return f"{self.name} -> {self.target.name}"

    def save(self, *args, **kwargs):
        """Ensure only one default context exists."""
        if self.is_default:
            DockerContext.objects.filter(is_default=True).exclude(pk=self.pk).update(
                is_default=False
            )
        super().save(*args, **kwargs)

    def mark_used(self):
        """Update last_used_at timestamp."""
        from django.utils import timezone

        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at", "updated_at"])

    @classmethod
    def get_default(cls):
        """Get the default context, if any."""
        return cls.objects.filter(is_default=True).first()
