"""
Celery tasks for remote_compose.
"""

from .deployment_tasks import (
    deploy_async,
    rollback_async,
    check_deployment_health,
    cleanup_old_deployments,
)
from .health_tasks import (
    check_target_health,
    check_all_targets_health,
    run_health_checks,
)
from .notification_tasks import (
    send_deployment_notification,
    send_webhook,
)

__all__ = [
    # Deployment tasks
    'deploy_async',
    'rollback_async',
    'check_deployment_health',
    'cleanup_old_deployments',
    # Health tasks
    'check_target_health',
    'check_all_targets_health',
    'run_health_checks',
    # Notification tasks
    'send_deployment_notification',
    'send_webhook',
]
