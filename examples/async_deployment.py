"""
Async deployment example using Celery tasks.

This example demonstrates:
- Running deployments asynchronously with Celery
- Tracking task progress
- Handling webhook notifications
"""

from remote_compose.tasks import (
    deploy_async,
    rollback_async,
    check_deployment_health,
    cleanup_old_deployments,
)
from remote_compose.models import DeploymentTarget


def deploy_async_example():
    """Deploy asynchronously and get task ID."""
    # Get target by ID
    target = DeploymentTarget.objects.get(name='production-server')

    # Launch async deployment
    result = deploy_async.delay(
        target_id=target.id,
        compose_file_path='/path/to/docker-compose.yml',
        project_name='myapp',
        version='v1.0.0',
        deployed_by='admin@example.com',
        # Optional: notify via webhook when complete
        webhook_url='https://example.com/webhooks/deployment',
    )

    print(f"Deployment task started: {result.id}")
    print(f"Task status: {result.status}")

    return result


def check_task_status(task_result):
    """Check the status of an async task."""
    print(f"Task ID: {task_result.id}")
    print(f"Status: {task_result.status}")

    if task_result.ready():
        result = task_result.get()
        print(f"Result: {result}")

        if result.get('success'):
            print(f"Deployment ID: {result.get('deployment_id')}")
        else:
            print(f"Error: {result.get('error')}")
    else:
        print("Task still running...")


def wait_for_deployment(task_result, timeout=300):
    """Wait for deployment to complete."""
    try:
        result = task_result.get(timeout=timeout)
        return result
    except Exception as e:
        print(f"Task failed or timed out: {e}")
        return None


def deploy_with_webhook():
    """Deploy with webhook notification on completion."""
    target = DeploymentTarget.objects.get(name='production-server')

    result = deploy_async.delay(
        target_id=target.id,
        compose_file_path='/path/to/docker-compose.yml',
        project_name='myapp',
        version='v1.0.0',
        deployed_by='admin@example.com',
        # Webhook URL - will receive POST on completion
        webhook_url='https://your-server.com/api/webhooks/deployment',
    )

    return result


def rollback_async_example(deployment_id):
    """Rollback asynchronously."""
    result = rollback_async.delay(
        deployment_id=deployment_id,
        deployed_by='admin@example.com',
        webhook_url='https://example.com/webhooks/rollback',
    )

    print(f"Rollback task started: {result.id}")
    return result


def schedule_health_checks():
    """Schedule health checks using Celery beat."""
    # This would typically be in your Celery beat schedule
    # celeryconfig.py or settings.py:
    #
    # CELERY_BEAT_SCHEDULE = {
    #     'check-all-targets-every-5-minutes': {
    #         'task': 'remote_compose.tasks.check_all_targets_health',
    #         'schedule': 300.0,  # 5 minutes
    #     },
    #     'check-stale-deployments-hourly': {
    #         'task': 'remote_compose.tasks.monitor_stale_deployments',
    #         'schedule': 3600.0,  # 1 hour
    #         'args': (24,),  # max_running_hours
    #     },
    #     'cleanup-old-deployments-daily': {
    #         'task': 'remote_compose.tasks.cleanup_old_deployments',
    #         'schedule': 86400.0,  # 24 hours
    #         'args': (90,),  # retention_days
    #     },
    # }
    pass


def check_deployment_health_example(deployment_id):
    """Check health of a specific deployment."""
    result = check_deployment_health.delay(deployment_id)
    health_status = result.get(timeout=60)

    print(f"Health check result: {health_status}")
    return health_status


def cleanup_old_deployments_example():
    """Clean up old deployment records."""
    result = cleanup_old_deployments.delay(retention_days=90)
    cleanup_result = result.get(timeout=300)

    print(f"Cleanup result: {cleanup_result}")
    return cleanup_result


if __name__ == '__main__':
    # Example: Start async deployment
    task = deploy_async_example()

    # Check status periodically
    import time
    for _ in range(10):
        check_task_status(task)
        if task.ready():
            break
        time.sleep(5)
