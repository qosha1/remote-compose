"""
Audit log model for tracking all deployment-related actions.
"""

from django.db import models
from django.utils import timezone


class AuditLog(models.Model):
    """
    Database model for storing audit logs.

    Tracks all deployment-related actions for security and compliance.
    """

    class Meta:
        db_table = 'remote_compose_audit_log'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['action']),
            models.Index(fields=['actor']),
            models.Index(fields=['resource_type', 'resource_id']),
            models.Index(fields=['timestamp']),
            models.Index(fields=['success']),
        ]
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'

    action = models.CharField(
        max_length=100,
        db_index=True,
        help_text='Type of action performed',
    )
    actor = models.CharField(
        max_length=255,
        db_index=True,
        help_text='User or system that performed the action',
    )
    timestamp = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text='When the action occurred',
    )

    resource_type = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text='Type of resource affected (e.g., deployment, target)',
    )
    resource_id = models.IntegerField(
        null=True,
        blank=True,
        help_text='ID of the resource affected',
    )
    resource_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text='Name of the resource affected',
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text='IP address of the actor',
    )
    user_agent = models.TextField(
        null=True,
        blank=True,
        help_text='User agent string if applicable',
    )

    details = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional details about the action',
    )
    success = models.BooleanField(
        default=True,
        help_text='Whether the action succeeded',
    )
    error_message = models.TextField(
        null=True,
        blank=True,
        help_text='Error message if action failed',
    )

    def __str__(self):
        return f"{self.timestamp} - {self.action} by {self.actor}"

    @classmethod
    def log(
        cls,
        action: str,
        actor: str,
        resource_type: str = None,
        resource_id: int = None,
        resource_name: str = None,
        ip_address: str = None,
        user_agent: str = None,
        details: dict = None,
        success: bool = True,
        error_message: str = None,
    ) -> 'AuditLog':
        """
        Create an audit log entry.

        Args:
            action: Type of action
            actor: User or system performing the action
            resource_type: Type of resource affected
            resource_id: ID of resource
            resource_name: Name of resource
            ip_address: Actor's IP address
            user_agent: Actor's user agent
            details: Additional details
            success: Whether action succeeded
            error_message: Error message if failed

        Returns:
            Created AuditLog instance
        """
        return cls.objects.create(
            action=action,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details or {},
            success=success,
            error_message=error_message,
        )
