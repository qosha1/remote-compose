"""
Deployment Pipeline Framework.

A modular, extensible pipeline architecture for orchestrating
Docker Compose to ECS deployments.

Example usage:

    from remote_compose.services.deployment_pipeline import (
        DeploymentPipeline,
        PipelineContext,
        PipelineBuilder,
    )

    # Create context with deployment parameters
    context = PipelineContext(
        cluster=cluster,
        compose_file_path=Path('/path/to/docker-compose.yml'),
        project_name='my-project',
        image_tag='v1.0.0',
    )

    # Build the standard deployment pipeline
    pipeline = PipelineBuilder.standard_deployment()

    # Execute
    result = pipeline.execute(context)

    if result.success:
        print(f"Deployed: {context.ecs_service.aws_service_arn}")
    else:
        print(f"Failed: {result.error_message}")
"""

from .context import (
    PipelineContext,
    DeploymentConfig,
    InfrastructureState,
    ImageState,
    EFSState,
    ServiceRegistry,
)
from .step import (
    PipelineStep,
    StepResult,
    CompositeStep,
    ConditionalStep,
)
from .pipeline import (
    DeploymentPipeline,
    PipelineResult,
    PipelineBuilder,
)
from .steps import (
    # Initialization
    InitializeDeploymentStep,
    # Preprocessing
    PreprocessComposeStep,
    # ECR
    AuthenticateECRStep,
    CreateECRRepositoriesStep,
    # Build
    BuildAndPushImagesStep,
    # EFS
    SetupEFSVolumesStep,
    # ECS
    ConvertToTaskDefinitionStep,
    RegisterTaskDefinitionStep,
    CreateOrUpdateServiceStep,
    WaitForStabilityStep,
    # Finalization
    FinalizeDeploymentStep,
    RecordDeploymentFailureStep,
)

__all__ = [
    # Core classes
    "PipelineContext",
    "DeploymentConfig",
    "InfrastructureState",
    "ImageState",
    "EFSState",
    "ServiceRegistry",
    "PipelineStep",
    "StepResult",
    "CompositeStep",
    "ConditionalStep",
    "DeploymentPipeline",
    "PipelineResult",
    "PipelineBuilder",
    # Steps
    "InitializeDeploymentStep",
    "PreprocessComposeStep",
    "AuthenticateECRStep",
    "CreateECRRepositoriesStep",
    "BuildAndPushImagesStep",
    "SetupEFSVolumesStep",
    "ConvertToTaskDefinitionStep",
    "RegisterTaskDefinitionStep",
    "CreateOrUpdateServiceStep",
    "WaitForStabilityStep",
    "FinalizeDeploymentStep",
    "RecordDeploymentFailureStep",
]
