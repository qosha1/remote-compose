"""
Initialization step for the deployment pipeline.

Creates and tracks the deployment record for monitoring and auditing.
"""

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class InitializeDeploymentStep(PipelineStep):
    """
    Create deployment record and initialize tracking.

    This is typically the first step in any deployment pipeline.
    It creates the Deployment model instance that tracks the
    entire deployment lifecycle.
    """

    def __init__(self):
        super().__init__("InitializeDeployment")

    def execute(self, context: PipelineContext) -> StepResult:
        """Create deployment record."""
        from django.utils import timezone
        from ....models import Deployment

        # Read compose content
        compose_content = context.compose_file_path.read_text()

        # Create deployment record
        deployment = Deployment(
            project_name=context.project_name,
            compose_file_path=str(context.compose_file_path),
            compose_content=compose_content,
            version=context.version,
            deployed_by=context.deployed_by,
            status=Deployment.Status.RUNNING,
            started_at=timezone.now(),
            environment=context.environment or {},
            metadata={
                'type': 'ecs',
                'cluster_name': context.cluster.name,
                'cluster_arn': context.cluster.aws_cluster_arn,
                'region': context.cluster.aws_region,
                'image_tag': context.image_tag,
                'dry_run': context.dry_run,
            },
        )

        # Set relationships if available
        if context.target:
            deployment.target = context.target
            if hasattr(context.target, 'contexts') and context.target.contexts.exists():
                deployment.context = context.target.contexts.first()

        # Only save if we have required relationships
        # (or if the model allows null context/target)
        try:
            if deployment.context_id and deployment.target_id:
                deployment.save()
        except Exception:
            # If save fails, we'll proceed with unsaved deployment
            # The finalization step will try to save again
            pass

        context.deployment = deployment

        return StepResult.ok(
            f"Initialized deployment for project '{context.project_name}'"
        )

    def cleanup(self, context: PipelineContext) -> None:
        """Mark deployment as failed if rollback is triggered."""
        from django.utils import timezone

        if context.deployment:
            context.deployment.status = 'failed'
            context.deployment.completed_at = timezone.now()
            context.deployment.error_message = "Deployment rolled back due to pipeline failure"

            if context.deployment.id:
                try:
                    context.deployment.save()
                except Exception:
                    pass
