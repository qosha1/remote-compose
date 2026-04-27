"""
ECS Deployment Service.

Orchestrates the full deployment process for ECS:
1. Convert docker-compose to task definition
2. Register task definition in AWS
3. Create or update ECS service
4. Wait for deployment stability
5. Track deployment in database
"""

from typing import Optional, Dict, Any, List
from pathlib import Path

from django.utils import timezone

from ..models import (
    ECSCluster,
    ECSTaskDefinition,
    ECSService as ECSServiceModel,
    Deployment,
    DeploymentTarget,
    DeploymentLog,
)
from ..exceptions import (
    ECSDeploymentError,
    ValidationError,
)
from .base import BaseService
from .ecs_service import ECSService
from .compose_converter import ComposeToECSConverter
from .audit_service import AuditService, AuditAction
from .compose_preprocessor import ComposePreprocessor
from .ecr_service import ECRService
from .image_build_service import ImageBuildService
from .efs_service import EFSService
from .deployment_pipeline import (
    PipelineContext,
    PipelineBuilder,
    PipelineResult,
)


class ECSDeploymentService(BaseService):
    """
    High-level service for deploying docker-compose applications to ECS.

    Handles the complete deployment workflow from compose file to
    running ECS service.
    """

    def __init__(
        self,
        ecs_service: Optional[ECSService] = None,
        compose_converter: Optional[ComposeToECSConverter] = None,
        audit_service: Optional[AuditService] = None,
        preprocessor: Optional[ComposePreprocessor] = None,
        ecr_service: Optional[ECRService] = None,
        image_build_service: Optional[ImageBuildService] = None,
        efs_service: Optional[EFSService] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.ecs_service = ecs_service or ECSService()
        self.compose_converter = compose_converter or ComposeToECSConverter()
        self.audit_service = audit_service or AuditService()
        self._preprocessor = preprocessor
        self._ecr_service = ecr_service
        self._image_build_service = image_build_service
        self._efs_service = efs_service

    @property
    def preprocessor(self) -> ComposePreprocessor:
        """Get the preprocessor, creating it lazily if needed."""
        if self._preprocessor is None:
            self._preprocessor = ComposePreprocessor()
        return self._preprocessor

    @property
    def ecr_service(self) -> ECRService:
        """Get the ECR service, creating it lazily if needed."""
        if self._ecr_service is None:
            self._ecr_service = ECRService()
        return self._ecr_service

    @property
    def image_build_service(self) -> ImageBuildService:
        """Get the image build service, creating it lazily if needed."""
        if self._image_build_service is None:
            self._image_build_service = ImageBuildService(ecr_service=self.ecr_service)
        return self._image_build_service

    @property
    def efs_service(self) -> EFSService:
        """Get the EFS service, creating it lazily if needed."""
        if self._efs_service is None:
            self._efs_service = EFSService()
        return self._efs_service

    def deploy(
        self,
        cluster: ECSCluster,
        compose_file_path: str,
        project_name: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        version: str = '',
        deployed_by: str = 'system',
        desired_count: int = 1,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
        wait_for_stable: bool = True,
        timeout: int = 300,
        target: Optional[DeploymentTarget] = None,
    ) -> Deployment:
        """
        Deploy a docker-compose application to ECS.

        Args:
            cluster: Target ECS cluster
            compose_file_path: Path to docker-compose.yml
            project_name: Project name (defaults to directory name)
            environment: Additional environment variables
            version: Version tag for this deployment
            deployed_by: Who is deploying
            desired_count: Number of tasks to run
            cpu: Override CPU (Fargate units)
            memory: Override memory (MB)
            wait_for_stable: Wait for service to be stable
            timeout: Timeout for waiting
            target: Optional DeploymentTarget for tracking

        Returns:
            Deployment model instance
        """
        start_time = timezone.now()
        compose_path = Path(compose_file_path)

        if not compose_path.exists():
            raise ValidationError(f"Compose file not found: {compose_path}")

        if not project_name:
            project_name = compose_path.parent.name

        self._ensure_cluster_networking(cluster)
        self._ensure_cluster_execution_role(cluster)

        deployment = self._create_deployment_record(
            compose_path=compose_path,
            project_name=project_name,
            version=version,
            deployed_by=deployed_by,
            target=target,
            cluster=cluster,
        )

        try:
            self._log_step(deployment, "Starting ECS deployment")
            self._log_step(deployment, f"Cluster: {cluster.name} ({cluster.aws_region})")

            # Step 1: Convert compose to task definition
            self._log_step(deployment, "Converting docker-compose to ECS task definition")
            task_definition = self._convert_compose(
                compose_path=compose_path,
                cluster=cluster,
                project_name=project_name,
                environment=environment,
                cpu=cpu,
                memory=memory,
            )

            for warning in self.compose_converter.warnings:
                self._log_step(deployment, f"Warning: {warning}", level='warning')

            self._log_step(
                deployment,
                f"Task definition: {task_definition.name} "
                f"(CPU: {task_definition.cpu}, Memory: {task_definition.memory}MB)"
            )

            # Step 2: Register task definition in AWS
            self._log_step(deployment, "Registering task definition in AWS")
            task_definition = self.ecs_service.register_task_definition(task_definition)
            self._log_step(deployment, f"Registered: {task_definition.aws_task_definition_arn}")

            # Step 3: Create or update service
            ecs_service = self._get_or_create_service(
                cluster=cluster,
                task_definition=task_definition,
                project_name=project_name,
                desired_count=desired_count,
            )

            if ecs_service.aws_service_arn:
                self._log_step(deployment, f"Updating ECS service: {ecs_service.name}")
                ecs_service = self.ecs_service.update_service(
                    ecs_service=ecs_service,
                    task_definition=task_definition,
                    desired_count=desired_count,
                    force_new_deployment=True,
                )
            else:
                self._log_step(deployment, f"Creating ECS service: {ecs_service.name}")
                ecs_service = self.ecs_service.create_service(ecs_service)

            self._log_step(deployment, f"Service ARN: {ecs_service.aws_service_arn}")

            # Step 4: Wait for stability
            if wait_for_stable:
                self._log_step(deployment, "Waiting for service to stabilize...")
                ecs_service = self.ecs_service.wait_for_service_stable(
                    ecs_service=ecs_service,
                    timeout=timeout,
                )
                self._log_step(
                    deployment,
                    f"Service stable: {ecs_service.running_count}/{ecs_service.desired_count} tasks running"
                )

            # Step 5: Finalize deployment
            end_time = timezone.now()
            duration = (end_time - start_time).total_seconds()

            deployment.status = Deployment.Status.SUCCESS
            deployment.completed_at = end_time
            deployment.metadata.update({
                'task_definition_arn': task_definition.aws_task_definition_arn,
                'service_arn': ecs_service.aws_service_arn,
                'running_count': ecs_service.running_count,
                'cluster_arn': cluster.aws_cluster_arn,
            })

            # Only save if we have a valid deployment record
            if deployment.id:
                deployment.save()
                ecs_service.deployments.add(deployment)

            ecs_service.last_deployment_at = end_time
            ecs_service.save()

            # Set duration for return value
            deployment._duration = duration

            self._log_step(deployment, f"Deployment completed in {duration:.1f}s")

            self.audit_service.log(
                action=AuditAction.DEPLOYMENT_COMPLETED,
                actor=deployed_by,
                resource_type='ecs_deployment',
                resource_id=deployment.id if deployment.id else None,
                resource_name=project_name,
                details={
                    'cluster': cluster.name,
                    'project': project_name,
                    'version': version,
                    'duration': duration,
                },
            )

            self.notify_observers('ecs_deployment_success', deployment=deployment)
            return deployment

        except Exception as e:
            end_time = timezone.now()
            duration = (end_time - start_time).total_seconds()

            deployment.status = Deployment.Status.FAILED
            deployment.completed_at = end_time
            deployment.error_message = str(e)
            deployment._duration = duration

            if deployment.id:
                deployment.save()

            self._log_step(deployment, f"Deployment failed: {e}", level='error')

            self.audit_service.log(
                action=AuditAction.DEPLOYMENT_FAILED,
                actor=deployed_by,
                resource_type='ecs_deployment',
                resource_id=deployment.id if deployment.id else None,
                resource_name=project_name,
                details={
                    'cluster': cluster.name,
                    'project': project_name,
                    'error': str(e),
                },
                success=False,
                error_message=str(e),
            )

            self.notify_observers('ecs_deployment_failed', deployment=deployment, error=e)
            raise ECSDeploymentError(f"Deployment failed: {e}")

    def deploy_with_pipeline(
        self,
        cluster: ECSCluster,
        compose_file_path: str,
        project_name: Optional[str] = None,

        # Build options
        build_images: bool = True,
        force_rebuild: bool = False,
        push_images: bool = True,
        image_tag: str = 'latest',

        # Environment
        environment: Optional[Dict[str, str]] = None,

        # Volume handling
        create_efs_for_volumes: bool = True,

        # Deployment
        version: str = '',
        deployed_by: str = 'system',
        desired_count: int = 1,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
        wait_for_stable: bool = True,
        timeout: int = 300,

        # Behavior
        strict_mode: bool = False,
        dry_run: bool = False,

        target: Optional[DeploymentTarget] = None,

        # Event handler for observability
        event_handler: Optional[callable] = None,
    ) -> Deployment:
        """
        Deploy using the modular pipeline architecture.

        This method uses the new pipeline framework for better modularity,
        automatic rollback on failure, and improved observability.

        Args:
            cluster: Target ECS cluster
            compose_file_path: Path to docker-compose.yml
            project_name: Project name (defaults to directory name)

            build_images: Whether to build images with build contexts
            force_rebuild: Force rebuild even if image exists
            push_images: Push built images to ECR
            image_tag: Tag for built images

            environment: Additional environment variables to inject

            create_efs_for_volumes: Create EFS for named volumes

            version: Version tag for this deployment
            deployed_by: Who is deploying
            desired_count: Number of tasks to run
            cpu: Override CPU (Fargate units)
            memory: Override memory (MB)
            wait_for_stable: Wait for service to be stable
            timeout: Timeout for waiting

            strict_mode: Fail on warnings instead of continuing
            dry_run: Print what would happen without deploying

            target: Optional DeploymentTarget for tracking
            event_handler: Optional callback for pipeline events

        Returns:
            Deployment model instance

        Raises:
            ECSDeploymentError: If deployment fails
        """
        compose_path = Path(compose_file_path)

        if not compose_path.exists():
            raise ValidationError(f"Compose file not found: {compose_path}")

        if not project_name:
            project_name = compose_path.parent.name

        # Ensure cluster is ready
        self._ensure_cluster_networking(cluster)
        self._ensure_cluster_execution_role(cluster)

        # Create pipeline context with all parameters
        context = PipelineContext(
            cluster=cluster,
            compose_file_path=compose_path,
            project_name=project_name,
            image_tag=image_tag,

            # Build options
            build_images=build_images,
            force_rebuild=force_rebuild,
            push_images=push_images,

            # Environment
            environment=environment or {},

            # Volume handling
            create_efs_for_volumes=create_efs_for_volumes,

            # Deployment settings
            desired_count=desired_count,
            cpu=cpu,
            memory=memory,
            wait_for_stable=wait_for_stable,
            timeout=timeout,

            # Behavior
            strict_mode=strict_mode,
            dry_run=dry_run,

            # Tracking
            deployed_by=deployed_by,
            version=version,
            target=target,
        )

        # Build and configure the pipeline
        pipeline = PipelineBuilder.standard_deployment()

        # Attach event handler for logging if provided
        if event_handler:
            pipeline.attach_event_handler(event_handler)

        # Default logging handler
        def log_handler(event_type, **kwargs):
            step = kwargs.get('step', '')
            message = kwargs.get('message', '')
            if event_type == 'step_started':
                self.log_info(f"Step started: {step}")
            elif event_type == 'step_completed':
                self.log_info(f"Step completed: {step} - {message}")
            elif event_type == 'step_failed':
                self.log_error(f"Step failed: {step} - {message}")
            elif event_type == 'pipeline_completed':
                duration = kwargs.get('duration', 0)
                self.log_info(f"Pipeline completed in {duration:.1f}s")
            elif event_type == 'rollback_started':
                self.log_warning("Rollback started due to failure")

        pipeline.attach_event_handler(log_handler)

        # Execute the pipeline
        result: PipelineResult = pipeline.execute(context)

        # Handle pipeline result
        if result.success:
            self.audit_service.log(
                action=AuditAction.DEPLOYMENT_COMPLETED,
                actor=deployed_by,
                resource_type='ecs_pipeline_deployment',
                resource_id=context.deployment.id if context.deployment else None,
                resource_name=project_name,
                details={
                    'cluster': cluster.name,
                    'project': project_name,
                    'version': version,
                    'duration': result.duration_seconds,
                    'steps_completed': result.completed_steps,
                },
            )

            self.notify_observers(
                'ecs_pipeline_deployment_success',
                deployment=context.deployment,
                result=result
            )

            return context.deployment

        else:
            # Deployment failed
            error_message = str(result.error) if result.error else "Unknown error"

            self.audit_service.log(
                action=AuditAction.DEPLOYMENT_FAILED,
                actor=deployed_by,
                resource_type='ecs_pipeline_deployment',
                resource_id=context.deployment.id if context.deployment else None,
                resource_name=project_name,
                details={
                    'cluster': cluster.name,
                    'project': project_name,
                    'failed_step': result.failed_step,
                    'completed_steps': result.completed_steps,
                    'error': error_message,
                },
                success=False,
                error_message=error_message,
            )

            self.notify_observers(
                'ecs_pipeline_deployment_failed',
                deployment=context.deployment,
                error=result.error,
                failed_step=result.failed_step
            )

            raise ECSDeploymentError(
                f"Pipeline deployment failed at step '{result.failed_step}': {error_message}"
            )

    def deploy_update(
        self,
        ecs_service: ECSServiceModel,
        compose_file_path: Optional[str] = None,
        force_new_deployment: bool = False,
        desired_count: Optional[int] = None,
        deployed_by: str = 'system',
        wait_for_stable: bool = True,
        timeout: int = 300,
    ) -> Deployment:
        """
        Update an existing ECS service.

        Can update with a new compose file or just force a new deployment
        with the existing task definition.
        """
        start_time = timezone.now()
        cluster = ecs_service.cluster

        deployment = Deployment.objects.create(
            project_name=ecs_service.name,
            version='update',
            deployed_by=deployed_by,
            status=Deployment.Status.RUNNING,
            metadata={
                'type': 'ecs_update',
                'service': ecs_service.name,
                'cluster': cluster.name,
            }
        )

        try:
            task_definition = ecs_service.task_definition

            if compose_file_path:
                compose_path = Path(compose_file_path)
                if not compose_path.exists():
                    raise ValidationError(f"Compose file not found: {compose_path}")

                self._log_step(deployment, "Converting new compose file")
                task_definition = self._convert_compose(
                    compose_path=compose_path,
                    cluster=cluster,
                    project_name=ecs_service.name,
                )

                self._log_step(deployment, "Registering new task definition")
                task_definition = self.ecs_service.register_task_definition(task_definition)

            self._log_step(deployment, "Updating service")
            ecs_service = self.ecs_service.update_service(
                ecs_service=ecs_service,
                task_definition=task_definition,
                desired_count=desired_count,
                force_new_deployment=force_new_deployment or compose_file_path is not None,
            )

            if wait_for_stable:
                self._log_step(deployment, "Waiting for stability")
                ecs_service = self.ecs_service.wait_for_service_stable(
                    ecs_service=ecs_service,
                    timeout=timeout,
                )

            end_time = timezone.now()
            duration = (end_time - start_time).total_seconds()

            deployment.status = Deployment.Status.SUCCESS
            deployment.completed_at = end_time
            # remote-compose-mps: ``Deployment.duration`` is a @property
            # computed from completed_at - started_at; assigning to it
            # raised AttributeError. Setting completed_at above is enough
            # for the property to return the right value to readers.
            deployment.save()

            ecs_service.deployments.add(deployment)
            ecs_service.last_deployment_at = end_time
            ecs_service.save()

            self._log_step(deployment, f"Update completed in {duration:.1f}s")
            return deployment

        except Exception as e:
            deployment.status = Deployment.Status.FAILED
            deployment.error_message = str(e)
            deployment.completed_at = timezone.now()
            deployment.save()

            self._log_step(deployment, f"Update failed: {e}", level='error')
            raise

    def scale(
        self,
        ecs_service: ECSServiceModel,
        desired_count: int,
        wait_for_stable: bool = True,
        timeout: int = 300,
    ) -> ECSServiceModel:
        """
        Scale an ECS service to a desired count.

        Args:
            ecs_service: Service to scale
            desired_count: New desired task count
            wait_for_stable: Wait for scaling to complete
            timeout: Timeout for waiting

        Returns:
            Updated ECSService
        """
        self.log_info(f"Scaling {ecs_service.name} from {ecs_service.desired_count} to {desired_count}")

        ecs_service = self.ecs_service.update_service(
            ecs_service=ecs_service,
            desired_count=desired_count,
        )

        if wait_for_stable:
            ecs_service = self.ecs_service.wait_for_service_stable(
                ecs_service=ecs_service,
                timeout=timeout,
            )

        self.log_info(f"Scaled {ecs_service.name} to {ecs_service.running_count} tasks")
        return ecs_service

    def rollback(
        self,
        ecs_service: ECSServiceModel,
        to_task_definition: Optional[ECSTaskDefinition] = None,
        wait_for_stable: bool = True,
        timeout: int = 300,
    ) -> ECSServiceModel:
        """
        Rollback service to a previous task definition.

        If no task definition specified, rolls back to the previous revision.
        """
        current_task_def = ecs_service.task_definition

        if not to_task_definition:
            previous = ECSTaskDefinition.objects.filter(
                cluster=ecs_service.cluster,
                name=current_task_def.name,
                revision__lt=current_task_def.revision,
                status=ECSTaskDefinition.Status.REGISTERED,
            ).order_by('-revision').first()

            if not previous:
                raise ECSDeploymentError("No previous task definition found for rollback")

            to_task_definition = previous

        self.log_info(
            f"Rolling back {ecs_service.name} from "
            f"{current_task_def.full_arn} to {to_task_definition.full_arn}"
        )

        ecs_service = self.ecs_service.update_service(
            ecs_service=ecs_service,
            task_definition=to_task_definition,
            force_new_deployment=True,
        )

        if wait_for_stable:
            ecs_service = self.ecs_service.wait_for_service_stable(
                ecs_service=ecs_service,
                timeout=timeout,
            )

        self.log_info(f"Rollback complete: {ecs_service.name}")
        return ecs_service

    def get_service_status(
        self,
        ecs_service: ECSServiceModel,
    ) -> Dict[str, Any]:
        """Get current status of an ECS service."""
        aws_service = self.ecs_service.describe_service(
            cluster_name=ecs_service.cluster.aws_cluster_name,
            service_name=ecs_service.name,
            region=ecs_service.cluster.aws_region,
            credential=ecs_service.cluster.aws_credential,
        )

        ecs_service.update_from_aws(aws_service)

        task_arns = self.ecs_service.list_tasks(
            cluster=ecs_service.cluster,
            service_name=ecs_service.name,
        )

        tasks = []
        if task_arns:
            task_details = self.ecs_service.describe_tasks(
                cluster=ecs_service.cluster,
                task_arns=task_arns,
            )
            for task in task_details:
                tasks.append({
                    'task_arn': task['taskArn'],
                    'status': task['lastStatus'],
                    'desired_status': task['desiredStatus'],
                    'health': task.get('healthStatus', 'UNKNOWN'),
                    'started_at': task.get('startedAt'),
                })

        return {
            'service_name': ecs_service.name,
            'cluster': ecs_service.cluster.name,
            'status': ecs_service.status,
            'desired_count': ecs_service.desired_count,
            'running_count': ecs_service.running_count,
            'pending_count': ecs_service.pending_count,
            'is_healthy': ecs_service.is_healthy,
            'task_definition': ecs_service.task_definition.full_arn,
            'last_deployment': ecs_service.last_deployment_at,
            'tasks': tasks,
        }

    def list_services(
        self,
        cluster: ECSCluster,
    ) -> List[Dict[str, Any]]:
        """List all services in a cluster with their status."""
        services = []

        for ecs_service in cluster.services.filter(
            status__in=[
                ECSServiceModel.ServiceStatus.ACTIVE,
                ECSServiceModel.ServiceStatus.UPDATING,
            ]
        ):
            try:
                status = self.get_service_status(ecs_service)
                services.append(status)
            except Exception as e:
                services.append({
                    'service_name': ecs_service.name,
                    'status': 'error',
                    'error': str(e),
                })

        return services

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _ensure_cluster_networking(self, cluster: ECSCluster) -> None:
        """Ensure cluster has networking configured for Fargate."""
        if cluster.launch_type == ECSCluster.LaunchType.FARGATE:
            if not cluster.subnet_ids or not cluster.security_group_ids:
                self.log_info("Discovering VPC networking for cluster")
                self.ecs_service.sync_cluster_networking(cluster)

    def _ensure_cluster_execution_role(self, cluster: ECSCluster) -> None:
        """Ensure cluster has an execution role for Fargate."""
        if cluster.launch_type == ECSCluster.LaunchType.FARGATE:
            if not cluster.task_execution_role_arn:
                self.log_info("Configuring task execution role for cluster")
                self.ecs_service.ensure_cluster_has_execution_role(cluster)

    def _convert_compose(
        self,
        compose_path: Path,
        cluster: ECSCluster,
        project_name: str,
        environment: Optional[Dict[str, str]] = None,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
    ) -> ECSTaskDefinition:
        """Convert compose file to task definition."""
        content = compose_path.read_text()

        if environment:
            import yaml
            compose_dict = yaml.safe_load(content)
            for service_name, service_config in compose_dict.get('services', {}).items():
                existing_env = service_config.get('environment', {})
                if isinstance(existing_env, list):
                    existing_env = {
                        e.split('=')[0]: e.split('=')[1]
                        for e in existing_env if '=' in e
                    }
                existing_env.update(environment)
                service_config['environment'] = existing_env
            content = yaml.dump(compose_dict)

        return self.compose_converter.convert(
            compose_content=content,
            cluster=cluster,
            task_family_name=project_name,
            cpu=cpu,
            memory=memory,
        )

    def _get_or_create_service(
        self,
        cluster: ECSCluster,
        task_definition: ECSTaskDefinition,
        project_name: str,
        desired_count: int,
    ) -> ECSServiceModel:
        """Get existing service or create a new one."""
        # Check local database first
        existing = ECSServiceModel.objects.filter(
            cluster=cluster,
            name=project_name,
        ).first()

        if existing:
            return existing

        # Check if service exists in AWS (but not in local DB)
        try:
            aws_service = self.ecs_service.describe_service(
                cluster_name=cluster.aws_cluster_name,
                service_name=project_name,
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )
            # Only treat ACTIVE services as existing - INACTIVE services should be recreated
            aws_status = aws_service.get('status', 'UNKNOWN')
            if aws_status == 'ACTIVE':
                # Service exists and is active in AWS, import it to local DB
                self.log_info(f"Found existing active service in AWS: {project_name}")
                ecs_service = ECSServiceModel.objects.create(
                    name=project_name,
                    cluster=cluster,
                    task_definition=task_definition,
                    aws_service_arn=aws_service['serviceArn'],
                    desired_count=aws_service.get('desiredCount', desired_count),
                    running_count=aws_service.get('runningCount', 0),
                    pending_count=aws_service.get('pendingCount', 0),
                    status=ECSServiceModel.ServiceStatus.ACTIVE,
                )
                return ecs_service
            else:
                self.log_info(f"Found {aws_status} service in AWS: {project_name} - will create new")
        except Exception:
            # Service doesn't exist in AWS, create new
            pass

        return ECSServiceModel.objects.create(
            name=project_name,
            cluster=cluster,
            task_definition=task_definition,
            desired_count=desired_count,
            status=ECSServiceModel.ServiceStatus.PENDING,
        )

    def _create_deployment_record(
        self,
        compose_path: Path,
        project_name: str,
        version: str,
        deployed_by: str,
        target: Optional[DeploymentTarget],
        cluster: ECSCluster,
    ) -> Deployment:
        """Create a deployment record for tracking."""
        compose_content = compose_path.read_text()

        # For ECS deployments, we may not have a traditional target/context
        # Create a minimal deployment record that tracks ECS-specific info
        deployment = Deployment(
            project_name=project_name,
            compose_file_path=str(compose_path),
            compose_content=compose_content,
            version=version,
            deployed_by=deployed_by,
            status=Deployment.Status.RUNNING,
            started_at=timezone.now(),
            metadata={
                'type': 'ecs',
                'cluster_name': cluster.name,
                'cluster_arn': cluster.aws_cluster_arn,
                'region': cluster.aws_region,
            },
        )

        # If we have a target, use it. Otherwise we need to handle ECS without target/context
        if target:
            deployment.target = target
            if hasattr(target, 'contexts') and target.contexts.exists():
                deployment.context = target.contexts.first()

        # For ECS deployments without a target, we need to create placeholder relationships
        # or handle this differently. For now, skip saving if no target.
        if target and deployment.context_id:
            deployment.save()
        else:
            # Create without the DB save - track in memory for ECS
            deployment.id = None  # Will be set when we can save properly

        return deployment

    def _log_step(
        self,
        deployment: Deployment,
        message: str,
        level: str = 'info',
    ) -> None:
        """Log a deployment step."""
        # Always print to stdout for visibility
        prefix = ''
        if level == 'warning':
            prefix = 'Warning: '
        elif level == 'error':
            prefix = 'ERROR: '
        print(f"{prefix}{message}")

        # Only save to database if deployment is saved
        if deployment.id:
            log_level_map = {
                'info': DeploymentLog.LogLevel.INFO,
                'warning': DeploymentLog.LogLevel.WARNING,
                'error': DeploymentLog.LogLevel.ERROR,
            }

            DeploymentLog.objects.create(
                deployment=deployment,
                log_level=log_level_map.get(level, DeploymentLog.LogLevel.INFO),
                message=message,
            )

        # Always log to console/service logger
        if level == 'error':
            self.log_error(message)
        elif level == 'warning':
            self.log_warning(message)
        else:
            self.log_info(message)
