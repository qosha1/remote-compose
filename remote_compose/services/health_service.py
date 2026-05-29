"""
Service for health monitoring of deployments and targets.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, List, Dict, Any

from django.utils import timezone
from django.db.models import QuerySet

from ..models import Deployment, DeploymentTarget, DeploymentLog
from .base import BaseService
from .target_service import TargetService
from .compose_service import ComposeService

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a health check."""

    healthy: bool
    target_name: str
    deployment_id: Optional[int] = None
    project_name: Optional[str] = None
    message: str = ""
    details: Optional[Dict[str, Any]] = None
    checked_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "target_name": self.target_name,
            "deployment_id": self.deployment_id,
            "project_name": self.project_name,
            "message": self.message,
            "details": self.details or {},
            "checked_at": self.checked_at or timezone.now().isoformat(),
        }


@dataclass
class HealthReport:
    """Aggregated health report."""

    total_checked: int
    healthy_count: int
    unhealthy_count: int
    results: List[HealthCheckResult]
    generated_at: str

    @property
    def overall_healthy(self) -> bool:
        return self.unhealthy_count == 0

    def to_dict(self) -> dict:
        return {
            "overall_healthy": self.overall_healthy,
            "total_checked": self.total_checked,
            "healthy_count": self.healthy_count,
            "unhealthy_count": self.unhealthy_count,
            "results": [r.to_dict() for r in self.results],
            "generated_at": self.generated_at,
        }


class HealthService(BaseService):
    """
    Service for monitoring health of deployment targets and running deployments.
    """

    def __init__(
        self,
        target_service: Optional[TargetService] = None,
        compose_service: Optional[ComposeService] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.target_service = target_service or TargetService()
        self.compose_service = compose_service or ComposeService()

    def check_target_health(self, target: DeploymentTarget) -> HealthCheckResult:
        """
        Check health of a deployment target.

        Args:
            target: DeploymentTarget instance

        Returns:
            HealthCheckResult
        """
        self.log_debug(f"Checking health of target: {target.name}")

        try:
            success, message = self.target_service.test_connection(target)

            if success:
                target.mark_healthy()
                self.log_info(f"Target {target.name} is healthy")
            else:
                target.mark_unhealthy(message)
                self.log_warning(f"Target {target.name} is unhealthy: {message}")

            return HealthCheckResult(
                healthy=success,
                target_name=target.name,
                message=message,
                details={
                    "host": target.host,
                    "port": target.port,
                    "health_status": target.health_status,
                    "last_health_check": (
                        target.last_health_check.isoformat()
                        if target.last_health_check
                        else None
                    ),
                },
            )

        except Exception as e:
            target.mark_unhealthy(str(e))
            self.log_error(f"Health check failed for target {target.name}: {e}")

            return HealthCheckResult(
                healthy=False,
                target_name=target.name,
                message=str(e),
            )

    def check_all_targets_health(
        self,
        only_active: bool = True,
    ) -> HealthReport:
        """
        Check health of all targets.

        Args:
            only_active: Only check active targets

        Returns:
            HealthReport with all results
        """
        targets = DeploymentTarget.objects.all()
        if only_active:
            targets = targets.filter(is_active=True)

        results = []
        for target in targets:
            result = self.check_target_health(target)
            results.append(result)

        healthy_count = sum(1 for r in results if r.healthy)

        return HealthReport(
            total_checked=len(results),
            healthy_count=healthy_count,
            unhealthy_count=len(results) - healthy_count,
            results=results,
            generated_at=timezone.now().isoformat(),
        )

    def check_deployment_health(self, deployment: Deployment) -> HealthCheckResult:
        """
        Check health of a specific deployment.

        Args:
            deployment: Deployment instance

        Returns:
            HealthCheckResult
        """
        if deployment.status != Deployment.Status.SUCCESS:
            return HealthCheckResult(
                healthy=False,
                target_name=deployment.target.name,
                deployment_id=deployment.id,
                project_name=deployment.project_name,
                message=f"Deployment is not in SUCCESS state: {deployment.status}",
            )

        try:
            ssh_client = self.target_service.get_ssh_client(deployment.target)
            with ssh_client:
                # Get service status
                service_status = self.compose_service.get_service_status(
                    ssh_client=ssh_client,
                    compose_path=f"/tmp/remote-compose/{deployment.project_name}/docker-compose.yml",
                    project_name=deployment.project_name,
                )

                # Check if all services are running
                all_running = True
                service_details = {}

                for name, info in service_status.items():
                    state = info.get("state", "").lower()
                    is_running = state in ("running", "up")
                    service_details[name] = {
                        "state": state,
                        "healthy": is_running,
                    }
                    if not is_running:
                        all_running = False

                message = (
                    "All services running"
                    if all_running
                    else "Some services not running"
                )

                return HealthCheckResult(
                    healthy=all_running,
                    target_name=deployment.target.name,
                    deployment_id=deployment.id,
                    project_name=deployment.project_name,
                    message=message,
                    details={
                        "services": service_details,
                        "container_ids": deployment.container_ids,
                    },
                )

        except Exception as e:
            self.log_error(f"Health check failed for deployment {deployment.id}: {e}")
            return HealthCheckResult(
                healthy=False,
                target_name=deployment.target.name,
                deployment_id=deployment.id,
                project_name=deployment.project_name,
                message=str(e),
            )

    def check_all_deployments_health(
        self,
        target: Optional[DeploymentTarget] = None,
        project_name: Optional[str] = None,
    ) -> HealthReport:
        """
        Check health of all successful deployments.

        Args:
            target: Optional filter by target
            project_name: Optional filter by project name

        Returns:
            HealthReport with all results
        """
        deployments = Deployment.objects.filter(status=Deployment.Status.SUCCESS)

        if target:
            deployments = deployments.filter(target=target)
        if project_name:
            deployments = deployments.filter(project_name=project_name)

        # Get only the latest deployment per project/target combination
        latest_deployments = {}
        for deployment in deployments.order_by("-completed_at"):
            key = (deployment.target_id, deployment.project_name)
            if key not in latest_deployments:
                latest_deployments[key] = deployment

        results = []
        for deployment in latest_deployments.values():
            result = self.check_deployment_health(deployment)
            results.append(result)

        healthy_count = sum(1 for r in results if r.healthy)

        return HealthReport(
            total_checked=len(results),
            healthy_count=healthy_count,
            unhealthy_count=len(results) - healthy_count,
            results=results,
            generated_at=timezone.now().isoformat(),
        )

    def get_unhealthy_targets(self) -> QuerySet:
        """Get all unhealthy targets."""
        return DeploymentTarget.objects.filter(
            health_status=DeploymentTarget.HealthStatus.UNHEALTHY,
            is_active=True,
        )

    def get_stale_deployments(self, max_running_hours: int = 24) -> QuerySet:
        """
        Get deployments that have been in RUNNING state too long.

        Args:
            max_running_hours: Maximum hours a deployment should be running

        Returns:
            QuerySet of stale deployments
        """
        cutoff_time = timezone.now() - timedelta(hours=max_running_hours)
        return Deployment.objects.filter(
            status=Deployment.Status.RUNNING,
            started_at__lt=cutoff_time,
        ).select_related("target")

    def get_health_history(
        self,
        target: DeploymentTarget,
        hours: int = 24,
    ) -> List[dict]:
        """
        Get health check history for a target.

        Args:
            target: DeploymentTarget instance
            hours: Hours of history to retrieve

        Returns:
            List of health check records
        """
        cutoff_time = timezone.now() - timedelta(hours=hours)

        # Get health-related logs
        logs = DeploymentLog.objects.filter(
            deployment__target=target,
            created_at__gte=cutoff_time,
            message__icontains="health",
        ).order_by("-created_at")[:100]

        history = []
        for log in logs:
            history.append(
                {
                    "timestamp": log.created_at.isoformat(),
                    "deployment_id": log.deployment_id,
                    "message": log.message,
                    "level": log.level,
                }
            )

        return history

    def run_custom_health_check(
        self,
        deployment: Deployment,
        command: str,
        expected_output: Optional[str] = None,
        expected_exit_code: int = 0,
    ) -> HealthCheckResult:
        """
        Run a custom health check command on a deployment.

        Args:
            deployment: Deployment instance
            command: Command to execute (will be validated)
            expected_output: Optional expected output pattern (regex)
            expected_exit_code: Expected exit code (default: 0)

        Returns:
            HealthCheckResult
        """
        # Validate command for safety
        if not self._validate_health_command(command):
            return HealthCheckResult(
                healthy=False,
                target_name=deployment.target.name,
                deployment_id=deployment.id,
                project_name=deployment.project_name,
                message="Invalid or unsafe health check command",
            )

        try:
            ssh_client = self.target_service.get_ssh_client(deployment.target)
            with ssh_client:
                result = ssh_client.execute(command, timeout=30)

                healthy = result.exit_code == expected_exit_code

                if healthy and expected_output:
                    if not re.search(expected_output, result.stdout):
                        healthy = False

                return HealthCheckResult(
                    healthy=healthy,
                    target_name=deployment.target.name,
                    deployment_id=deployment.id,
                    project_name=deployment.project_name,
                    message=(
                        "Custom health check passed"
                        if healthy
                        else "Custom health check failed"
                    ),
                    details={
                        "command": command,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout[:500],  # Truncate
                        "expected_exit_code": expected_exit_code,
                    },
                )

        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                target_name=deployment.target.name,
                deployment_id=deployment.id,
                project_name=deployment.project_name,
                message=f"Health check command failed: {e}",
            )

    def _validate_health_command(self, command: str) -> bool:
        """
        Validate health check command for safety.

        Only allows certain read-only commands.
        """
        # Allowed command prefixes for health checks
        allowed_prefixes = (
            "docker ps",
            "docker inspect",
            "docker compose ps",
            "docker compose top",
            "curl ",
            "wget ",
            "nc ",
            "cat /proc/",
            "echo ",
            "test ",
            "true",
            "false",
        )

        # Dangerous patterns to block
        dangerous_patterns = (
            ";",
            "&&",
            "||",
            "|",
            "`",
            "$(",
            "rm ",
            "mv ",
            "cp ",
            "chmod ",
            "chown ",
            "kill ",
            "pkill ",
            "shutdown ",
            "reboot ",
            ">",
            ">>",
            "<",
        )

        command_lower = command.lower().strip()

        # Check for dangerous patterns
        for pattern in dangerous_patterns:
            if pattern in command:
                self.log_warning(f"Blocked dangerous health command: {command}")
                return False

        # Check if command starts with allowed prefix
        if not any(command_lower.startswith(prefix) for prefix in allowed_prefixes):
            self.log_warning(f"Health command not in allowed list: {command}")
            return False

        return True
