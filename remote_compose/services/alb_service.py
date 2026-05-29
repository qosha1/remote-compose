"""
Service for AWS Application Load Balancer provisioning for ECS deployments.

Provides functionality for creating and managing ALBs, target groups,
and listener rules for routing traffic to ECS services.
"""

import time
from typing import Optional, Dict, List, Any

from botocore.exceptions import ClientError

from ..models import LoadBalancerConfig, TargetGroupConfig
from ..exceptions import ALBProvisioningError, TargetGroupError
from .base import BaseService
from .aws_client_factory import AWSClientFactory, get_aws_client_factory


class ALBService(BaseService):
    """
    Service for provisioning Application Load Balancers for ECS clusters.

    Creates internet-facing ALBs with HTTP/HTTPS listeners, target groups
    for individual ECS services, and listener rules for path/host routing.
    """

    def __init__(self, aws_factory: Optional[AWSClientFactory] = None, **kwargs):
        super().__init__(**kwargs)
        self.aws_factory = aws_factory or get_aws_client_factory()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def provision_alb(
        self,
        cluster,
        vpc_infrastructure,
        security_group_id: str,
        certificate_arn: Optional[str] = None,
        region: Optional[str] = None,
        credential=None,
    ) -> LoadBalancerConfig:
        """
        Provision an internet-facing ALB for the ECS cluster.

        Creates the ALB in public subnets, configures an HTTP listener
        (with optional HTTPS redirect), and optionally creates an HTTPS
        listener when a certificate ARN is provided.

        Args:
            cluster: ECSCluster model instance.
            vpc_infrastructure: VPCInfrastructure model with subnet IDs.
            security_group_id: Security group ID for the ALB.
            certificate_arn: ACM certificate ARN for HTTPS (optional).
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            LoadBalancerConfig model instance.

        Raises:
            ALBProvisioningError: If ALB creation or configuration fails.
        """
        elbv2 = self.aws_factory.get_client(
            "elbv2", region=region, credential=credential
        )
        cluster_name = cluster.name
        alb_name = f"{cluster_name}-alb"

        # Truncate ALB name to 32 chars (AWS limit)
        if len(alb_name) > 32:
            alb_name = alb_name[:32]

        # Check for existing ALB
        existing_alb = self._find_existing_alb(elbv2, alb_name)
        if existing_alb:
            self.log_info(f"Found existing ALB: {alb_name}")
            return self._sync_alb_to_db(
                cluster, existing_alb, security_group_id, certificate_arn
            )

        try:
            # Create ALB
            public_subnets = vpc_infrastructure.public_subnet_ids
            if not public_subnets or len(public_subnets) < 2:
                raise ALBProvisioningError(
                    "At least 2 public subnets are required for ALB creation"
                )

            response = elbv2.create_load_balancer(
                Name=alb_name,
                Subnets=public_subnets,
                SecurityGroups=[security_group_id],
                Scheme="internet-facing",
                Type="application",
                IpAddressType="ipv4",
                Tags=[
                    {"Key": "remote-compose:cluster", "Value": cluster_name},
                    {"Key": "remote-compose:managed", "Value": "true"},
                    {"Key": "Name", "Value": alb_name},
                ],
            )

            alb = response["LoadBalancers"][0]
            alb_arn = alb["LoadBalancerArn"]
            alb_dns = alb.get("DNSName", "")
            alb_zone_id = alb.get("CanonicalHostedZoneId", "")

            self.log_info(f"Created ALB {alb_name}: {alb_dns}")

            # Wait for ALB to be active
            self._wait_for_alb_active(elbv2, alb_arn)

            # Create default target group for the HTTP listener
            default_tg_name = f"{cluster_name}-default"
            if len(default_tg_name) > 32:
                default_tg_name = default_tg_name[:32]

            default_tg_response = elbv2.create_target_group(
                Name=default_tg_name,
                Protocol="HTTP",
                Port=80,
                VpcId=vpc_infrastructure.vpc_id,
                TargetType="ip",
                HealthCheckProtocol="HTTP",
                HealthCheckPath="/health",
                HealthCheckIntervalSeconds=30,
                HealthyThresholdCount=3,
                UnhealthyThresholdCount=3,
                Tags=[
                    {"Key": "remote-compose:cluster", "Value": cluster_name},
                    {"Key": "remote-compose:managed", "Value": "true"},
                ],
            )
            default_tg_arn = default_tg_response["TargetGroups"][0]["TargetGroupArn"]

            # Create HTTP listener
            http_listener_arn = ""
            https_listener_arn = ""

            if certificate_arn:
                # HTTP listener redirects to HTTPS
                http_response = elbv2.create_listener(
                    LoadBalancerArn=alb_arn,
                    Protocol="HTTP",
                    Port=80,
                    DefaultActions=[
                        {
                            "Type": "redirect",
                            "RedirectConfig": {
                                "Protocol": "HTTPS",
                                "Port": "443",
                                "StatusCode": "HTTP_301",
                            },
                        }
                    ],
                )
                http_listener_arn = http_response["Listeners"][0]["ListenerArn"]

                # HTTPS listener forwards to default target group
                https_response = elbv2.create_listener(
                    LoadBalancerArn=alb_arn,
                    Protocol="HTTPS",
                    Port=443,
                    SslPolicy="ELBSecurityPolicy-TLS13-1-2-2021-06",
                    Certificates=[{"CertificateArn": certificate_arn}],
                    DefaultActions=[
                        {
                            "Type": "forward",
                            "TargetGroupArn": default_tg_arn,
                        }
                    ],
                )
                https_listener_arn = https_response["Listeners"][0]["ListenerArn"]
            else:
                # HTTP listener forwards to default target group
                http_response = elbv2.create_listener(
                    LoadBalancerArn=alb_arn,
                    Protocol="HTTP",
                    Port=80,
                    DefaultActions=[
                        {
                            "Type": "forward",
                            "TargetGroupArn": default_tg_arn,
                        }
                    ],
                )
                http_listener_arn = http_response["Listeners"][0]["ListenerArn"]

            self.log_info(f"Configured listeners for ALB {alb_name}")

            # Persist to database
            lb_config, _ = LoadBalancerConfig.objects.update_or_create(
                cluster=cluster,
                defaults={
                    "alb_arn": alb_arn,
                    "alb_dns_name": alb_dns,
                    "alb_hosted_zone_id": alb_zone_id,
                    "http_listener_arn": http_listener_arn,
                    "https_listener_arn": https_listener_arn,
                    "certificate_arn": certificate_arn or "",
                    "security_group_id": security_group_id,
                },
            )

            # Save default target group record
            TargetGroupConfig.objects.update_or_create(
                cluster=cluster,
                target_group_name=default_tg_name,
                defaults={
                    "target_group_arn": default_tg_arn,
                    "port": 80,
                    "protocol": "HTTP",
                    "health_check_path": "/health",
                    "is_default": True,
                },
            )

            self.notify_observers(
                "alb_provisioned",
                cluster_name=cluster_name,
                alb_arn=alb_arn,
                dns_name=alb_dns,
            )

            return lb_config

        except ClientError as e:
            raise ALBProvisioningError(
                f"Failed to provision ALB for cluster {cluster_name}: {e}",
                alb_arn=locals().get("alb_arn"),
            )

    def create_target_group(
        self,
        cluster,
        vpc_id: str,
        service_name: str,
        port: int,
        health_check_path: str = "/health",
        region: Optional[str] = None,
        credential=None,
    ) -> TargetGroupConfig:
        """
        Create a target group for an ECS service.

        Target type is 'ip' for Fargate awsvpc networking mode. The target
        group name is truncated to 32 characters (AWS limit).

        Args:
            cluster: ECSCluster model instance.
            vpc_id: VPC ID.
            service_name: Name of the ECS service.
            port: Port the service listens on.
            health_check_path: Health check endpoint (default '/health').
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            TargetGroupConfig model instance.

        Raises:
            TargetGroupError: If target group creation fails.
        """
        elbv2 = self.aws_factory.get_client(
            "elbv2", region=region, credential=credential
        )
        cluster_name = cluster.name

        tg_name = f"{cluster_name}-{service_name}"
        if len(tg_name) > 32:
            tg_name = tg_name[:32]

        # Check for existing target group
        try:
            response = elbv2.describe_target_groups(Names=[tg_name])
            existing_tgs = response.get("TargetGroups", [])
            if existing_tgs:
                tg = existing_tgs[0]
                self.log_info(f"Found existing target group: {tg_name}")
                tg_config, _ = TargetGroupConfig.objects.update_or_create(
                    cluster=cluster,
                    target_group_name=tg_name,
                    defaults={
                        "target_group_arn": tg["TargetGroupArn"],
                        "port": port,
                        "protocol": tg.get("Protocol", "HTTP"),
                        "health_check_path": health_check_path,
                    },
                )
                return tg_config
        except ClientError as e:
            if "TargetGroupNotFound" not in str(e):
                self.log_warning(f"Error checking for target group {tg_name}: {e}")

        try:
            response = elbv2.create_target_group(
                Name=tg_name,
                Protocol="HTTP",
                Port=port,
                VpcId=vpc_id,
                TargetType="ip",
                HealthCheckProtocol="HTTP",
                HealthCheckPath=health_check_path,
                HealthCheckIntervalSeconds=30,
                HealthyThresholdCount=3,
                UnhealthyThresholdCount=3,
                Tags=[
                    {"Key": "remote-compose:cluster", "Value": cluster_name},
                    {"Key": "remote-compose:managed", "Value": "true"},
                    {"Key": "remote-compose:service", "Value": service_name},
                    {"Key": "Name", "Value": tg_name},
                ],
            )

            tg = response["TargetGroups"][0]
            tg_arn = tg["TargetGroupArn"]

            self.log_info(f"Created target group {tg_name} on port {port}")

            tg_config, _ = TargetGroupConfig.objects.update_or_create(
                cluster=cluster,
                target_group_name=tg_name,
                defaults={
                    "target_group_arn": tg_arn,
                    "port": port,
                    "protocol": "HTTP",
                    "health_check_path": health_check_path,
                },
            )

            self.notify_observers(
                "target_group_created",
                cluster_name=cluster_name,
                service_name=service_name,
                target_group_arn=tg_arn,
            )

            return tg_config

        except ClientError as e:
            raise TargetGroupError(
                f"Failed to create target group {tg_name}: {e}",
            )

    def create_listener_rule(
        self,
        listener_arn: str,
        target_group_arn: str,
        priority: int,
        conditions: Optional[List[Dict[str, Any]]] = None,
        region: Optional[str] = None,
        credential=None,
    ) -> str:
        """
        Create a listener rule to route traffic to a target group.

        Args:
            listener_arn: ARN of the listener.
            target_group_arn: ARN of the target group.
            priority: Rule priority (1-50000).
            conditions: List of condition dicts. Defaults to a path pattern
                        matching '/*' if not specified.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            The ARN of the created listener rule.

        Raises:
            ALBProvisioningError: If rule creation fails.
        """
        elbv2 = self.aws_factory.get_client(
            "elbv2", region=region, credential=credential
        )

        if conditions is None:
            conditions = [
                {
                    "Field": "path-pattern",
                    "Values": ["/*"],
                }
            ]

        try:
            response = elbv2.create_rule(
                ListenerArn=listener_arn,
                Conditions=conditions,
                Priority=priority,
                Actions=[
                    {
                        "Type": "forward",
                        "TargetGroupArn": target_group_arn,
                    }
                ],
            )

            rule_arn = response["Rules"][0]["RuleArn"]
            self.log_info(f"Created listener rule (priority {priority}): {rule_arn}")

            return rule_arn

        except ClientError as e:
            if "PriorityInUse" in str(e):
                raise ALBProvisioningError(
                    f"Listener rule priority {priority} is already in use",
                    alb_arn=listener_arn,
                )
            raise ALBProvisioningError(
                f"Failed to create listener rule: {e}",
                alb_arn=listener_arn,
            )

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _find_existing_alb(self, elbv2, alb_name: str) -> Optional[Dict[str, Any]]:
        """Find an existing ALB by name."""
        try:
            response = elbv2.describe_load_balancers(Names=[alb_name])
            load_balancers = response.get("LoadBalancers", [])
            if load_balancers:
                return load_balancers[0]
            return None
        except ClientError as e:
            if "LoadBalancerNotFound" in str(e):
                return None
            self.log_warning(f"Error searching for ALB {alb_name}: {e}")
            return None

    def _sync_alb_to_db(
        self,
        cluster,
        alb_data: Dict[str, Any],
        security_group_id: str,
        certificate_arn: Optional[str],
    ) -> LoadBalancerConfig:
        """Sync an existing ALB's data to the database model."""
        alb_arn = alb_data["LoadBalancerArn"]

        # Get existing listeners
        elbv2 = self.aws_factory.get_client("elbv2")
        http_listener_arn = ""
        https_listener_arn = ""

        try:
            listener_response = elbv2.describe_listeners(LoadBalancerArn=alb_arn)
            for listener in listener_response.get("Listeners", []):
                if listener.get("Port") == 80:
                    http_listener_arn = listener["ListenerArn"]
                elif listener.get("Port") == 443:
                    https_listener_arn = listener["ListenerArn"]
        except ClientError:
            pass

        lb_config, _ = LoadBalancerConfig.objects.update_or_create(
            cluster=cluster,
            defaults={
                "alb_arn": alb_arn,
                "alb_dns_name": alb_data.get("DNSName", ""),
                "alb_hosted_zone_id": alb_data.get("CanonicalHostedZoneId", ""),
                "http_listener_arn": http_listener_arn,
                "https_listener_arn": https_listener_arn,
                "certificate_arn": certificate_arn or "",
                "security_group_id": security_group_id,
            },
        )
        return lb_config

    def _wait_for_alb_active(
        self,
        elbv2,
        alb_arn: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> None:
        """Wait for an ALB to reach the 'active' state."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = elbv2.describe_load_balancers(
                    LoadBalancerArns=[alb_arn],
                )
                lbs = response.get("LoadBalancers", [])
                if lbs and lbs[0].get("State", {}).get("Code") == "active":
                    self.log_info(f"ALB {alb_arn} is active")
                    return

                time.sleep(poll_interval)

            except ClientError as e:
                self.log_warning(f"Error polling ALB status: {e}")
                time.sleep(poll_interval)

        raise ALBProvisioningError(
            "Timeout waiting for ALB to become active",
            alb_arn=alb_arn,
        )
