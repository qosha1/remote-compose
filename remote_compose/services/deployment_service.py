"""
Service for orchestrating deployments.
"""

import os
import uuid
from typing import Optional, Dict, List

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from ..models import (
    Deployment,
    DeploymentLog,
    DockerContext,
    DeploymentTarget,
)
from ..conf import get_setting
from ..exceptions import (
    DeploymentError,
    DeploymentTimeoutError,
    DeploymentInProgressError,
    RollbackError,
    ValidationError,
)
from .base import BaseService
from .target_service import TargetService
from .context_service import ContextService
from .compose_service import ComposeService
from .credential_service import CredentialService


class DeploymentService(BaseService):
    """
    Service for orchestrating Docker Compose deployments.
    """

    def __init__(
        self,
        target_service: Optional[TargetService] = None,
        context_service: Optional[ContextService] = None,
        compose_service: Optional[ComposeService] = None,
        credential_service: Optional[CredentialService] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.target_service = target_service or TargetService(
            credential_service=self.credential_service
        )
        self.context_service = context_service or ContextService(
            target_service=self.target_service
        )
        self.compose_service = compose_service or ComposeService(
            credential_service=self.credential_service
        )

    def deploy(
        self,
        target: DeploymentTarget,
        compose_file_path: str,
        project_name: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        env_file_path: Optional[str] = None,
        version: str = '',
        deployed_by: str = '',
        context: Optional[DockerContext] = None,
        timeout: Optional[int] = None,
        pull_images: bool = True,
        build_images: bool = False,
        metadata: Optional[dict] = None,
    ) -> Deployment:
        """
        Deploy a Docker Compose application to a remote target.

        Args:
            target: DeploymentTarget instance
            compose_file_path: Path to local docker-compose.yml file
            project_name: Optional Docker Compose project name
            environment: Optional environment variables
            env_file_path: Optional path to .env file
            version: Version tag for this deployment
            deployed_by: User performing the deployment
            context: Optional specific DockerContext to use
            timeout: Deployment timeout in seconds
            pull_images: Pull images before starting
            build_images: Build images before starting
            metadata: Additional deployment metadata

        Returns:
            Deployment instance
        """
        timeout = timeout or get_setting('DEPLOYMENT_TIMEOUT', 600)

        # Validate compose file
        validation = self.compose_service.validate_compose_file(compose_file_path)
        compose_content = validation['content']

        # Get or create context
        if not context:
            context = self.context_service.get_or_create_context(target)

        # Generate project name if not provided
        if not project_name:
            project_name = os.path.basename(os.path.dirname(compose_file_path))
            if not project_name or project_name == '.':
                project_name = f"deploy-{uuid.uuid4().hex[:8]}"

        # Check for in-progress deployments to same target/project
        self._check_deployment_lock(target, project_name)

        # Create deployment record
        deployment = Deployment.objects.create(
            context=context,
            target=target,
            compose_file_path=compose_file_path,
            compose_content=compose_content,
            project_name=project_name,
            environment=environment or {},
            status=Deployment.Status.PENDING,
            deployment_type=Deployment.DeploymentType.DEPLOY,
            version=version,
            deployed_by=deployed_by,
            metadata=metadata or {},
        )

        self._log(deployment, 'info', f"Deployment {deployment.id} created")
        self.notify_observers('deployment_created', deployment=deployment)

        # Execute deployment
        try:
            self._execute_deployment(
                deployment=deployment,
                env_file_path=env_file_path,
                timeout=timeout,
                pull_images=pull_images,
                build_images=build_images,
            )
        except Exception as e:
            self.log_error(f"Deployment {deployment.id} failed: {e}", exc_info=True)
            deployment.fail(str(e))
            self._log(deployment, 'error', f"Deployment failed: {e}")
            self.notify_observers('deployment_failed', deployment=deployment, error=e)
            raise

        return deployment

    def rollback(
        self,
        deployment: Deployment,
        deployed_by: str = '',
        timeout: Optional[int] = None,
    ) -> Deployment:
        """
        Rollback to a previous deployment.

        Args:
            deployment: Deployment to rollback to
            deployed_by: User performing the rollback
            timeout: Rollback timeout in seconds

        Returns:
            New Deployment instance for the rollback
        """
        if deployment.status != Deployment.Status.SUCCESS:
            raise RollbackError(
                f"Cannot rollback to deployment {deployment.id}: status is {deployment.status}"
            )

        timeout = timeout or get_setting('DEPLOYMENT_TIMEOUT', 600)

        # Create rollback deployment
        rollback_deployment = Deployment.objects.create(
            context=deployment.context,
            target=deployment.target,
            compose_file_path=deployment.compose_file_path,
            compose_content=deployment.compose_content,
            project_name=deployment.project_name,
            environment=deployment.environment,
            status=Deployment.Status.PENDING,
            deployment_type=Deployment.DeploymentType.ROLLBACK,
            version=f"rollback-{deployment.version}",
            deployed_by=deployed_by,
            parent_deployment=deployment,
            metadata={'rollback_from': deployment.id},
        )

        self._log(rollback_deployment, 'info', f"Rolling back to deployment {deployment.id}")
        self.notify_observers('rollback_started', deployment=rollback_deployment)

        try:
            self._execute_deployment(
                deployment=rollback_deployment,
                timeout=timeout,
                pull_images=False,  # Use existing images
                build_images=False,
            )

            # Mark original deployment as rolled back
            deployment.mark_rolled_back()

            self.notify_observers('rollback_completed', deployment=rollback_deployment)

        except Exception as e:
            rollback_deployment.fail(str(e))
            self._log(rollback_deployment, 'error', f"Rollback failed: {e}")
            self.notify_observers('rollback_failed', deployment=rollback_deployment, error=e)
            raise RollbackError(f"Rollback failed: {e}")

        return rollback_deployment

    def get_deployment(self, deployment_id: int) -> Deployment:
        """Get a deployment by ID."""
        try:
            return Deployment.objects.select_related('context', 'target').get(id=deployment_id)
        except Deployment.DoesNotExist:
            raise ValidationError(f"Deployment not found: {deployment_id}")

    def list_deployments(
        self,
        target: Optional[DeploymentTarget] = None,
        context: Optional[DockerContext] = None,
        status: Optional[str] = None,
        project_name: Optional[str] = None,
        limit: int = 50,
    ) -> QuerySet:
        """
        List deployments with optional filters.

        Args:
            target: Filter by target
            context: Filter by context
            status: Filter by status
            project_name: Filter by project name
            limit: Maximum results

        Returns:
            QuerySet of Deployment instances
        """
        qs = Deployment.objects.select_related('context', 'target').all()

        if target:
            qs = qs.filter(target=target)
        if context:
            qs = qs.filter(context=context)
        if status:
            qs = qs.filter(status=status)
        if project_name:
            qs = qs.filter(project_name=project_name)

        return qs[:limit]

    def get_status(self, deployment: Deployment) -> dict:
        """
        Get current status of a deployment.

        Args:
            deployment: Deployment instance

        Returns:
            Dict with status information
        """
        result = {
            'id': deployment.id,
            'status': deployment.status,
            'project_name': deployment.project_name,
            'version': deployment.version,
            'started_at': deployment.started_at,
            'completed_at': deployment.completed_at,
            'duration': deployment.duration,
            'error_message': deployment.error_message,
            'container_ids': deployment.container_ids,
            'service_status': deployment.service_status,
        }

        # Get live status if deployment is successful
        if deployment.status == Deployment.Status.SUCCESS:
            try:
                ssh_client = self.target_service.get_ssh_client(deployment.target)
                with ssh_client:
                    service_status = self.compose_service.get_service_status(
                        ssh_client=ssh_client,
                        compose_path=f'/tmp/remote-compose/{deployment.project_name}/docker-compose.yml',
                        project_name=deployment.project_name,
                    )
                    result['live_service_status'] = service_status
            except Exception as e:
                result['live_status_error'] = str(e)

        return result

    def get_logs(
        self,
        deployment: Deployment,
        service: Optional[str] = None,
        tail: int = 100,
    ) -> str:
        """
        Get logs from deployed services.

        Args:
            deployment: Deployment instance
            service: Optional specific service name
            tail: Number of lines to tail

        Returns:
            Log output string
        """
        if deployment.status not in [Deployment.Status.SUCCESS, Deployment.Status.RUNNING]:
            raise DeploymentError(
                f"Cannot get logs: deployment status is {deployment.status}"
            )

        ssh_client = self.target_service.get_ssh_client(deployment.target)
        with ssh_client:
            result = self.compose_service.logs(
                ssh_client=ssh_client,
                compose_path=f'/tmp/remote-compose/{deployment.project_name}/docker-compose.yml',
                project_name=deployment.project_name,
                service=service,
                tail=tail,
            )

            return result.stdout if result.success else result.stderr

    def stop(self, deployment: Deployment) -> bool:
        """
        Stop a running deployment.

        Args:
            deployment: Deployment instance

        Returns:
            True if stopped successfully
        """
        ssh_client = self.target_service.get_ssh_client(deployment.target)
        with ssh_client:
            result = self.compose_service.down(
                ssh_client=ssh_client,
                compose_path=f'/tmp/remote-compose/{deployment.project_name}/docker-compose.yml',
                project_name=deployment.project_name,
            )

            if result.success:
                self._log(deployment, 'info', 'Deployment stopped')
                self.notify_observers('deployment_stopped', deployment=deployment)
                return True
            else:
                raise DeploymentError(f"Failed to stop deployment: {result.stderr}")

    def cancel(self, deployment: Deployment) -> Deployment:
        """
        Cancel a pending or running deployment.

        Args:
            deployment: Deployment instance

        Returns:
            Updated Deployment instance
        """
        if deployment.is_terminal:
            raise DeploymentError(
                f"Cannot cancel: deployment is already {deployment.status}"
            )

        deployment.cancel()
        self._log(deployment, 'warning', 'Deployment cancelled')
        self.notify_observers('deployment_cancelled', deployment=deployment)

        return deployment

    def cleanup_old_deployments(self, retention_days: int = 90) -> int:
        """
        Clean up old deployment records.

        Args:
            retention_days: Days to retain deployments

        Returns:
            Number of deployments deleted
        """
        cutoff_date = timezone.now() - timezone.timedelta(days=retention_days)

        old_deployments = Deployment.objects.filter(
            created_at__lt=cutoff_date,
            status__in=[
                Deployment.Status.SUCCESS,
                Deployment.Status.FAILED,
                Deployment.Status.ROLLED_BACK,
                Deployment.Status.CANCELLED,
            ]
        )

        count = old_deployments.count()
        old_deployments.delete()

        self.log_info(f"Cleaned up {count} old deployments")

        return count

    def _execute_deployment(
        self,
        deployment: Deployment,
        env_file_path: Optional[str] = None,
        timeout: int = 600,
        pull_images: bool = True,
        build_images: bool = False,
    ):
        """Execute the actual deployment."""
        deployment.start()
        self._log(deployment, 'info', 'Deployment started')
        self.notify_observers('deployment_started', deployment=deployment)

        ssh_client = self.target_service.get_ssh_client(deployment.target)

        with ssh_client:
            # Create remote directory for this project
            remote_dir = f'/tmp/remote-compose/{deployment.project_name}'

            # Upload compose file
            self._log(deployment, 'info', 'Uploading compose files')

            # Write compose content to temp file for upload
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
                f.write(deployment.compose_content)
                temp_compose_path = f.name

            try:
                upload_result = self.compose_service.upload_compose_files(
                    ssh_client=ssh_client,
                    compose_path=temp_compose_path,
                    env_file_path=env_file_path,
                    remote_dir=remote_dir,
                )
            finally:
                os.unlink(temp_compose_path)

            remote_compose_path = upload_result['compose_path']

            # Pull images if requested
            if pull_images:
                self._log(deployment, 'info', 'Pulling images')
                pull_result = self.compose_service.pull(
                    ssh_client=ssh_client,
                    compose_path=remote_compose_path,
                    project_name=deployment.project_name,
                    timeout=timeout,
                )
                if not pull_result.success:
                    self._log(deployment, 'warning', f'Image pull warning: {pull_result.stderr}')

            # Run compose up
            self._log(deployment, 'info', 'Starting containers')
            up_result = self.compose_service.up(
                ssh_client=ssh_client,
                compose_path=remote_compose_path,
                project_name=deployment.project_name,
                env_vars=deployment.environment,
                detached=True,
                build=build_images,
                timeout=timeout,
            )

            if not up_result.success:
                raise DeploymentError(
                    f"Docker compose up failed: {up_result.stderr}"
                )

            self._log(
                deployment,
                'info',
                'Containers started',
                command=up_result.command,
                output=up_result.stdout,
            )

            # Get container IDs and status
            container_ids = self.compose_service.get_container_ids(
                ssh_client=ssh_client,
                compose_path=remote_compose_path,
                project_name=deployment.project_name,
            )

            service_status = self.compose_service.get_service_status(
                ssh_client=ssh_client,
                compose_path=remote_compose_path,
                project_name=deployment.project_name,
            )

            # Mark deployment successful
            deployment.succeed(
                container_ids=container_ids,
                service_status=service_status,
            )

            self._log(deployment, 'info', 'Deployment completed successfully')
            self.notify_observers('deployment_completed', deployment=deployment)

    def _check_deployment_lock(self, target: DeploymentTarget, project_name: str):
        """Check for in-progress deployments to the same target/project."""
        in_progress = Deployment.objects.filter(
            target=target,
            project_name=project_name,
            status__in=[Deployment.Status.PENDING, Deployment.Status.RUNNING],
        ).exists()

        if in_progress:
            raise DeploymentInProgressError(
                f"Another deployment is in progress for {project_name} on {target.name}"
            )

    def _log(
        self,
        deployment: Deployment,
        level: str,
        message: str,
        command: str = '',
        output: str = '',
        service_name: str = '',
    ):
        """Create a deployment log entry."""
        DeploymentLog.log(
            deployment=deployment,
            message=message,
            level=level,
            command=command,
            output=output,
            service_name=service_name,
        )
