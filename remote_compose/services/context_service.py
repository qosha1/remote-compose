"""
Service for managing Docker contexts.
"""

import subprocess
from typing import Optional

from django.db.models import QuerySet

from ..models import DockerContext, DeploymentTarget
from ..conf import get_setting
from ..exceptions import DockerContextError, ValidationError
from .base import BaseService
from .target_service import TargetService


class ContextService(BaseService):
    """
    Service for managing Docker contexts.
    """

    def __init__(self, target_service: Optional[TargetService] = None, **kwargs):
        super().__init__(**kwargs)
        self.target_service = target_service or TargetService()
        self.docker_command = get_setting("DOCKER_COMMAND", "docker")

    def create_context(
        self,
        name: str,
        target: DeploymentTarget,
        description: str = "",
        is_default: bool = False,
        sync_to_docker: bool = True,
    ) -> DockerContext:
        """
        Create a new Docker context for a deployment target.

        Args:
            name: Unique name for the context
            target: DeploymentTarget instance
            description: Optional description
            is_default: Whether this should be the default context
            sync_to_docker: Whether to create the context in Docker daemon

        Returns:
            DockerContext instance
        """
        # Build endpoint based on target type
        if target.target_type == DeploymentTarget.TargetType.SSH:
            endpoint = f"ssh://{target.username}@{target.host}:{target.port}"
            context_type = DockerContext.ContextType.SSH
        elif target.target_type == DeploymentTarget.TargetType.TCP:
            endpoint = f"tcp://{target.host}:{target.port}"
            context_type = DockerContext.ContextType.TCP
        else:
            endpoint = f"unix://{target.host}"
            context_type = DockerContext.ContextType.UNIX

        # Create context in database
        context = DockerContext.objects.create(
            name=name,
            target=target,
            context_type=context_type,
            endpoint=endpoint,
            description=description or f"Context for {target.name}",
            is_default=is_default,
            is_synced=False,
        )

        # Sync to Docker daemon if requested
        if sync_to_docker:
            try:
                self._create_docker_context(context)
                context.is_synced = True
                context.save(update_fields=["is_synced", "updated_at"])
            except Exception as e:
                self.log_warning(f"Failed to sync context to Docker: {e}")
                # Context is still usable, just not synced to Docker daemon

        self.log_info(f"Created Docker context: {name}")
        self.notify_observers("context_created", context=context)

        return context

    def get_or_create_context(
        self,
        target: DeploymentTarget,
        context_name: Optional[str] = None,
    ) -> DockerContext:
        """
        Get existing context for target or create a new one.

        Args:
            target: DeploymentTarget instance
            context_name: Optional name for new context

        Returns:
            DockerContext instance
        """
        # Try to find existing context for this target
        existing = DockerContext.objects.filter(target=target).first()
        if existing:
            return existing

        # Create new context
        name = context_name or f"{target.name}-context"
        return self.create_context(name=name, target=target)

    def get_context(self, context_id: int) -> DockerContext:
        """Get a context by ID."""
        try:
            return DockerContext.objects.get(id=context_id)
        except DockerContext.DoesNotExist:
            raise ValidationError(f"Context not found: {context_id}")

    def get_context_by_name(self, name: str) -> DockerContext:
        """Get a context by name."""
        try:
            return DockerContext.objects.get(name=name)
        except DockerContext.DoesNotExist:
            raise ValidationError(f"Context not found: {name}")

    def update_context(self, context: DockerContext, **kwargs) -> DockerContext:
        """
        Update a Docker context.

        Args:
            context: DockerContext instance
            **kwargs: Fields to update

        Returns:
            Updated DockerContext instance
        """
        allowed_fields = ["name", "description", "is_default", "metadata"]

        for field, value in kwargs.items():
            if field in allowed_fields:
                setattr(context, field, value)

        context.save()

        self.log_info(f"Updated context: {context.name}")
        self.notify_observers("context_updated", context=context)

        return context

    def delete_context(
        self,
        context: DockerContext,
        remove_from_docker: bool = True,
        force: bool = False,
    ) -> bool:
        """
        Delete a Docker context.

        Args:
            context: DockerContext instance
            remove_from_docker: Whether to remove from Docker daemon
            force: Force deletion even with active deployments

        Returns:
            True if deleted successfully
        """
        # Check for active deployments
        active_statuses = ["pending", "running"]
        if context.deployments.filter(status__in=active_statuses).exists():
            if not force:
                raise ValidationError(
                    f"Context {context.name} has active deployments. "
                    "Use force=True to delete anyway."
                )

        # Remove from Docker daemon
        if remove_from_docker and context.is_synced:
            try:
                self._delete_docker_context(context.name)
            except Exception as e:
                self.log_warning(f"Failed to remove context from Docker: {e}")

        name = context.name
        context.delete()

        self.log_info(f"Deleted context: {name}")
        self.notify_observers("context_deleted", context_name=name)

        return True

    def list_contexts(
        self,
        target: Optional[DeploymentTarget] = None,
        is_synced: Optional[bool] = None,
    ) -> QuerySet:
        """
        List Docker contexts with optional filters.

        Args:
            target: Filter by target
            is_synced: Filter by sync status

        Returns:
            QuerySet of DockerContext instances
        """
        qs = DockerContext.objects.select_related("target").all()

        if target:
            qs = qs.filter(target=target)
        if is_synced is not None:
            qs = qs.filter(is_synced=is_synced)

        return qs

    def set_default(self, context: DockerContext) -> DockerContext:
        """
        Set a context as the default.

        Args:
            context: DockerContext instance

        Returns:
            Updated DockerContext instance
        """
        context.is_default = True
        context.save()

        self.log_info(f"Set default context: {context.name}")
        self.notify_observers("context_set_default", context=context)

        return context

    def test_context(self, context: DockerContext) -> dict:
        """
        Test a Docker context connection.

        Args:
            context: DockerContext instance

        Returns:
            Dict with success status and message
        """
        # Test underlying target connection
        result = self.target_service.test_connection(context.target)

        if result["success"]:
            # Also test docker command through context
            try:
                docker_result = self._test_docker_context(context)
                result["docker_accessible"] = docker_result["success"]
                if not docker_result["success"]:
                    result["docker_message"] = docker_result["message"]
            except Exception as e:
                result["docker_accessible"] = False
                result["docker_message"] = str(e)

        return result

    def sync_context(self, context: DockerContext) -> bool:
        """
        Sync a context to the local Docker daemon.

        Args:
            context: DockerContext instance

        Returns:
            True if synced successfully
        """
        try:
            if context.is_synced:
                # Update existing context
                self._delete_docker_context(context.name)

            self._create_docker_context(context)
            context.is_synced = True
            context.save(update_fields=["is_synced", "updated_at"])

            self.log_info(f"Synced context to Docker: {context.name}")
            return True

        except Exception as e:
            raise DockerContextError(f"Failed to sync context: {e}")

    def list_docker_contexts(self) -> list:
        """
        List contexts registered with Docker daemon.

        Returns:
            List of Docker context dictionaries
        """
        try:
            result = subprocess.run(
                [self.docker_command, "context", "ls", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise DockerContextError(
                    f"Failed to list Docker contexts: {result.stderr}"
                )

            import json

            contexts = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    contexts.append(json.loads(line))

            return contexts

        except subprocess.TimeoutExpired:
            raise DockerContextError("Docker command timed out")
        except Exception as e:
            raise DockerContextError(f"Failed to list Docker contexts: {e}")

    def _create_docker_context(self, context: DockerContext):
        """Create context in Docker daemon."""
        cmd = [
            self.docker_command,
            "context",
            "create",
            context.name,
            "--docker",
            f"host={context.endpoint}",
        ]

        if context.description:
            cmd.extend(["--description", context.description])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            # Check if context already exists
            if "already exists" in result.stderr.lower():
                self.log_warning(f"Context {context.name} already exists in Docker")
                return

            raise DockerContextError(
                f"Failed to create Docker context: {result.stderr}"
            )

    def _delete_docker_context(self, context_name: str):
        """Delete context from Docker daemon."""
        result = subprocess.run(
            [self.docker_command, "context", "rm", context_name],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            # Ignore if context doesn't exist
            if "not found" not in result.stderr.lower():
                raise DockerContextError(
                    f"Failed to delete Docker context: {result.stderr}"
                )

    def _test_docker_context(self, context: DockerContext) -> dict:
        """Test Docker context by running docker info."""
        try:
            result = subprocess.run(
                [
                    self.docker_command,
                    "--context",
                    context.name,
                    "info",
                    "--format",
                    "{{.ServerVersion}}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Docker version: {result.stdout.strip()}",
                }
            else:
                return {
                    "success": False,
                    "message": result.stderr.strip(),
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Docker command timed out",
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
            }
