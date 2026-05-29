"""
Service for managing deployment targets.
"""

from typing import Optional, List

from django.db.models import QuerySet
from django.utils import timezone

from ..models import DeploymentTarget, SecureCredential
from ..utils.ssh import SSHClient
from ..exceptions import ValidationError, SSHConnectionError
from .base import BaseService
from .credential_service import CredentialService


class TargetService(BaseService):
    """
    Service for managing deployment targets.
    """

    def __init__(
        self, credential_service: Optional[CredentialService] = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()

    def create_target(
        self,
        name: str,
        host: str,
        username: str = "ubuntu",
        port: int = 22,
        ssh_key: Optional[SecureCredential] = None,
        ssh_key_path: Optional[str] = None,
        ssh_key_name: Optional[str] = None,
        target_type: str = DeploymentTarget.TargetType.SSH,
        environment: str = DeploymentTarget.Environment.DEVELOPMENT,
        description: str = "",
        aws_instance_id: Optional[str] = None,
        aws_region: Optional[str] = None,
        validate_connection: bool = True,
        metadata: Optional[dict] = None,
    ) -> DeploymentTarget:
        """
        Create a new deployment target.

        Args:
            name: Unique name for the target
            host: Remote host address
            username: SSH username
            port: SSH port
            ssh_key: SecureCredential instance for SSH key
            ssh_key_path: Path to SSH key (will create credential)
            ssh_key_name: Name for auto-created SSH key credential
            target_type: Type of connection (ssh, tcp, unix)
            environment: Target environment (development, staging, production)
            description: Optional description
            aws_instance_id: AWS EC2 instance ID (if applicable)
            aws_region: AWS region (if applicable)
            validate_connection: Whether to validate SSH connection
            metadata: Additional metadata

        Returns:
            DeploymentTarget instance
        """
        # Handle SSH key
        if ssh_key_path and not ssh_key:
            # Create credential from path
            key_name = ssh_key_name or f"{name}-ssh-key"
            ssh_key = self.credential_service.create_ssh_key(
                name=key_name,
                key_path=ssh_key_path,
                description=f"SSH key for target: {name}",
            )

        # Validate connection before saving
        if validate_connection and target_type == DeploymentTarget.TargetType.SSH:
            key_content = None
            if ssh_key:
                key_content = self.credential_service.get_ssh_key_content(ssh_key)

            success, message = self._test_ssh_connection(
                host=host,
                port=port,
                username=username,
                key_content=key_content,
            )
            if not success:
                raise SSHConnectionError(
                    f"Connection validation failed: {message}", host=host, port=port
                )

        target = DeploymentTarget.objects.create(
            name=name,
            host=host,
            port=port,
            username=username,
            ssh_key=ssh_key,
            target_type=target_type,
            environment=environment,
            description=description,
            aws_instance_id=aws_instance_id,
            aws_region=aws_region,
            metadata=metadata or {},
            health_status=(
                DeploymentTarget.HealthStatus.HEALTHY
                if validate_connection
                else DeploymentTarget.HealthStatus.UNKNOWN
            ),
            last_health_check=timezone.now() if validate_connection else None,
        )

        self.log_info(f"Created deployment target: {name}")
        self.notify_observers("target_created", target=target)

        return target

    def get_target(self, target_id: int) -> DeploymentTarget:
        """Get a target by ID."""
        try:
            return DeploymentTarget.objects.get(id=target_id)
        except DeploymentTarget.DoesNotExist:
            raise ValidationError(f"Target not found: {target_id}")

    def get_target_by_name(self, name: str) -> DeploymentTarget:
        """Get a target by name."""
        try:
            return DeploymentTarget.objects.get(name=name)
        except DeploymentTarget.DoesNotExist:
            raise ValidationError(f"Target not found: {name}")

    def update_target(self, target: DeploymentTarget, **kwargs) -> DeploymentTarget:
        """
        Update a deployment target.

        Args:
            target: DeploymentTarget instance
            **kwargs: Fields to update

        Returns:
            Updated DeploymentTarget instance
        """
        allowed_fields = [
            "name",
            "host",
            "port",
            "username",
            "ssh_key",
            "target_type",
            "environment",
            "description",
            "aws_instance_id",
            "aws_region",
            "is_active",
            "metadata",
        ]

        for field, value in kwargs.items():
            if field in allowed_fields:
                setattr(target, field, value)

        target.save()

        self.log_info(f"Updated target: {target.name}")
        self.notify_observers("target_updated", target=target)

        return target

    def delete_target(self, target: DeploymentTarget, force: bool = False) -> bool:
        """
        Delete a deployment target.

        Args:
            target: DeploymentTarget instance
            force: Force deletion even with associated deployments

        Returns:
            True if deleted successfully
        """
        # Check for active deployments
        active_statuses = ["pending", "running"]
        if target.deployments.filter(status__in=active_statuses).exists():
            if not force:
                raise ValidationError(
                    f"Target {target.name} has active deployments. "
                    "Use force=True to delete anyway."
                )

        name = target.name
        target.delete()

        self.log_info(f"Deleted target: {name}")
        self.notify_observers("target_deleted", target_name=name)

        return True

    def list_targets(
        self,
        environment: Optional[str] = None,
        is_active: Optional[bool] = None,
        target_type: Optional[str] = None,
    ) -> QuerySet:
        """
        List deployment targets with optional filters.

        Args:
            environment: Filter by environment
            is_active: Filter by active status
            target_type: Filter by target type

        Returns:
            QuerySet of DeploymentTarget instances
        """
        qs = DeploymentTarget.objects.all()

        if environment:
            qs = qs.filter(environment=environment)
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if target_type:
            qs = qs.filter(target_type=target_type)

        return qs

    def test_connection(self, target: DeploymentTarget) -> dict:
        """
        Test SSH connection to a target.

        Args:
            target: DeploymentTarget instance

        Returns:
            Dict with success status and message
        """
        key_content = None
        if target.ssh_key:
            key_content = self.credential_service.get_ssh_key_content(target.ssh_key)

        success, message = self._test_ssh_connection(
            host=target.host,
            port=target.port,
            username=target.username,
            key_content=key_content,
        )

        # Update health status
        if success:
            target.mark_healthy()
        else:
            target.mark_unhealthy()

        self.notify_observers(
            "target_health_checked", target=target, success=success, message=message
        )

        return {
            "success": success,
            "message": message,
            "health_status": target.health_status,
        }

    def check_health(self, target: DeploymentTarget) -> dict:
        """Alias for test_connection."""
        return self.test_connection(target)

    def check_all_health(self) -> List[dict]:
        """
        Check health of all active targets.

        Returns:
            List of health check results
        """
        results = []
        for target in self.list_targets(is_active=True):
            result = self.test_connection(target)
            result["target_name"] = target.name
            results.append(result)
        return results

    def get_ssh_client(self, target: DeploymentTarget) -> SSHClient:
        """
        Get an SSH client configured for a target.

        Args:
            target: DeploymentTarget instance

        Returns:
            SSHClient instance (not connected)
        """
        key_content = None
        if target.ssh_key:
            key_content = self.credential_service.get_ssh_key_content(target.ssh_key)

        return SSHClient(
            host=target.host,
            port=target.port,
            username=target.username,
            key_content=key_content,
        )

    def _test_ssh_connection(
        self,
        host: str,
        port: int,
        username: str,
        key_content: Optional[str] = None,
    ) -> tuple:
        """Test SSH connection to a host."""
        try:
            client = SSHClient(
                host=host,
                port=port,
                username=username,
                key_content=key_content,
            )
            return client.test_connection()
        except Exception as e:
            return False, str(e)
