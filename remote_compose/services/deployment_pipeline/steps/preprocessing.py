"""
Compose file preprocessing step.

Handles YAML anchor resolution, env_file parsing, and build context analysis.
"""

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class PreprocessComposeStep(PipelineStep):
    """
    Preprocess the docker-compose file.

    This step:
    - Resolves YAML anchors and aliases
    - Parses env_file directives
    - Analyzes build contexts
    - Identifies services that need image builds
    - Identifies named volumes that need EFS
    """

    def __init__(self):
        super().__init__("PreprocessCompose")

    def execute(self, context: PipelineContext) -> StepResult:
        """Preprocess compose file."""
        from ...compose_preprocessor import ComposePreprocessor

        # Get AWS account ID for ECR repository naming
        ecr_service = context.services.ecr
        try:
            account_id = ecr_service.get_account_id(
                credential=context.cluster.aws_credential
            )
            context.account_id = account_id
        except Exception as e:
            return StepResult.fail(
                f"Failed to get AWS account ID: {e}",
                error=e
            )

        # Configure and run preprocessor
        preprocessor = ComposePreprocessor(
            aws_account_id=account_id,
            aws_region=context.cluster.aws_region,
            ecr_repository_prefix=None,
        )

        try:
            preprocessed = preprocessor.preprocess_file(
                compose_path=str(context.compose_file_path),
                project_name=context.project_name,
                image_tag=context.image_tag,
            )
        except Exception as e:
            return StepResult.fail(
                f"Failed to preprocess compose file: {e}",
                error=e
            )

        # Check for preprocessing errors
        if preprocessed.errors:
            error_summary = "; ".join(preprocessed.errors[:3])
            if len(preprocessed.errors) > 3:
                error_summary += f" (and {len(preprocessed.errors) - 3} more)"
            return StepResult.fail(f"Preprocessing errors: {error_summary}")

        # Collect warnings
        for warning in preprocessed.warnings:
            context.add_warning(warning)

        # Fail in strict mode if there are warnings
        if context.strict_mode and preprocessed.warnings:
            return StepResult.fail(
                f"Strict mode: {len(preprocessed.warnings)} preprocessing warnings"
            )

        # Apply additional environment variables
        if context.environment:
            for service in preprocessed.services.values():
                service.env_vars.update(context.environment)

        context.preprocessed = preprocessed

        # Generate summary
        active_count = len(preprocessed.get_active_services())
        build_count = len(preprocessed.get_build_services())
        volume_count = len(preprocessed.named_volumes)

        summary_parts = [f"{active_count} services"]
        if build_count > 0:
            summary_parts.append(f"{build_count} need build")
        if volume_count > 0:
            summary_parts.append(f"{volume_count} named volumes")

        return StepResult.ok(
            f"Preprocessed compose: {', '.join(summary_parts)}"
        )
