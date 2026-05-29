"""
Deployment finalization step.

Handles final status updates and deployment record completion.
"""

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class FinalizeDeploymentStep(PipelineStep):
    """
    Finalize deployment and update status.

    This step:
    - Updates the Deployment record with final status
    - Records deployment duration
    - Stores final service state
    - Generates deployment summary
    """

    def __init__(self):
        super().__init__("FinalizeDeployment")

    def execute(self, context: PipelineContext) -> StepResult:
        """Finalize the deployment."""
        if context.dry_run:
            return self._dry_run_summary(context)

        from django.utils import timezone
        from ....models import Deployment

        # Update deployment record
        if context.deployment:
            try:
                context.deployment.status = Deployment.DeploymentStatus.COMPLETED
                context.deployment.completed_at = timezone.now()

                # Store service ARN if available
                if context.ecs_service:
                    context.deployment.ecs_service_arn = (
                        context.ecs_service.aws_service_arn
                    )

                # Store task definition ARN
                if context.task_definition:
                    context.deployment.task_definition_arn = (
                        context.task_definition.aws_task_definition_arn
                    )

                context.deployment.save()

            except Exception as e:
                # Don't fail deployment for record-keeping issues
                context.add_warning(f"Failed to update deployment record: {e}")

        # Generate summary
        summary = self._generate_summary(context)

        return StepResult.ok(summary)

    def _generate_summary(self, context: PipelineContext) -> str:
        """Generate deployment summary."""
        parts = []

        # Service info
        if context.ecs_service:
            parts.append(f"Service: {context.ecs_service.name}")
            if context.ecs_service.running_count is not None:
                parts.append(
                    f"Tasks: {context.ecs_service.running_count}/"
                    f"{context.ecs_service.desired_count}"
                )

        # Task definition
        if context.task_definition:
            parts.append(f"Task: {context.task_definition.name}")

        # Built images
        if context.built_images:
            parts.append(f"Images built: {len(context.built_images)}")

        # Warnings
        if context.warnings:
            parts.append(f"Warnings: {len(context.warnings)}")

        return (
            "Deployment complete: " + ", ".join(parts)
            if parts
            else "Deployment complete"
        )

    def _dry_run_summary(self, context: PipelineContext) -> StepResult:
        """Generate dry run summary."""
        parts = ["[DRY RUN] Deployment simulation complete"]

        if context.preprocessed:
            service_count = len(context.preprocessed.get_active_services())
            parts.append(f"{service_count} services")

            build_count = len(context.preprocessed.get_build_services())
            if build_count:
                parts.append(f"{build_count} would be built")

        if context.warnings:
            parts.append(f"{len(context.warnings)} warnings")

        return StepResult.ok(", ".join(parts))


class RecordDeploymentFailureStep(PipelineStep):
    """
    Record deployment failure in the database.

    This step is called by the pipeline when a failure occurs,
    not as part of the normal step sequence.
    """

    def __init__(self):
        super().__init__("RecordDeploymentFailure")

    def execute(self, context: PipelineContext) -> StepResult:
        """Record the failure."""
        from django.utils import timezone
        from ....models import Deployment

        if not context.deployment:
            return StepResult.ok("No deployment record to update")

        try:
            context.deployment.status = Deployment.DeploymentStatus.FAILED
            context.deployment.completed_at = timezone.now()

            # Store error info if available
            if context.errors:
                context.deployment.error_message = "; ".join(context.errors[:5])

            context.deployment.save()

            return StepResult.ok("Deployment failure recorded")

        except Exception as e:
            return StepResult.ok(f"Could not record failure: {e}")
