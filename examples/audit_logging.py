"""
Audit logging example.

This example demonstrates:
- Logging deployment actions
- Querying audit logs
- Getting activity summaries
- Cleaning up old logs
"""

from datetime import timedelta

from django.utils import timezone

from remote_compose.services import AuditService, AuditAction
from remote_compose.models import AuditLog


def log_deployment_actions():
    """Log common deployment actions."""
    audit = AuditService()

    # Log a deployment started event
    entry = audit.log(
        action=AuditAction.DEPLOYMENT_STARTED,
        actor='admin@example.com',
        resource_type='deployment',
        resource_id=123,
        resource_name='myapp',
        ip_address='192.168.1.100',
        details={
            'target_id': 1,
            'target_name': 'prod-server-1',
            'version': 'v2.0.0',
        },
    )

    print(f"Logged: {entry.action}")
    print(f"  Timestamp: {entry.timestamp}")
    print(f"  Actor: {entry.actor}")


def log_sensitive_data():
    """Sensitive data is automatically sanitized."""
    audit = AuditService()

    # Sensitive fields like 'password' are automatically masked
    entry = audit.log(
        action=AuditAction.TARGET_CREATED,
        actor='admin@example.com',
        resource_type='target',
        resource_id=5,
        resource_name='new-server',
        details={
            'host': 'server.example.com',
            'username': 'deploy',
            'password': 'super-secret-password',  # Will be masked as '***'
            'ssh_key': '-----BEGIN RSA PRIVATE KEY-----...',  # Will be redacted
        },
    )

    # The saved details will have sensitive data masked
    print("Logged with sanitized details")


def use_convenience_methods():
    """Use convenience methods for common actions."""
    from remote_compose.models import Deployment
    from unittest.mock import MagicMock

    audit = AuditService()

    # Create a mock deployment for demonstration
    deployment = MagicMock(spec=Deployment)
    deployment.id = 123
    deployment.project_name = 'myapp'
    deployment.target_id = 1
    deployment.target.name = 'prod-server'
    deployment.version = 'v2.0.0'
    deployment.deployment_type = 'deploy'
    deployment.duration = 45.2
    deployment.container_ids = ['abc123', 'def456']

    # Log deployment started
    audit.log_deployment_started(deployment, 'admin@example.com')

    # Log deployment completed
    audit.log_deployment_completed(deployment, 'admin@example.com')

    # Log deployment failed (if it fails)
    audit.log_deployment_failed(
        deployment,
        'admin@example.com',
        'Connection timeout after 60 seconds',
    )

    print("Logged deployment lifecycle events")


def log_security_events():
    """Log security-related events."""
    audit = AuditService()

    # Log rate limit exceeded
    audit.log_rate_limit_exceeded(
        actor='user@example.com',
        limit_type='per_target',
        ip_address='192.168.1.50',
        details={
            'target_id': 1,
            'limit': 5,
            'window': 60,
        },
    )

    # Log authentication failure
    audit.log(
        action=AuditAction.AUTHENTICATION_FAILED,
        actor='unknown',
        ip_address='10.0.0.5',
        success=False,
        error_message='Invalid API key',
        details={
            'attempted_key': 'abc***',  # Partially masked
            'user_agent': 'curl/7.64.1',
        },
    )

    print("Logged security events")


def query_audit_logs():
    """Query audit logs with filters."""
    audit = AuditService()

    # Query recent deployment events
    logs = audit.query_logs(
        action='deployment.started',
        limit=10,
    )

    print(f"Recent deployment starts ({len(logs)}):")
    for log in logs:
        print(f"  [{log.timestamp}] {log.actor} -> {log.resource_name}")

    # Query by actor
    user_logs = audit.query_logs(
        actor='admin@example.com',
        start_date=timezone.now() - timedelta(days=7),
        limit=50,
    )

    print(f"\nActions by admin@example.com (last 7 days): {len(user_logs)}")

    # Query failed actions
    failed = audit.query_logs(
        success=False,
        limit=20,
    )

    print(f"\nFailed actions: {len(failed)}")
    for log in failed:
        print(f"  [{log.timestamp}] {log.action}: {log.error_message}")


def get_activity_summary():
    """Get a summary of recent activity."""
    audit = AuditService()

    # Get summary for last 24 hours
    summary = audit.get_activity_summary(hours=24)

    print("Activity Summary (Last 24 Hours)")
    print("=" * 40)
    print(f"Total Events: {summary['total_events']}")
    print(f"Successful: {summary['success_count']}")
    print(f"Failed: {summary['failure_count']}")
    print(f"Unique Actors: {summary['unique_actors']}")

    print("\nActions by Type:")
    for action, count in sorted(summary['action_counts'].items()):
        print(f"  {action}: {count}")


def cleanup_old_logs():
    """Clean up old audit logs."""
    audit = AuditService()

    # Delete logs older than 90 days
    deleted_count = audit.cleanup_old_logs(retention_days=90)

    print(f"Cleaned up {deleted_count} old audit logs")


def export_audit_logs():
    """Export audit logs to JSON format."""
    import json

    # Query logs
    logs = AuditLog.objects.filter(
        timestamp__gte=timezone.now() - timedelta(days=7)
    ).order_by('timestamp')

    # Convert to JSON-serializable format
    export_data = []
    for log in logs:
        export_data.append({
            'timestamp': log.timestamp.isoformat(),
            'action': log.action,
            'actor': log.actor,
            'resource_type': log.resource_type,
            'resource_id': log.resource_id,
            'resource_name': log.resource_name,
            'success': log.success,
            'error_message': log.error_message,
            'details': log.details,
        })

    # Save to file
    with open('audit_export.json', 'w') as f:
        json.dump(export_data, f, indent=2)

    print(f"Exported {len(export_data)} audit logs to audit_export.json")


def setup_audit_file_logging():
    """Configure audit logging to file."""
    # In your Django settings, add:
    #
    # REMOTE_COMPOSE = {
    #     'AUDIT_LOG_ENABLED': True,
    #     'AUDIT_LOG_TO_DATABASE': True,
    #     'AUDIT_LOG_FILE': '/var/log/remote-compose/audit.log',
    # }

    print("Audit logging configuration:")
    print("  AUDIT_LOG_ENABLED: True")
    print("  AUDIT_LOG_TO_DATABASE: True")
    print("  AUDIT_LOG_FILE: /var/log/remote-compose/audit.log")


if __name__ == '__main__':
    print("=" * 50)
    print("Logging Actions")
    print("=" * 50)
    log_deployment_actions()

    print("\n" + "=" * 50)
    print("Activity Summary")
    print("=" * 50)
    get_activity_summary()

    print("\n" + "=" * 50)
    print("Query Audit Logs")
    print("=" * 50)
    query_audit_logs()
