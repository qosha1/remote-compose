"""
Audit logging service for tracking all deployment-related actions.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import timedelta
from enum import Enum
from typing import Optional, Dict, Any

from django.db.models import QuerySet
from django.utils import timezone

from ..models.audit import AuditLog
from ..conf import get_setting
from .base import BaseService

logger = logging.getLogger(__name__)


class AuditAction(str, Enum):
    """Types of auditable actions."""

    # Target actions
    TARGET_CREATED = "target.created"
    TARGET_UPDATED = "target.updated"
    TARGET_DELETED = "target.deleted"
    TARGET_CONNECTION_TEST = "target.connection_test"

    # Context actions
    CONTEXT_CREATED = "context.created"
    CONTEXT_UPDATED = "context.updated"
    CONTEXT_DELETED = "context.deleted"

    # Deployment actions
    DEPLOYMENT_STARTED = "deployment.started"
    DEPLOYMENT_COMPLETED = "deployment.completed"
    DEPLOYMENT_FAILED = "deployment.failed"
    DEPLOYMENT_CANCELLED = "deployment.cancelled"
    DEPLOYMENT_STOPPED = "deployment.stopped"

    # Rollback actions
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_COMPLETED = "rollback.completed"
    ROLLBACK_FAILED = "rollback.failed"

    # Credential actions
    CREDENTIAL_CREATED = "credential.created"
    CREDENTIAL_ACCESSED = "credential.accessed"
    CREDENTIAL_ROTATED = "credential.rotated"
    CREDENTIAL_DELETED = "credential.deleted"

    # Health check actions
    HEALTH_CHECK_RUN = "health_check.run"
    HEALTH_CHECK_FAILED = "health_check.failed"

    # Security actions
    RATE_LIMIT_EXCEEDED = "security.rate_limit_exceeded"
    AUTHENTICATION_FAILED = "security.authentication_failed"
    VALIDATION_FAILED = "security.validation_failed"


@dataclass
class AuditEntry:
    """Audit log entry."""

    action: str
    actor: str
    timestamp: str
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    resource_name: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    success: bool = True
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class AuditService(BaseService):
    """
    Service for audit logging.

    Provides centralized audit logging for all deployment-related actions.
    Supports multiple output backends: database, file, and external logging.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._logger = logging.getLogger("remote_compose.audit")
        self._setup_logger()

    def _setup_logger(self):
        """Setup dedicated audit logger."""
        audit_log_file = get_setting("AUDIT_LOG_FILE")
        if audit_log_file:
            handler = logging.FileHandler(audit_log_file)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                )
            )
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)

    def log(
        self,
        action: AuditAction,
        actor: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[int] = None,
        resource_name: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> AuditEntry:
        """
        Create an audit log entry.

        Args:
            action: Type of action being logged
            actor: User or system performing the action
            resource_type: Type of resource affected
            resource_id: ID of resource affected
            resource_name: Name of resource affected
            ip_address: IP address of the actor
            user_agent: User agent string
            details: Additional details about the action
            success: Whether the action succeeded
            error_message: Error message if action failed

        Returns:
            AuditEntry
        """
        # Sanitize details before logging
        sanitized_details = self._sanitize_details(details) if details else {}

        entry = AuditEntry(
            action=action.value if isinstance(action, AuditAction) else action,
            actor=actor,
            timestamp=timezone.now().isoformat(),
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            ip_address=ip_address,
            user_agent=user_agent,
            details=sanitized_details,
            success=success,
            error_message=error_message,
        )

        # Log to file
        self._logger.info(entry.to_json())

        # Log to database if enabled
        if get_setting("AUDIT_LOG_TO_DATABASE", True):
            self._save_to_database(entry)

        # Notify observers
        self.notify_observers("audit_logged", entry=entry)

        return entry

    def _save_to_database(self, entry: AuditEntry) -> None:
        """Save audit entry to database."""
        try:
            AuditLog.objects.create(
                action=entry.action,
                actor=entry.actor,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                resource_name=entry.resource_name,
                ip_address=entry.ip_address,
                user_agent=entry.user_agent,
                details=entry.details,
                success=entry.success,
                error_message=entry.error_message,
            )
        except Exception as e:
            logger.error(f"Failed to save audit log to database: {e}")

    def _sanitize_details(self, details: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize sensitive data from details."""
        from .log_sanitizer import LogSanitizer

        sanitizer = LogSanitizer()
        return sanitizer.sanitize_dict(details)

    # Convenience methods for common actions

    def log_deployment_started(
        self,
        deployment,
        actor: str,
        ip_address: Optional[str] = None,
    ) -> AuditEntry:
        """Log deployment started."""
        return self.log(
            action=AuditAction.DEPLOYMENT_STARTED,
            actor=actor,
            resource_type="deployment",
            resource_id=deployment.id,
            resource_name=deployment.project_name,
            ip_address=ip_address,
            details={
                "target_id": deployment.target_id,
                "target_name": deployment.target.name,
                "version": deployment.version,
                "deployment_type": deployment.deployment_type,
            },
        )

    def log_deployment_completed(
        self,
        deployment,
        actor: str,
        ip_address: Optional[str] = None,
    ) -> AuditEntry:
        """Log deployment completed."""
        return self.log(
            action=AuditAction.DEPLOYMENT_COMPLETED,
            actor=actor,
            resource_type="deployment",
            resource_id=deployment.id,
            resource_name=deployment.project_name,
            ip_address=ip_address,
            details={
                "target_id": deployment.target_id,
                "target_name": deployment.target.name,
                "version": deployment.version,
                "duration_seconds": deployment.duration,
                "container_ids": deployment.container_ids,
            },
        )

    def log_deployment_failed(
        self,
        deployment,
        actor: str,
        error: str,
        ip_address: Optional[str] = None,
    ) -> AuditEntry:
        """Log deployment failed."""
        return self.log(
            action=AuditAction.DEPLOYMENT_FAILED,
            actor=actor,
            resource_type="deployment",
            resource_id=deployment.id,
            resource_name=deployment.project_name,
            ip_address=ip_address,
            success=False,
            error_message=error,
            details={
                "target_id": deployment.target_id,
                "target_name": deployment.target.name,
                "version": deployment.version,
            },
        )

    def log_credential_access(
        self,
        credential,
        actor: str,
        ip_address: Optional[str] = None,
    ) -> AuditEntry:
        """Log credential access."""
        return self.log(
            action=AuditAction.CREDENTIAL_ACCESSED,
            actor=actor,
            resource_type="credential",
            resource_id=credential.id,
            resource_name=credential.name,
            ip_address=ip_address,
            details={
                "credential_type": credential.credential_type,
            },
        )

    def log_rate_limit_exceeded(
        self,
        actor: str,
        limit_type: str,
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        """Log rate limit exceeded."""
        return self.log(
            action=AuditAction.RATE_LIMIT_EXCEEDED,
            actor=actor,
            ip_address=ip_address,
            success=False,
            error_message=f"Rate limit exceeded: {limit_type}",
            details=details,
        )

    def query_logs(
        self,
        action: Optional[str] = None,
        actor: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[int] = None,
        success: Optional[bool] = None,
        start_date: Optional[timezone.datetime] = None,
        end_date: Optional[timezone.datetime] = None,
        limit: int = 100,
    ) -> QuerySet:
        """
        Query audit logs with filters.

        Args:
            action: Filter by action type
            actor: Filter by actor
            resource_type: Filter by resource type
            resource_id: Filter by resource ID
            success: Filter by success status
            start_date: Filter by start date
            end_date: Filter by end date
            limit: Maximum results

        Returns:
            QuerySet of AuditLog entries
        """
        qs = AuditLog.objects.all()

        if action:
            qs = qs.filter(action=action)
        if actor:
            qs = qs.filter(actor=actor)
        if resource_type:
            qs = qs.filter(resource_type=resource_type)
        if resource_id:
            qs = qs.filter(resource_id=resource_id)
        if success is not None:
            qs = qs.filter(success=success)
        if start_date:
            qs = qs.filter(timestamp__gte=start_date)
        if end_date:
            qs = qs.filter(timestamp__lte=end_date)

        return qs[:limit]

    def get_activity_summary(
        self,
        hours: int = 24,
    ) -> Dict[str, Any]:
        """
        Get summary of recent audit activity.

        Args:
            hours: Number of hours to look back

        Returns:
            Dict with activity summary
        """
        since = timezone.now() - timedelta(hours=hours)
        logs = AuditLog.objects.filter(timestamp__gte=since)

        # Count by action
        action_counts = {}
        for log in logs:
            action_counts[log.action] = action_counts.get(log.action, 0) + 1

        # Count successes and failures
        success_count = logs.filter(success=True).count()
        failure_count = logs.filter(success=False).count()

        # Get unique actors
        unique_actors = logs.values("actor").distinct().count()

        return {
            "period_hours": hours,
            "total_events": logs.count(),
            "success_count": success_count,
            "failure_count": failure_count,
            "unique_actors": unique_actors,
            "action_counts": action_counts,
            "generated_at": timezone.now().isoformat(),
        }

    def cleanup_old_logs(self, retention_days: int = 90) -> int:
        """
        Clean up old audit logs.

        Args:
            retention_days: Days to retain logs

        Returns:
            Number of logs deleted
        """
        cutoff_date = timezone.now() - timedelta(days=retention_days)
        deleted_count, _ = AuditLog.objects.filter(timestamp__lt=cutoff_date).delete()

        self.log(
            action=AuditAction.TARGET_DELETED,  # Using as generic cleanup action
            actor="system",
            details={
                "cleanup_type": "audit_logs",
                "deleted_count": deleted_count,
                "retention_days": retention_days,
            },
        )

        return deleted_count
