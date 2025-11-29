"""
Celery tasks for async deployment operations.
"""

import logging
from typing import Optional, Dict

from celery import shared_task
from django.utils import timezone

from ..models import Deployment, DeploymentTarget
from ..services import DeploymentService
from ..exceptions import DeploymentError, RollbackError

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(ConnectionError,),
    acks_late=True,
)
def deploy_async(
    self,
    target_id: int,
    compose_file_path: str,
    project_name: Optional[str] = None,
    environment: Optional[Dict[str, str]] = None,
    env_file_path: Optional[str] = None,
    version: str = '',
    deployed_by: str = '',
    timeout: Optional[int] = None,
    pull_images: bool = True,
    build_images: bool = False,
    metadata: Optional[dict] = None,
    webhook_url: Optional[str] = None,
) -> dict:
    """
    Async deployment task.

    Args:
        target_id: ID of the DeploymentTarget
        compose_file_path: Path to local docker-compose.yml file
        project_name: Optional Docker Compose project name
        environment: Optional environment variables
        env_file_path: Optional path to .env file
        version: Version tag for this deployment
        deployed_by: User performing the deployment
        timeout: Deployment timeout in seconds
        pull_images: Pull images before starting
        build_images: Build images before starting
        metadata: Additional deployment metadata
        webhook_url: Optional webhook URL for notifications

    Returns:
        Dict with deployment result
    """
    logger.info(f"Starting async deployment to target {target_id}")

    try:
        target = DeploymentTarget.objects.get(id=target_id)
    except DeploymentTarget.DoesNotExist:
        logger.error(f"Target {target_id} not found")
        return {
            'success': False,
            'error': f"Target {target_id} not found",
            'task_id': self.request.id,
        }

    deployment_service = DeploymentService()

    try:
        deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_file_path,
            project_name=project_name,
            environment=environment,
            env_file_path=env_file_path,
            version=version,
            deployed_by=deployed_by,
            timeout=timeout,
            pull_images=pull_images,
            build_images=build_images,
            metadata={
                **(metadata or {}),
                'celery_task_id': self.request.id,
            },
        )

        result = {
            'success': True,
            'deployment_id': deployment.id,
            'status': deployment.status,
            'project_name': deployment.project_name,
            'version': deployment.version,
            'task_id': self.request.id,
        }

        # Send webhook notification if configured
        if webhook_url:
            send_webhook.delay(
                webhook_url=webhook_url,
                event='deployment.completed',
                payload=result,
            )

        return result

    except DeploymentError as e:
        logger.error(f"Deployment failed: {e}")
        result = {
            'success': False,
            'error': str(e),
            'task_id': self.request.id,
        }

        if webhook_url:
            send_webhook.delay(
                webhook_url=webhook_url,
                event='deployment.failed',
                payload=result,
            )

        return result

    except Exception as e:
        logger.exception(f"Unexpected error during deployment: {e}")
        raise self.retry(exc=e)


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def rollback_async(
    self,
    deployment_id: int,
    deployed_by: str = '',
    timeout: Optional[int] = None,
    webhook_url: Optional[str] = None,
) -> dict:
    """
    Async rollback task.

    Args:
        deployment_id: ID of the Deployment to rollback to
        deployed_by: User performing the rollback
        timeout: Rollback timeout in seconds
        webhook_url: Optional webhook URL for notifications

    Returns:
        Dict with rollback result
    """
    logger.info(f"Starting async rollback to deployment {deployment_id}")

    try:
        deployment = Deployment.objects.get(id=deployment_id)
    except Deployment.DoesNotExist:
        return {
            'success': False,
            'error': f"Deployment {deployment_id} not found",
            'task_id': self.request.id,
        }

    deployment_service = DeploymentService()

    try:
        rollback_deployment = deployment_service.rollback(
            deployment=deployment,
            deployed_by=deployed_by,
            timeout=timeout,
        )

        result = {
            'success': True,
            'rollback_deployment_id': rollback_deployment.id,
            'original_deployment_id': deployment_id,
            'status': rollback_deployment.status,
            'task_id': self.request.id,
        }

        if webhook_url:
            send_webhook.delay(
                webhook_url=webhook_url,
                event='rollback.completed',
                payload=result,
            )

        return result

    except RollbackError as e:
        logger.error(f"Rollback failed: {e}")
        result = {
            'success': False,
            'error': str(e),
            'task_id': self.request.id,
        }

        if webhook_url:
            send_webhook.delay(
                webhook_url=webhook_url,
                event='rollback.failed',
                payload=result,
            )

        return result

    except Exception as e:
        logger.exception(f"Unexpected error during rollback: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True)
def check_deployment_health(self, deployment_id: int) -> dict:
    """
    Check health status of a deployment.

    Args:
        deployment_id: ID of the Deployment

    Returns:
        Dict with health status
    """
    try:
        deployment = Deployment.objects.get(id=deployment_id)
    except Deployment.DoesNotExist:
        return {
            'success': False,
            'error': f"Deployment {deployment_id} not found",
        }

    deployment_service = DeploymentService()

    try:
        status = deployment_service.get_status(deployment)
        return {
            'success': True,
            'deployment_id': deployment_id,
            'status': status,
        }
    except Exception as e:
        return {
            'success': False,
            'deployment_id': deployment_id,
            'error': str(e),
        }


@shared_task
def cleanup_old_deployments(retention_days: int = 90) -> dict:
    """
    Clean up old deployment records.

    Args:
        retention_days: Days to retain deployments

    Returns:
        Dict with cleanup result
    """
    deployment_service = DeploymentService()

    try:
        count = deployment_service.cleanup_old_deployments(retention_days)
        return {
            'success': True,
            'deleted_count': count,
            'retention_days': retention_days,
        }
    except Exception as e:
        logger.exception(f"Cleanup failed: {e}")
        return {
            'success': False,
            'error': str(e),
        }


# Import at end to avoid circular imports
from .notification_tasks import send_webhook
