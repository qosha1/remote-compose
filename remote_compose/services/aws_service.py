"""
Service for AWS EC2 integration.
"""

from typing import Optional, List, Dict

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from ..models import DeploymentTarget, SecureCredential
from ..conf import get_setting
from ..exceptions import AWSError, EC2Error, AWSCredentialError
from .base import BaseService
from .credential_service import CredentialService
from .target_service import TargetService


class AWSService(BaseService):
    """
    Service for AWS EC2 instance discovery and management.
    """

    def __init__(
        self,
        credential_service: Optional[CredentialService] = None,
        target_service: Optional[TargetService] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.target_service = target_service or TargetService(
            credential_service=self.credential_service
        )
        self.default_region = get_setting("AWS_DEFAULT_REGION", "us-east-1")

    def _get_ec2_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """
        Get an EC2 client with optional credentials.

        Args:
            region: AWS region
            credential: Optional SecureCredential with AWS keys

        Returns:
            boto3 EC2 client
        """
        region = region or self.default_region

        try:
            if (
                credential
                and credential.credential_type
                == SecureCredential.CredentialType.AWS_ACCESS_KEY
            ):
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    "ec2",
                    region_name=region,
                    aws_access_key_id=aws_creds["access_key_id"],
                    aws_secret_access_key=aws_creds["secret_access_key"],
                )
            else:
                # Use default credential chain
                return boto3.client("ec2", region_name=region)

        except NoCredentialsError:
            raise AWSCredentialError(
                "No AWS credentials found. Configure credentials via environment variables, "
                "AWS credentials file, or create an AWS credential in remote_compose."
            )
        except Exception as e:
            raise AWSError(f"Failed to create EC2 client: {e}")

    def list_instances(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        filters: Optional[Dict[str, str]] = None,
        instance_ids: Optional[List[str]] = None,
    ) -> List[dict]:
        """
        List EC2 instances with optional filters.

        Args:
            region: AWS region
            credential: Optional AWS credential
            filters: Dict of filter key-value pairs (e.g., {'tag:Environment': 'production'})
            instance_ids: Optional list of specific instance IDs

        Returns:
            List of instance dictionaries
        """
        client = self._get_ec2_client(region, credential)

        try:
            # Build filters
            ec2_filters = []
            if filters:
                for key, value in filters.items():
                    if key.startswith("tag:"):
                        ec2_filters.append(
                            {
                                "Name": key,
                                "Values": [value] if isinstance(value, str) else value,
                            }
                        )
                    elif key == "state":
                        ec2_filters.append(
                            {
                                "Name": "instance-state-name",
                                "Values": [value] if isinstance(value, str) else value,
                            }
                        )
                    else:
                        ec2_filters.append(
                            {
                                "Name": key,
                                "Values": [value] if isinstance(value, str) else value,
                            }
                        )

            # Make API call
            kwargs = {}
            if ec2_filters:
                kwargs["Filters"] = ec2_filters
            if instance_ids:
                kwargs["InstanceIds"] = instance_ids

            response = client.describe_instances(**kwargs)

            # Parse response
            instances = []
            for reservation in response.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instances.append(self._parse_instance(instance, region))

            self.log_debug(f"Found {len(instances)} instances in {region}")
            return instances

        except ClientError as e:
            raise EC2Error(f"Failed to list EC2 instances: {e}")

    def get_instance(
        self,
        instance_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> dict:
        """
        Get details for a specific EC2 instance.

        Args:
            instance_id: EC2 instance ID
            region: AWS region
            credential: Optional AWS credential

        Returns:
            Instance dictionary
        """
        instances = self.list_instances(
            region=region,
            credential=credential,
            instance_ids=[instance_id],
        )

        if not instances:
            raise EC2Error(f"Instance not found: {instance_id}")

        return instances[0]

    def discover_instances(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        tag_filters: Optional[Dict[str, str]] = None,
        running_only: bool = True,
    ) -> List[dict]:
        """
        Discover EC2 instances suitable for Docker deployment.

        Args:
            region: AWS region
            credential: Optional AWS credential
            tag_filters: Optional tag filters
            running_only: Only return running instances

        Returns:
            List of instance dictionaries
        """
        filters = tag_filters or {}

        if running_only:
            filters["state"] = "running"

        return self.list_instances(
            region=region,
            credential=credential,
            filters=filters,
        )

    def create_target_from_instance(
        self,
        instance_id: str,
        target_name: str,
        ssh_key: Optional[SecureCredential] = None,
        ssh_key_path: Optional[str] = None,
        ssh_username: str = "ubuntu",
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        environment: str = "development",
        validate_connection: bool = True,
    ) -> DeploymentTarget:
        """
        Create a deployment target from an EC2 instance.

        Args:
            instance_id: EC2 instance ID
            target_name: Name for the deployment target
            ssh_key: SecureCredential with SSH key
            ssh_key_path: Path to SSH key file (alternative)
            ssh_username: SSH username
            region: AWS region
            credential: AWS credential
            environment: Target environment
            validate_connection: Whether to validate SSH connection

        Returns:
            DeploymentTarget instance
        """
        instance = self.get_instance(
            instance_id=instance_id,
            region=region,
            credential=credential,
        )

        if instance["state"] != "running":
            raise EC2Error(
                f"Instance {instance_id} is not running (state: {instance['state']})"
            )

        # Determine host address
        host = instance.get("public_ip") or instance.get("public_dns")
        if not host:
            host = instance.get("private_ip")
            if not host:
                raise EC2Error(f"Instance {instance_id} has no accessible IP address")

        # Map environment string
        env_map = {
            "development": DeploymentTarget.Environment.DEVELOPMENT,
            "staging": DeploymentTarget.Environment.STAGING,
            "production": DeploymentTarget.Environment.PRODUCTION,
        }

        target = self.target_service.create_target(
            name=target_name,
            host=host,
            username=ssh_username,
            port=22,
            ssh_key=ssh_key,
            ssh_key_path=ssh_key_path,
            environment=env_map.get(
                environment, DeploymentTarget.Environment.DEVELOPMENT
            ),
            aws_instance_id=instance_id,
            aws_region=region or self.default_region,
            validate_connection=validate_connection,
            metadata={
                "aws_instance_type": instance.get("instance_type"),
                "aws_key_name": instance.get("key_name"),
                "aws_tags": instance.get("tags", {}),
            },
        )

        self.log_info(f"Created target from EC2 instance: {target.name}")
        self.notify_observers(
            "target_created_from_ec2", target=target, instance=instance
        )

        return target

    def sync_target_ip(
        self,
        target: DeploymentTarget,
        credential: Optional[SecureCredential] = None,
    ) -> DeploymentTarget:
        """
        Sync target IP address with EC2 instance.

        Useful when instance IP changes (e.g., after restart).

        Args:
            target: DeploymentTarget with aws_instance_id
            credential: Optional AWS credential

        Returns:
            Updated DeploymentTarget
        """
        if not target.aws_instance_id:
            raise EC2Error(f"Target {target.name} is not linked to an EC2 instance")

        instance = self.get_instance(
            instance_id=target.aws_instance_id,
            region=target.aws_region,
            credential=credential,
        )

        new_host = (
            instance.get("public_ip")
            or instance.get("public_dns")
            or instance.get("private_ip")
        )

        if new_host and new_host != target.host:
            old_host = target.host
            target.host = new_host
            target.save(update_fields=["host", "updated_at"])

            self.log_info(f"Updated target {target.name} IP: {old_host} -> {new_host}")
            self.notify_observers("target_ip_updated", target=target, old_host=old_host)

        return target

    def start_instance(
        self,
        instance_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        wait: bool = True,
    ) -> dict:
        """
        Start an EC2 instance.

        Args:
            instance_id: EC2 instance ID
            region: AWS region
            credential: Optional AWS credential
            wait: Wait for instance to be running

        Returns:
            Instance dictionary
        """
        client = self._get_ec2_client(region, credential)

        try:
            client.start_instances(InstanceIds=[instance_id])

            self.log_info(f"Starting instance: {instance_id}")

            if wait:
                waiter = client.get_waiter("instance_running")
                waiter.wait(InstanceIds=[instance_id])
                self.log_info(f"Instance {instance_id} is now running")

            return self.get_instance(instance_id, region, credential)

        except ClientError as e:
            raise EC2Error(f"Failed to start instance: {e}")

    def stop_instance(
        self,
        instance_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        wait: bool = True,
    ) -> dict:
        """
        Stop an EC2 instance.

        Args:
            instance_id: EC2 instance ID
            region: AWS region
            credential: Optional AWS credential
            wait: Wait for instance to be stopped

        Returns:
            Instance dictionary
        """
        client = self._get_ec2_client(region, credential)

        try:
            client.stop_instances(InstanceIds=[instance_id])

            self.log_info(f"Stopping instance: {instance_id}")

            if wait:
                waiter = client.get_waiter("instance_stopped")
                waiter.wait(InstanceIds=[instance_id])
                self.log_info(f"Instance {instance_id} is now stopped")

            return self.get_instance(instance_id, region, credential)

        except ClientError as e:
            raise EC2Error(f"Failed to stop instance: {e}")

    def _parse_instance(self, instance: dict, region: str) -> dict:
        """Parse EC2 instance response into a simpler dictionary."""
        tags = {}
        for tag in instance.get("Tags", []):
            tags[tag["Key"]] = tag["Value"]

        return {
            "instance_id": instance["InstanceId"],
            "instance_type": instance.get("InstanceType"),
            "state": instance.get("State", {}).get("Name"),
            "public_ip": instance.get("PublicIpAddress"),
            "private_ip": instance.get("PrivateIpAddress"),
            "public_dns": instance.get("PublicDnsName"),
            "private_dns": instance.get("PrivateDnsName"),
            "key_name": instance.get("KeyName"),
            "launch_time": instance.get("LaunchTime"),
            "region": region,
            "tags": tags,
            "name": tags.get("Name", ""),
            "security_groups": [
                sg["GroupId"] for sg in instance.get("SecurityGroups", [])
            ],
        }
