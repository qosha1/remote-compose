"""
ECS-related pipeline steps.

Handles task definition conversion, registration, service creation,
and deployment stability waiting.
"""

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class ConvertToTaskDefinitionStep(PipelineStep):
    """
    Convert preprocessed compose to ECS task definition.

    Transforms the preprocessed compose configuration into an
    ECS task definition format, handling container definitions,
    resource allocation, and EFS volume mounts.
    """

    def __init__(self):
        super().__init__("ConvertToTaskDefinition")

    def execute(self, context: PipelineContext) -> StepResult:
        """Convert to task definition."""
        from ...compose_converter import ComposeToECSConverter

        converter = ComposeToECSConverter()  # stateful per-call, not from registry

        try:
            task_definition = converter.convert_preprocessed(
                preprocessed=context.preprocessed,
                cluster=context.cluster,
                task_family_name=context.project_name,
                efs_config=context.efs_config,
                cpu=context.cpu,
                memory=context.memory,
                strict_mode=context.strict_mode,
            )
            task_definition.save()
        except Exception as e:
            return StepResult.fail(
                f"Task definition conversion failed: {e}",
                error=e
            )

        # Collect converter warnings
        for warning in converter.warnings:
            context.add_warning(warning)

        context.task_definition = task_definition

        container_count = len(task_definition.container_definitions)

        return StepResult.ok(
            f"Task definition created: {task_definition.name} "
            f"({container_count} containers, CPU: {task_definition.cpu}, "
            f"Memory: {task_definition.memory}MB)"
        )


class RegisterTaskDefinitionStep(PipelineStep):
    """
    Register task definition with AWS ECS.

    Registers the task definition and updates it with the ARN
    returned by AWS.
    """

    def __init__(self):
        super().__init__("RegisterTaskDefinition")

    def execute(self, context: PipelineContext) -> StepResult:
        """Register task definition."""
        if context.dry_run:
            return StepResult.ok(
                "[DRY RUN] Would register task definition"
            )

        ecs_service = context.services.ecs

        try:
            task_definition = ecs_service.register_task_definition(
                context.task_definition
            )
            context.task_definition = task_definition

            context.track_resource(
                resource_type='ecs_task_definition',
                resource_id=task_definition.aws_task_definition_arn,
                family=task_definition.name,
                revision=task_definition.revision,
            )

        except Exception as e:
            return StepResult.fail(
                f"Task definition registration failed: {e}",
                error=e
            )

        return StepResult.ok(
            f"Registered: {task_definition.aws_task_definition_arn}"
        )


class CreateOrUpdateServiceStep(PipelineStep):
    """
    Create new ECS service or update existing one.

    Handles both initial service creation and updates to existing
    services, including force new deployment for updates.
    """

    def __init__(self):
        super().__init__("CreateOrUpdateService")
        self._created_new_service = False

    def execute(self, context: PipelineContext) -> StepResult:
        """Create or update ECS service."""
        self._created_new_service = False
        if context.dry_run:
            return StepResult.ok(
                "[DRY RUN] Would create/update ECS service"
            )

        from ....models import ECSService as ECSServiceModel

        ecs_service = context.services.ecs

        # Check for existing service
        service_model = self._get_or_create_service_model(context)

        try:
            if service_model.aws_service_arn:
                # Update existing service
                service_model = ecs_service.update_service(
                    ecs_service=service_model,
                    task_definition=context.task_definition,
                    desired_count=context.desired_count,
                    force_new_deployment=True,
                )
                action = "Updated"
            else:
                # Create new service
                service_model = ecs_service.create_service(service_model)
                self._created_new_service = True
                action = "Created"

            context.ecs_service = service_model

            context.track_resource(
                resource_type='ecs_service',
                resource_id=service_model.aws_service_arn,
                name=service_model.name,
            )

            return StepResult.ok(
                f"{action} service: {service_model.name} "
                f"(desired: {service_model.desired_count})"
            )

        except Exception as e:
            return StepResult.fail(
                f"Service create/update failed: {e}",
                error=e
            )

    def _get_or_create_service_model(self, context):
        """Get existing service model or create new one."""
        from ....models import ECSService as ECSServiceModel

        # Check database first
        existing = ECSServiceModel.objects.filter(
            cluster=context.cluster,
            name=context.project_name,
        ).first()

        if existing:
            # Update task definition reference
            existing.task_definition = context.task_definition
            existing.save()
            return existing

        # Check AWS for existing service not in our database
        ecs_service = context.services.ecs
        try:
            aws_service = ecs_service.describe_service(
                cluster_name=context.cluster.aws_cluster_name,
                service_name=context.project_name,
                region=context.cluster.aws_region,
                credential=context.cluster.aws_credential,
            )

            # Check if service is active (not INACTIVE)
            if aws_service.get('status') == 'ACTIVE':
                return ECSServiceModel.objects.create(
                    name=context.project_name,
                    cluster=context.cluster,
                    task_definition=context.task_definition,
                    aws_service_arn=aws_service['serviceArn'],
                    desired_count=aws_service.get('desiredCount', context.desired_count),
                    running_count=aws_service.get('runningCount', 0),
                    pending_count=aws_service.get('pendingCount', 0),
                    status=ECSServiceModel.ServiceStatus.ACTIVE,
                )
        except Exception:
            pass

        # Create new service model
        return ECSServiceModel.objects.create(
            name=context.project_name,
            cluster=context.cluster,
            task_definition=context.task_definition,
            desired_count=context.desired_count,
            status=ECSServiceModel.ServiceStatus.PENDING,
        )

    def cleanup(self, context: PipelineContext) -> None:
        """
        Clean up newly created service on rollback.

        Only deletes service if it was created by this pipeline run.
        Does not delete pre-existing services.
        """
        if not self._created_new_service:
            return

        if not context.ecs_service:
            return

        ecs_service = context.services.ecs

        try:
            # Scale down first
            ecs_service.scale_service(
                cluster=context.cluster,
                service_name=context.ecs_service.name,
                desired_count=0,
            )

            # Delete service
            ecs_service.delete_service(
                cluster=context.cluster,
                service_name=context.ecs_service.name,
            )

            # Delete model
            context.ecs_service.delete()

        except Exception:
            # Log but don't fail cleanup
            pass


class WaitForStabilityStep(PipelineStep):
    """
    Wait for ECS service to reach stable state.

    Polls AWS until the service deployment is complete and all
    tasks are running, or until timeout is reached.
    """

    def __init__(self):
        super().__init__("WaitForStability")

    def should_run(self, context: PipelineContext) -> bool:
        """Only run if wait_for_stable is enabled and not dry run."""
        return context.wait_for_stable and not context.dry_run

    def execute(self, context: PipelineContext) -> StepResult:
        """Wait for service stability."""
        ecs_service = context.services.ecs

        self.emit_event(
            'waiting_for_stability',
            service=context.ecs_service.name,
            timeout=context.timeout
        )

        try:
            service_model = ecs_service.wait_for_service_stable(
                ecs_service=context.ecs_service,
                timeout=context.timeout,
            )

            context.ecs_service = service_model

            return StepResult.ok(
                f"Service stable: {service_model.running_count}/"
                f"{service_model.desired_count} tasks running"
            )

        except Exception as e:
            # Check if it's a timeout
            error_msg = str(e)
            if 'timeout' in error_msg.lower():
                return StepResult.fail(
                    f"Service did not stabilize within {context.timeout}s: {e}",
                    error=e
                )
            return StepResult.fail(
                f"Error waiting for stability: {e}",
                error=e
            )
