"""
ECR-related pipeline steps.

Handles ECR authentication and repository creation.
"""

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class AuthenticateECRStep(PipelineStep):
    """
    Authenticate Docker with ECR.

    Required before pushing images to ECR repositories.
    Only runs if there are services that need to be built.
    """

    def __init__(self):
        super().__init__("AuthenticateECR")

    def should_run(self, context: PipelineContext) -> bool:
        """Only run if we need to build and push images."""
        return (
            context.build_images and
            context.push_images and
            context.has_build_services
        )

    def execute(self, context: PipelineContext) -> StepResult:
        """Authenticate with ECR."""
        if context.dry_run:
            return StepResult.ok("[DRY RUN] Would authenticate with ECR")

        image_build_service = context.services.image_build

        try:
            image_build_service.authenticate_ecr(
                region=context.cluster.aws_region,
                credential=context.cluster.aws_credential,
            )
        except Exception as e:
            return StepResult.fail(
                f"ECR authentication failed: {e}",
                error=e
            )

        return StepResult.ok(
            f"Authenticated with ECR in {context.cluster.aws_region}"
        )


class CreateECRRepositoriesStep(PipelineStep):
    """
    Create ECR repositories for services that need to be built.

    Creates one repository per service that has a build configuration.
    Repositories are named: {project_name}/{service_name}
    """

    def __init__(self):
        super().__init__("CreateECRRepositories")

    def should_run(self, context: PipelineContext) -> bool:
        """Only run if we need to build images."""
        return context.build_images and context.has_build_services

    def execute(self, context: PipelineContext) -> StepResult:
        """Create ECR repositories."""
        if context.dry_run:
            build_services = context.preprocessed.get_build_services()
            return StepResult.ok(
                f"[DRY RUN] Would create {len(build_services)} ECR repositories"
            )

        ecr_service = context.services.ecr
        build_services = context.preprocessed.get_build_services()
        created_count = 0

        for service_name in build_services.keys():
            repo_name = f"{context.project_name}/{service_name}"

            try:
                repo = ecr_service.get_or_create_repository(
                    name=repo_name,
                    region=context.cluster.aws_region,
                    credential=context.cluster.aws_credential,
                )

                context.ecr_repositories[service_name] = repo
                context.track_resource(
                    resource_type='ecr_repository',
                    resource_id=repo.get('repository_arn', repo_name),
                    name=repo_name,
                    uri=repo.get('repository_uri'),
                )
                created_count += 1

            except Exception as e:
                return StepResult.fail(
                    f"Failed to create ECR repository '{repo_name}': {e}",
                    error=e
                )

        return StepResult.ok(
            f"Created/verified {created_count} ECR repositories"
        )

    def cleanup(self, context: PipelineContext) -> None:
        """
        Clean up created ECR repositories.

        Note: We don't delete ECR repositories on rollback by default
        since they may contain useful images from previous deployments.
        This can be overridden if needed.
        """
        # By design, we don't delete ECR repositories on rollback
        # They're persistent infrastructure, not ephemeral resources
        pass
