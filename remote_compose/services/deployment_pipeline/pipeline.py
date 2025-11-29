"""
Deployment pipeline executor.

Orchestrates step execution with event emission, error handling,
and automatic rollback on failure.
"""

from typing import List, Optional, Callable
from dataclasses import dataclass, field
import time

from .context import PipelineContext
from .step import PipelineStep, StepResult


@dataclass
class PipelineResult:
    """
    Result from executing a complete pipeline.

    Contains the final state, completed steps, and any error information.
    """

    success: bool
    context: PipelineContext
    completed_steps: List[str] = field(default_factory=list)
    failed_step: Optional[str] = None
    error: Optional[Exception] = None
    duration_seconds: float = 0.0

    @property
    def summary(self) -> str:
        """Generate a human-readable summary."""
        if self.success:
            return (
                f"Pipeline completed successfully in {self.duration_seconds:.1f}s. "
                f"Steps: {', '.join(self.completed_steps)}"
            )
        else:
            return (
                f"Pipeline failed at '{self.failed_step}' after {self.duration_seconds:.1f}s. "
                f"Completed: {', '.join(self.completed_steps) or 'none'}. "
                f"Error: {self.error}"
            )


class DeploymentPipeline:
    """
    Orchestrates execution of deployment pipeline steps.

    Handles:
    - Sequential step execution
    - Event emission for observability
    - Error handling and propagation
    - Rollback/cleanup on failure

    Usage:
        pipeline = DeploymentPipeline(
            name="ECS Deployment",
            steps=[
                PreprocessStep(),
                BuildImagesStep(),
                DeployStep(),
            ],
        )

        result = pipeline.execute(context)
        if result.success:
            print(f"Deployed: {result.context.ecs_service}")
    """

    def __init__(
        self,
        name: str,
        steps: List[PipelineStep],
        enable_rollback: bool = True
    ):
        """
        Initialize the pipeline.

        Args:
            name: Pipeline name for identification
            steps: Ordered list of steps to execute
            enable_rollback: If True, cleanup completed steps on failure
        """
        self.name = name
        self.steps = steps
        self.enable_rollback = enable_rollback
        self._event_handlers: List[Callable] = []

    def attach_event_handler(self, handler: Callable) -> None:
        """
        Attach an event handler for pipeline events.

        The handler will also be propagated to all steps.

        Args:
            handler: Callable receiving (event_type, **kwargs)
        """
        self._event_handlers.append(handler)
        for step in self.steps:
            step.attach_event_handler(handler)

    def emit_event(self, event_type: str, **kwargs) -> None:
        """Emit a pipeline-level event."""
        for handler in self._event_handlers:
            try:
                handler(event_type, pipeline=self.name, **kwargs)
            except Exception:
                pass

    def execute(self, context: PipelineContext) -> PipelineResult:
        """
        Execute the pipeline.

        Runs each step in sequence, handling errors and performing
        rollback if configured.

        Args:
            context: Initial pipeline context

        Returns:
            PipelineResult with outcome, final context, and metadata
        """
        start_time = time.time()
        completed_steps: List[str] = []

        self.emit_event('pipeline_started', context=context, step_count=len(self.steps))

        try:
            for step in self.steps:
                result = step.run(context)

                if result.success:
                    completed_steps.append(step.name)

                    if not result.should_continue:
                        # Successful but requested early stop
                        duration = time.time() - start_time
                        self.emit_event(
                            'pipeline_stopped',
                            context=context,
                            reason=result.message,
                            completed_steps=completed_steps
                        )
                        return PipelineResult(
                            success=True,
                            context=context,
                            completed_steps=completed_steps,
                            duration_seconds=duration
                        )
                else:
                    # Step failed
                    duration = time.time() - start_time
                    self.emit_event(
                        'pipeline_failed',
                        failed_step=step.name,
                        error=result.error,
                        context=context,
                        completed_steps=completed_steps
                    )

                    # Rollback if enabled
                    if self.enable_rollback and completed_steps:
                        self._rollback(completed_steps, context)

                    return PipelineResult(
                        success=False,
                        context=context,
                        completed_steps=completed_steps,
                        failed_step=step.name,
                        error=result.error,
                        duration_seconds=duration
                    )

            # All steps completed successfully
            duration = time.time() - start_time
            self.emit_event(
                'pipeline_completed',
                context=context,
                completed_steps=completed_steps,
                duration=duration
            )

            return PipelineResult(
                success=True,
                context=context,
                completed_steps=completed_steps,
                duration_seconds=duration
            )

        except Exception as e:
            # Unexpected error during pipeline execution
            duration = time.time() - start_time
            self.emit_event('pipeline_error', error=e, context=context)

            if self.enable_rollback and completed_steps:
                self._rollback(completed_steps, context)

            return PipelineResult(
                success=False,
                context=context,
                completed_steps=completed_steps,
                error=e,
                duration_seconds=duration
            )

    def _rollback(self, completed_steps: List[str], context: PipelineContext) -> None:
        """
        Rollback completed steps in reverse order.

        Args:
            completed_steps: Names of steps that completed successfully
            context: Pipeline context
        """
        self.emit_event('rollback_started', completed_steps=completed_steps)

        # Find steps to rollback (in reverse order)
        steps_to_rollback = [
            step for step in reversed(self.steps)
            if step.name in completed_steps
        ]

        for step in steps_to_rollback:
            try:
                self.emit_event('step_cleanup_started', step=step.name)
                step.cleanup(context)
                self.emit_event('step_cleanup_completed', step=step.name)
            except Exception as e:
                self.emit_event('step_cleanup_failed', step=step.name, error=e)
                # Continue with other cleanups even if one fails

        self.emit_event('rollback_completed', steps_cleaned=len(steps_to_rollback))

    def dry_run(self, context: PipelineContext) -> List[str]:
        """
        Perform a dry run to show which steps would execute.

        Args:
            context: Pipeline context (with dry_run=True recommended)

        Returns:
            List of step names that would execute
        """
        would_run = []
        for step in self.steps:
            if step.should_run(context):
                would_run.append(step.name)
            else:
                would_run.append(f"[SKIP] {step.name}")
        return would_run

    def __repr__(self) -> str:
        step_names = [s.name for s in self.steps]
        return f"<DeploymentPipeline: {self.name} [{' -> '.join(step_names)}]>"


class PipelineBuilder:
    """
    Builder pattern for constructing pipelines.

    Provides a fluent interface for building pipelines with
    conditional steps and configuration.

    Usage:
        pipeline = (
            PipelineBuilder("ECS Deployment")
            .add_step(InitStep())
            .add_step(BuildStep(), condition=lambda ctx: ctx.build_images)
            .add_step(DeployStep())
            .with_rollback(True)
            .build()
        )
    """

    def __init__(self, name: str):
        self.name = name
        self.steps: List[PipelineStep] = []
        self.enable_rollback = True
        self._event_handlers: List[Callable] = []

    def add_step(
        self,
        step: PipelineStep,
        condition: Optional[Callable[[PipelineContext], bool]] = None
    ) -> 'PipelineBuilder':
        """
        Add a step to the pipeline.

        Args:
            step: The step to add
            condition: Optional condition for when step should run

        Returns:
            Self for chaining
        """
        if condition:
            from .step import ConditionalStep
            step = ConditionalStep(step, condition)
        self.steps.append(step)
        return self

    def with_rollback(self, enabled: bool) -> 'PipelineBuilder':
        """Enable or disable rollback on failure."""
        self.enable_rollback = enabled
        return self

    def with_event_handler(self, handler: Callable) -> 'PipelineBuilder':
        """Add an event handler."""
        self._event_handlers.append(handler)
        return self

    def build(self) -> DeploymentPipeline:
        """Build and return the configured pipeline."""
        pipeline = DeploymentPipeline(
            name=self.name,
            steps=self.steps,
            enable_rollback=self.enable_rollback
        )
        for handler in self._event_handlers:
            pipeline.attach_event_handler(handler)
        return pipeline

    @classmethod
    def standard_deployment(cls) -> DeploymentPipeline:
        """
        Build the standard ECS deployment pipeline.

        This includes all standard steps for a full deployment:
        - Initialize deployment tracking
        - Preprocess compose file
        - ECR authentication and repository creation
        - Image building and pushing
        - EFS setup for named volumes
        - Task definition conversion and registration
        - Service creation/update
        - Stability waiting
        - Finalization

        Returns:
            Configured DeploymentPipeline
        """
        from .steps import (
            InitializeDeploymentStep,
            PreprocessComposeStep,
            AuthenticateECRStep,
            CreateECRRepositoriesStep,
            BuildAndPushImagesStep,
            SetupEFSVolumesStep,
            ConvertToTaskDefinitionStep,
            RegisterTaskDefinitionStep,
            CreateOrUpdateServiceStep,
            WaitForStabilityStep,
            FinalizeDeploymentStep,
        )

        return (
            cls("ECS Full Deployment")
            .add_step(InitializeDeploymentStep())
            .add_step(PreprocessComposeStep())
            .add_step(AuthenticateECRStep())
            .add_step(CreateECRRepositoriesStep())
            .add_step(BuildAndPushImagesStep())
            .add_step(SetupEFSVolumesStep())
            .add_step(ConvertToTaskDefinitionStep())
            .add_step(RegisterTaskDefinitionStep())
            .add_step(CreateOrUpdateServiceStep())
            .add_step(WaitForStabilityStep())
            .add_step(FinalizeDeploymentStep())
            .with_rollback(True)
            .build()
        )

    @classmethod
    def infrastructure_provisioning(cls) -> DeploymentPipeline:
        """
        Build the infrastructure provisioning pipeline.

        Provisions all AWS infrastructure needed before deploying services:
        VPC -> Security Groups -> IAM -> Service Connect -> ALB -> Secrets -> EFS

        Run this once before the first multi-service deployment.

        Returns:
            Configured DeploymentPipeline
        """
        from .steps import (
            InitializeDeploymentStep,
            PreprocessComposeStep,
            LoadServiceConfigStep,
            ProvisionVPCStep,
            CreateSecurityGroupsStep,
            SetupIAMRolesStep,
            SetupServiceConnectStep,
            ProvisionSecretsStep,
            ProvisionALBStep,
            SetupEFSVolumesStep,
        )

        return (
            cls("Infrastructure Provisioning")
            .add_step(InitializeDeploymentStep())
            .add_step(PreprocessComposeStep())
            .add_step(LoadServiceConfigStep())
            .add_step(ProvisionVPCStep())
            .add_step(CreateSecurityGroupsStep())
            .add_step(SetupIAMRolesStep())
            .add_step(SetupServiceConnectStep())
            .add_step(ProvisionALBStep())
            .add_step(ProvisionSecretsStep())
            .add_step(SetupEFSVolumesStep())
            .with_rollback(True)
            .build()
        )

    @classmethod
    def multi_service_deployment(cls) -> DeploymentPipeline:
        """
        Build the multi-service ECS deployment pipeline.

        Deploys multiple compose services as individual ECS services
        with Service Connect for inter-service communication.

        Steps:
        Init -> Preprocess -> LoadConfig -> Order -> SharedImages ->
        ECR Auth -> ECR Repos -> Build -> Convert -> Register ->
        TargetGroups -> CreateServices -> WaitStable -> Finalize

        Returns:
            Configured DeploymentPipeline
        """
        from .steps import (
            InitializeDeploymentStep,
            PreprocessComposeStep,
            LoadServiceConfigStep,
            DetermineServiceOrderStep,
            DetectSharedImagesStep,
            AuthenticateECRStep,
            CreateECRRepositoriesStep,
            BuildAndPushImagesStep,
            SetupEFSVolumesStep,
            ConvertToTaskDefinitionsStep,
            RegisterTaskDefinitionsStep,
            CreateTargetGroupsStep,
            CreateOrUpdateMultiServiceStep,
            WaitForAllServicesStableStep,
            FinalizeMultiServiceDeploymentStep,
        )

        return (
            cls("Multi-Service ECS Deployment")
            .add_step(InitializeDeploymentStep())
            .add_step(PreprocessComposeStep())
            .add_step(LoadServiceConfigStep())
            .add_step(DetermineServiceOrderStep())
            .add_step(DetectSharedImagesStep())
            .add_step(AuthenticateECRStep())
            .add_step(CreateECRRepositoriesStep())
            .add_step(BuildAndPushImagesStep())
            .add_step(SetupEFSVolumesStep())
            .add_step(ConvertToTaskDefinitionsStep())
            .add_step(RegisterTaskDefinitionsStep())
            .add_step(CreateTargetGroupsStep())
            .add_step(CreateOrUpdateMultiServiceStep())
            .add_step(WaitForAllServicesStableStep())
            .add_step(FinalizeMultiServiceDeploymentStep())
            .with_rollback(True)
            .build()
        )

    @classmethod
    def minimal_deployment(cls) -> DeploymentPipeline:
        """
        Build a minimal deployment pipeline without image building.

        Use when images are already pushed to ECR or using public images.

        Returns:
            Configured DeploymentPipeline
        """
        from .steps import (
            InitializeDeploymentStep,
            PreprocessComposeStep,
            ConvertToTaskDefinitionStep,
            RegisterTaskDefinitionStep,
            CreateOrUpdateServiceStep,
            WaitForStabilityStep,
            FinalizeDeploymentStep,
        )

        return (
            cls("ECS Minimal Deployment")
            .add_step(InitializeDeploymentStep())
            .add_step(PreprocessComposeStep())
            .add_step(ConvertToTaskDefinitionStep())
            .add_step(RegisterTaskDefinitionStep())
            .add_step(CreateOrUpdateServiceStep())
            .add_step(WaitForStabilityStep())
            .add_step(FinalizeDeploymentStep())
            .with_rollback(True)
            .build()
        )
