"""
Service for AWS Security Group provisioning for ECS deployments.

Provides functionality for creating and configuring security groups
for ALB, ECS tasks, database, cache, and EFS access.
"""

from typing import Optional, Dict

from botocore.exceptions import ClientError

from ..models import SecurityGroupConfig
from ..exceptions import SecurityGroupProvisioningError
from .base import BaseService
from .aws_client_factory import AWSClientFactory, get_aws_client_factory


class SecurityGroupService(BaseService):
    """
    Service for provisioning security groups for ECS cluster infrastructure.

    Creates and configures five purpose-specific security groups with
    appropriate inbound/outbound rules for a multi-service ECS deployment.
    """

    # Security group purposes matching the model choices
    PURPOSES = ["alb", "ecs_tasks", "database", "cache", "efs"]

    def __init__(self, aws_factory: Optional[AWSClientFactory] = None, **kwargs):
        super().__init__(**kwargs)
        self.aws_factory = aws_factory or get_aws_client_factory()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def provision_security_groups(
        self,
        cluster,
        vpc_id: str,
        region: Optional[str] = None,
        credential=None,
    ) -> Dict[str, str]:
        """
        Create all security groups for the ECS cluster.

        Creates five security groups (ALB, ECS Tasks, Database, Cache, EFS)
        and configures their inbound/outbound rules based on inter-group
        dependencies.

        Args:
            cluster: ECSCluster model instance.
            vpc_id: VPC ID where security groups will be created.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            Dict mapping purpose to security group ID, e.g.
            {'alb': 'sg-abc123', 'ecs_tasks': 'sg-def456', ...}

        Raises:
            SecurityGroupProvisioningError: If creation or configuration fails.
        """
        cluster_name = cluster.name
        sg_ids: Dict[str, str] = {}

        # Phase 1: Create all security groups
        for purpose in self.PURPOSES:
            sg_name = f"{cluster_name}-{purpose.replace('_', '-')}-sg"
            description = self._get_description(purpose, cluster_name)

            sg_id = self._create_or_get_sg(
                name=sg_name,
                description=description,
                vpc_id=vpc_id,
                purpose=purpose,
                cluster=cluster,
                region=region,
                credential=credential,
            )
            sg_ids[purpose] = sg_id

        # Phase 2: Configure rules (requires all SG IDs to set cross-references)
        for purpose in self.PURPOSES:
            self._configure_rules(
                sg_id=sg_ids[purpose],
                purpose=purpose,
                sg_ids_map=sg_ids,
                region=region,
                credential=credential,
            )

        self.notify_observers(
            "security_groups_provisioned",
            cluster_name=cluster_name,
            sg_ids=sg_ids,
        )

        self.log_info(
            f"Provisioned {len(sg_ids)} security groups for cluster {cluster_name}"
        )
        return sg_ids

    # remote-compose-jzp: bounded retries on the find/create race so a
    # broken find path can't recurse to stack overflow.
    _MAX_DUPLICATE_RETRIES = 3

    def _create_or_get_sg(
        self,
        name: str,
        description: str,
        vpc_id: str,
        purpose: str,
        cluster,
        region: Optional[str] = None,
        credential=None,
    ) -> str:
        """
        Create a security group if it does not already exist.

        Uses the Name tag to check for an existing security group. When
        create races against another caller and AWS returns
        ``InvalidGroup.Duplicate``, the find/create loop retries up to
        ``_MAX_DUPLICATE_RETRIES`` times — bounded so a misbehaving find
        path can't recurse forever.

        Args:
            name: Security group name.
            description: Security group description.
            vpc_id: VPC ID.
            purpose: Purpose identifier (alb, ecs_tasks, etc.).
            cluster: ECSCluster model instance.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            Security group ID.

        Raises:
            SecurityGroupProvisioningError: If creation fails.
        """
        ec2 = self.aws_factory.get_client("ec2", region=region, credential=credential)

        last_duplicate_error: Optional[ClientError] = None
        for attempt in range(self._MAX_DUPLICATE_RETRIES):
            # Check for existing SG by name in this VPC.
            try:
                response = ec2.describe_security_groups(
                    Filters=[
                        {"Name": "vpc-id", "Values": [vpc_id]},
                        {"Name": "tag:Name", "Values": [name]},
                        {"Name": "tag:remote-compose:managed", "Values": ["true"]},
                    ]
                )
                existing = response.get("SecurityGroups", [])
                if existing:
                    sg_id = existing[0]["GroupId"]
                    self.log_info(f"Found existing security group {name}: {sg_id}")
                    SecurityGroupConfig.objects.get_or_create(
                        cluster=cluster,
                        purpose=purpose,
                        defaults={
                            "security_group_id": sg_id,
                            "vpc_id": vpc_id,
                        },
                    )
                    return sg_id
            except ClientError as e:
                self.log_warning(f"Error searching for security group {name}: {e}")

            # Try to create.
            try:
                response = ec2.create_security_group(
                    GroupName=name,
                    Description=description,
                    VpcId=vpc_id,
                    TagSpecifications=[
                        {
                            "ResourceType": "security-group",
                            "Tags": [
                                {"Key": "Name", "Value": name},
                                {
                                    "Key": "remote-compose:cluster",
                                    "Value": cluster.name,
                                },
                                {"Key": "remote-compose:managed", "Value": "true"},
                                {"Key": "remote-compose:purpose", "Value": purpose},
                            ],
                        }
                    ],
                )
                sg_id = response["GroupId"]
                self.log_info(f"Created security group {name}: {sg_id}")
                SecurityGroupConfig.objects.update_or_create(
                    cluster=cluster,
                    purpose=purpose,
                    defaults={
                        "security_group_id": sg_id,
                        "vpc_id": vpc_id,
                    },
                )
                return sg_id
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code != "InvalidGroup.Duplicate":
                    raise SecurityGroupProvisioningError(
                        f"Failed to create security group {name}: {e}",
                        security_group_id=None,
                    )
                # Lost a create race — find should pick it up next pass.
                last_duplicate_error = e
                continue

        raise SecurityGroupProvisioningError(
            f"Failed to create security group {name} after "
            f"{self._MAX_DUPLICATE_RETRIES} retries — find/create kept "
            f"racing without converging. Last error: "
            f"{last_duplicate_error}",
            security_group_id=None,
        )

    def _configure_rules(
        self,
        sg_id: str,
        purpose: str,
        sg_ids_map: Dict[str, str],
        region: Optional[str] = None,
        credential=None,
    ) -> None:
        """
        Add inbound and outbound rules to a security group based on its purpose.

        Rule definitions:
        - ALB: inbound 80/443 from 0.0.0.0/0, outbound all
        - ECS Tasks: inbound all from ALB SG, outbound all
        - Database: inbound 5432 from ECS Tasks SG
        - Cache: inbound 6379 from ECS Tasks SG
        - EFS: inbound 2049 from ECS Tasks SG

        Args:
            sg_id: Security group ID to configure.
            purpose: Purpose identifier.
            sg_ids_map: Mapping of purpose to SG ID for cross-references.
            region: AWS region override.
            credential: SecureCredential for AWS access.
        """
        ec2 = self.aws_factory.get_client("ec2", region=region, credential=credential)

        inbound_rules = []
        outbound_rules = []

        if purpose == "alb":
            inbound_rules = [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": "HTTP from anywhere"}
                    ],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": "HTTPS from anywhere"}
                    ],
                },
            ]
            outbound_rules = [
                {
                    "IpProtocol": "-1",
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": "All outbound traffic"}
                    ],
                },
            ]

        elif purpose == "ecs_tasks":
            alb_sg = sg_ids_map.get("alb")
            inbound_rules = [
                {
                    "IpProtocol": "-1",
                    "UserIdGroupPairs": [
                        {"GroupId": alb_sg, "Description": "All traffic from ALB"}
                    ],
                },
            ]
            outbound_rules = [
                {
                    "IpProtocol": "-1",
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": "All outbound traffic"}
                    ],
                },
            ]

        elif purpose == "database":
            ecs_sg = sg_ids_map.get("ecs_tasks")
            inbound_rules = [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 5432,
                    "ToPort": 5432,
                    "UserIdGroupPairs": [
                        {"GroupId": ecs_sg, "Description": "PostgreSQL from ECS tasks"}
                    ],
                },
            ]

        elif purpose == "cache":
            ecs_sg = sg_ids_map.get("ecs_tasks")
            inbound_rules = [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6379,
                    "ToPort": 6379,
                    "UserIdGroupPairs": [
                        {"GroupId": ecs_sg, "Description": "Redis from ECS tasks"}
                    ],
                },
            ]

        elif purpose == "efs":
            ecs_sg = sg_ids_map.get("ecs_tasks")
            inbound_rules = [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "UserIdGroupPairs": [
                        {"GroupId": ecs_sg, "Description": "NFS from ECS tasks"}
                    ],
                },
            ]

        # Apply inbound rules
        if inbound_rules:
            try:
                ec2.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=inbound_rules,
                )
            except ClientError as e:
                if "InvalidPermission.Duplicate" not in str(e):
                    raise SecurityGroupProvisioningError(
                        f"Failed to configure inbound rules for {purpose} SG {sg_id}: {e}",
                        security_group_id=sg_id,
                    )
                self.log_debug(f"Inbound rules already exist for {purpose} SG {sg_id}")

        # Apply outbound rules (only if non-default)
        if outbound_rules:
            try:
                ec2.authorize_security_group_egress(
                    GroupId=sg_id,
                    IpPermissions=outbound_rules,
                )
            except ClientError as e:
                if "InvalidPermission.Duplicate" not in str(e):
                    raise SecurityGroupProvisioningError(
                        f"Failed to configure outbound rules for {purpose} SG {sg_id}: {e}",
                        security_group_id=sg_id,
                    )
                self.log_debug(f"Outbound rules already exist for {purpose} SG {sg_id}")

        # Update database record with rule details
        try:
            sg_config = SecurityGroupConfig.objects.get(security_group_id=sg_id)
            sg_config.inbound_rules = [self._serialize_rule(r) for r in inbound_rules]
            sg_config.outbound_rules = [self._serialize_rule(r) for r in outbound_rules]
            sg_config.save(update_fields=["inbound_rules", "outbound_rules"])
        except SecurityGroupConfig.DoesNotExist:
            self.log_warning(f"No database record found for SG {sg_id}")

    def _get_description(self, purpose: str, cluster_name: str) -> str:
        """Return a human-readable description for a security group purpose."""
        descriptions = {
            "alb": f"Application Load Balancer for {cluster_name}",
            "ecs_tasks": f"ECS Tasks for {cluster_name}",
            "database": f"Database (PostgreSQL) access for {cluster_name}",
            "cache": f"Cache (Redis) access for {cluster_name}",
            "efs": f"EFS (NFS) access for {cluster_name}",
        }
        return descriptions.get(purpose, f"{purpose} for {cluster_name}")

    def _serialize_rule(self, rule: Dict) -> Dict:
        """Serialize a rule dict for JSON storage."""
        # The rule dicts are already JSON-serializable
        return dict(rule)
