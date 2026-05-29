"""
Celery tasks for health monitoring.
"""

import logging
from typing import Optional

from celery import shared_task
from django.utils import timezone

from ..models import DeploymentTarget, Deployment
from ..services import TargetService, DeploymentService

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def check_target_health(self, target_id: int) -> dict:
    """
    Check health status of a single deployment target.

    Args:
        target_id: ID of the DeploymentTarget

    Returns:
        Dict with health status
    """
    try:
        target = DeploymentTarget.objects.get(id=target_id)
    except DeploymentTarget.DoesNotExist:
        return {
            "success": False,
            "target_id": target_id,
            "error": f"Target {target_id} not found",
        }

    target_service = TargetService()

    try:
        success, message = target_service.test_connection(target)

        if success:
            target.mark_healthy()
        else:
            target.mark_unhealthy(message)

        return {
            "success": True,
            "target_id": target_id,
            "target_name": target.name,
            "healthy": success,
            "message": message,
            "checked_at": timezone.now().isoformat(),
        }

    except Exception as e:
        logger.exception(f"Health check failed for target {target_id}: {e}")
        target.mark_unhealthy(str(e))
        return {
            "success": False,
            "target_id": target_id,
            "error": str(e),
        }


@shared_task
def check_all_targets_health() -> dict:
    """
    Check health status of all active deployment targets.

    Returns:
        Dict with overall health results
    """
    targets = DeploymentTarget.objects.filter(is_active=True)
    results = []
    healthy_count = 0
    unhealthy_count = 0

    for target in targets:
        result = check_target_health(target.id)
        results.append(result)

        if result.get("healthy"):
            healthy_count += 1
        else:
            unhealthy_count += 1

    return {
        "success": True,
        "total_targets": len(results),
        "healthy_count": healthy_count,
        "unhealthy_count": unhealthy_count,
        "results": results,
        "checked_at": timezone.now().isoformat(),
    }


@shared_task(bind=True)
def run_health_checks(
    self,
    deployment_id: Optional[int] = None,
    target_id: Optional[int] = None,
    project_name: Optional[str] = None,
) -> dict:
    """
    Run comprehensive health checks on deployments.

    Can check a specific deployment, all deployments on a target,
    or all deployments with a specific project name.

    Args:
        deployment_id: Optional specific deployment ID
        target_id: Optional target ID to check all deployments
        project_name: Optional project name to check

    Returns:
        Dict with health check results
    """
    deployment_service = DeploymentService()
    results = []

    # Build query
    deployments = Deployment.objects.filter(status=Deployment.Status.SUCCESS)

    if deployment_id:
        deployments = deployments.filter(id=deployment_id)
    if target_id:
        deployments = deployments.filter(target_id=target_id)
    if project_name:
        deployments = deployments.filter(project_name=project_name)

    for deployment in deployments:
        try:
            status = deployment_service.get_status(deployment)

            # Check if services are running
            live_status = status.get("live_service_status", {})
            all_running = (
                all(
                    svc.get("state", "").lower() in ("running", "up")
                    for svc in live_status.values()
                )
                if live_status
                else False
            )

            results.append(
                {
                    "deployment_id": deployment.id,
                    "project_name": deployment.project_name,
                    "target": deployment.target.name,
                    "healthy": all_running,
                    "service_status": live_status,
                    "error": status.get("live_status_error"),
                }
            )

        except Exception as e:
            results.append(
                {
                    "deployment_id": deployment.id,
                    "project_name": deployment.project_name,
                    "target": deployment.target.name,
                    "healthy": False,
                    "error": str(e),
                }
            )

    healthy_count = sum(1 for r in results if r.get("healthy"))

    return {
        "success": True,
        "total_checked": len(results),
        "healthy_count": healthy_count,
        "unhealthy_count": len(results) - healthy_count,
        "results": results,
        "checked_at": timezone.now().isoformat(),
    }


@shared_task
def monitor_stale_deployments(max_running_hours: int = 24) -> dict:
    """
    Monitor for deployments that have been running longer than expected.

    Args:
        max_running_hours: Maximum hours a deployment should be in RUNNING state

    Returns:
        Dict with stale deployment information
    """
    cutoff_time = timezone.now() - timezone.timedelta(hours=max_running_hours)

    stale_deployments = Deployment.objects.filter(
        status=Deployment.Status.RUNNING,
        started_at__lt=cutoff_time,
    ).select_related("target")

    stale_list = []
    for deployment in stale_deployments:
        stale_list.append(
            {
                "deployment_id": deployment.id,
                "project_name": deployment.project_name,
                "target": deployment.target.name,
                "started_at": deployment.started_at.isoformat(),
                "running_hours": (
                    timezone.now() - deployment.started_at
                ).total_seconds()
                / 3600,
            }
        )

        # Log warning
        logger.warning(
            f"Stale deployment detected: {deployment.id} on {deployment.target.name} "
            f"has been running since {deployment.started_at}"
        )

    return {
        "success": True,
        "stale_count": len(stale_list),
        "stale_deployments": stale_list,
        "checked_at": timezone.now().isoformat(),
    }
