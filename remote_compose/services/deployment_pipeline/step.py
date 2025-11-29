"""
Base classes for deployment pipeline steps.

Provides the foundational abstractions for building composable,
testable pipeline steps with built-in event emission and error handling.
"""

from abc import ABC, abstractmethod
from typing import Optional, Callable, List, Any
from dataclasses import dataclass

from .context import PipelineContext


@dataclass
class StepResult:
    """
    Result from executing a pipeline step.

    Provides a consistent way to communicate step outcomes
    including success/failure, messages, and error details.
    """

    success: bool
    message: str
    should_continue: bool = True
    error: Optional[Exception] = None
    data: Optional[Any] = None  # Optional data to pass to next step

    @classmethod
    def ok(cls, message: str, data: Any = None) -> 'StepResult':
        """Create a successful result."""
        return cls(success=True, message=message, should_continue=True, data=data)

    @classmethod
    def skip(cls, message: str) -> 'StepResult':
        """Create a result indicating step was skipped (still counts as success)."""
        return cls(success=True, message=f"Skipped: {message}", should_continue=True)

    @classmethod
    def fail(cls, message: str, error: Optional[Exception] = None) -> 'StepResult':
        """Create a failure result that stops the pipeline."""
        return cls(success=False, message=message, should_continue=False, error=error)

    @classmethod
    def stop(cls, message: str) -> 'StepResult':
        """Create a successful result that stops further execution."""
        return cls(success=True, message=message, should_continue=False)


class PipelineStep(ABC):
    """
    Abstract base class for a pipeline step.

    Each step:
    - Has a name for identification and logging
    - Can validate if it should run (conditional execution)
    - Executes its core logic
    - Can clean up resources if later steps fail
    - Emits events for observability

    Subclasses must implement execute(). Other methods are optional.
    """

    def __init__(self, name: Optional[str] = None):
        """
        Initialize the step.

        Args:
            name: Step name for identification. Defaults to class name.
        """
        self.name = name or self.__class__.__name__
        self._event_handlers: List[Callable] = []

    def attach_event_handler(self, handler: Callable) -> None:
        """
        Attach an event handler for step events.

        Args:
            handler: Callable that receives (event_type, **kwargs)
        """
        self._event_handlers.append(handler)

    def emit_event(self, event_type: str, **kwargs) -> None:
        """
        Emit an event to all attached handlers.

        Args:
            event_type: Type of event (e.g., 'step_started', 'step_completed')
            **kwargs: Event-specific data
        """
        for handler in self._event_handlers:
            try:
                handler(event_type, step=self.name, **kwargs)
            except Exception:
                # Don't let handler failures break the pipeline
                pass

    def should_run(self, context: PipelineContext) -> bool:
        """
        Determine if this step should run given the context.

        Override this to implement conditional step execution.
        Default is to always run.

        Args:
            context: Current pipeline context

        Returns:
            True if step should execute, False to skip
        """
        return True

    @abstractmethod
    def execute(self, context: PipelineContext) -> StepResult:
        """
        Execute the step's main logic.

        This is the core method that subclasses must implement.

        Args:
            context: Pipeline context with input and accumulated state

        Returns:
            StepResult indicating success/failure and whether to continue
        """
        pass

    def cleanup(self, context: PipelineContext) -> None:
        """
        Clean up resources created by this step.

        Called if a later step fails and rollback is needed.
        Override to implement cleanup logic. Default does nothing.

        Args:
            context: Pipeline context (may contain partial state)
        """
        pass

    def run(self, context: PipelineContext) -> StepResult:
        """
        Execute the step with event emission and error handling.

        This is the main entry point - don't override this.
        Override execute() instead.

        Args:
            context: Pipeline context

        Returns:
            StepResult from execution
        """
        self.emit_event('step_started', context=context)

        try:
            # Check if step should run
            if not self.should_run(context):
                result = StepResult.skip(f"{self.name} conditions not met")
                self.emit_event('step_skipped', result=result, context=context)
                return result

            # Execute the step
            result = self.execute(context)

            # Emit appropriate event
            if result.success:
                self.emit_event('step_completed', result=result, context=context)
            else:
                self.emit_event('step_failed', result=result, context=context)

            return result

        except Exception as e:
            result = StepResult.fail(
                message=f"{self.name} failed with exception: {str(e)}",
                error=e
            )
            self.emit_event('step_failed', result=result, context=context, error=e)
            return result

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.name}>"


class CompositeStep(PipelineStep):
    """
    A step composed of multiple sub-steps.

    Useful for grouping related steps together while maintaining
    single responsibility for each sub-step. Sub-steps execute
    sequentially and share the same context.
    """

    def __init__(self, name: str, steps: List[PipelineStep]):
        """
        Initialize composite step.

        Args:
            name: Name for this composite step
            steps: List of sub-steps to execute
        """
        super().__init__(name)
        self.steps = steps

    def attach_event_handler(self, handler: Callable) -> None:
        """Attach handler to this step and all sub-steps."""
        super().attach_event_handler(handler)
        for step in self.steps:
            step.attach_event_handler(handler)

    def execute(self, context: PipelineContext) -> StepResult:
        """Execute all sub-steps in sequence."""
        completed = []

        for step in self.steps:
            result = step.run(context)
            completed.append(step.name)

            if not result.success or not result.should_continue:
                return result

        return StepResult.ok(
            f"{self.name}: completed {len(completed)} sub-steps"
        )

    def cleanup(self, context: PipelineContext) -> None:
        """Clean up all sub-steps in reverse order."""
        for step in reversed(self.steps):
            try:
                step.cleanup(context)
            except Exception as e:
                self.emit_event('cleanup_error', step=step.name, error=e)


class ConditionalStep(PipelineStep):
    """
    A step that wraps another step with a condition.

    Executes the wrapped step only if the condition is met.
    """

    def __init__(
        self,
        step: PipelineStep,
        condition: Callable[[PipelineContext], bool],
        skip_message: str = "Condition not met"
    ):
        """
        Initialize conditional step.

        Args:
            step: The step to conditionally execute
            condition: Callable that returns True if step should run
            skip_message: Message to show when step is skipped
        """
        super().__init__(f"Conditional({step.name})")
        self.step = step
        self.condition = condition
        self.skip_message = skip_message

    def attach_event_handler(self, handler: Callable) -> None:
        """Attach handler to this step and wrapped step."""
        super().attach_event_handler(handler)
        self.step.attach_event_handler(handler)

    def should_run(self, context: PipelineContext) -> bool:
        """Check the condition."""
        return self.condition(context)

    def execute(self, context: PipelineContext) -> StepResult:
        """Execute the wrapped step."""
        return self.step.run(context)

    def cleanup(self, context: PipelineContext) -> None:
        """Delegate cleanup to wrapped step."""
        self.step.cleanup(context)
