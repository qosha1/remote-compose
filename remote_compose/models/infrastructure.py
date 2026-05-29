"""
Infrastructure models for multi-service ECS deployments.

Tracks VPC, security groups, load balancers, target groups,
secrets, and Service Connect namespaces provisioned by remote-compose.
"""

from django.db import models
from .base import TimestampedModel


class VPCInfrastructure(TimestampedModel):
    """
    Tracks VPC infrastructure provisioned for ECS deployments.

    Stores IDs for VPC, subnets, internet gateway, NAT gateway,
    and route tables created by remote-compose.
    """

    cluster = models.OneToOneField(
        "ECSCluster",
        on_delete=models.CASCADE,
        related_name="vpc_infrastructure",
    )

    vpc_id = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
    )
    vpc_cidr = models.CharField(max_length=20, default="10.0.0.0/16")

    public_subnet_ids = models.JSONField(
        default=list, help_text="Public subnet IDs (for ALB, NAT gateway)"
    )
    private_subnet_ids = models.JSONField(
        default=list, help_text="Private subnet IDs (for ECS tasks)"
    )

    internet_gateway_id = models.CharField(max_length=50, blank=True)
    nat_gateway_id = models.CharField(max_length=50, blank=True)
    elastic_ip_allocation_id = models.CharField(max_length=50, blank=True)

    public_route_table_id = models.CharField(max_length=50, blank=True)
    private_route_table_id = models.CharField(max_length=50, blank=True)

    is_managed = models.BooleanField(
        default=True, help_text="Whether this VPC was created by remote-compose"
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_vpc_infrastructure"
        verbose_name = "VPC Infrastructure"
        verbose_name_plural = "VPC Infrastructures"

    def __str__(self):
        return f"VPC {self.vpc_id} ({self.cluster.name})"

    @property
    def all_subnet_ids(self):
        return (self.public_subnet_ids or []) + (self.private_subnet_ids or [])


class SecurityGroupConfig(TimestampedModel):
    """
    Tracks security groups created for ECS deployments.

    Each security group serves a specific purpose (ALB, ECS tasks,
    database, cache, EFS).
    """

    class Purpose(models.TextChoices):
        ALB = "alb", "Application Load Balancer"
        ECS_TASKS = "ecs_tasks", "ECS Tasks"
        DATABASE = "database", "Database (PostgreSQL)"
        CACHE = "cache", "Cache (Redis)"
        EFS = "efs", "Elastic File System"

    cluster = models.ForeignKey(
        "ECSCluster",
        on_delete=models.CASCADE,
        related_name="security_groups",
    )

    security_group_id = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
    )
    purpose = models.CharField(
        max_length=20,
        choices=Purpose.choices,
    )
    vpc_id = models.CharField(max_length=50)

    inbound_rules = models.JSONField(
        default=list, help_text="Inbound security group rules"
    )
    outbound_rules = models.JSONField(
        default=list, help_text="Outbound security group rules"
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_security_groups"
        verbose_name = "Security Group"
        verbose_name_plural = "Security Groups"
        unique_together = [["cluster", "purpose"]]

    def __str__(self):
        return f"{self.get_purpose_display()} SG {self.security_group_id}"


class LoadBalancerConfig(TimestampedModel):
    """
    Tracks Application Load Balancers provisioned for ECS deployments.
    """

    cluster = models.OneToOneField(
        "ECSCluster",
        on_delete=models.CASCADE,
        related_name="load_balancer",
    )

    alb_arn = models.CharField(
        max_length=512,
        unique=True,
        db_index=True,
    )
    alb_dns_name = models.CharField(max_length=512, blank=True)
    alb_hosted_zone_id = models.CharField(max_length=50, blank=True)

    http_listener_arn = models.CharField(max_length=512, blank=True)
    https_listener_arn = models.CharField(max_length=512, blank=True)
    certificate_arn = models.CharField(max_length=512, blank=True)

    security_group_id = models.CharField(max_length=50, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_load_balancers"
        verbose_name = "Load Balancer"
        verbose_name_plural = "Load Balancers"

    def __str__(self):
        return f"ALB {self.alb_dns_name} ({self.cluster.name})"


class TargetGroupConfig(TimestampedModel):
    """
    Tracks ALB target groups linked to ECS services.
    """

    cluster = models.ForeignKey(
        "ECSCluster",
        on_delete=models.CASCADE,
        related_name="target_groups",
    )
    ecs_service = models.OneToOneField(
        "ECSService",
        on_delete=models.CASCADE,
        related_name="target_group",
        null=True,
        blank=True,
    )

    target_group_arn = models.CharField(
        max_length=512,
        unique=True,
        db_index=True,
    )
    target_group_name = models.CharField(max_length=32)
    port = models.IntegerField()
    protocol = models.CharField(max_length=10, default="HTTP")

    health_check_path = models.CharField(max_length=255, default="/health")
    health_check_interval = models.IntegerField(default=30)
    healthy_threshold = models.IntegerField(default=3)
    unhealthy_threshold = models.IntegerField(default=3)

    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the default target group for the ALB listener",
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_target_groups"
        verbose_name = "Target Group"
        verbose_name_plural = "Target Groups"

    def __str__(self):
        return f"TG {self.target_group_name} (port {self.port})"


class SecretConfig(TimestampedModel):
    """
    Tracks AWS Secrets Manager secrets managed by remote-compose.
    """

    cluster = models.ForeignKey(
        "ECSCluster",
        on_delete=models.CASCADE,
        related_name="managed_secrets",
    )

    secret_arn = models.CharField(
        max_length=512,
        unique=True,
        db_index=True,
    )
    secret_name = models.CharField(max_length=512)
    env_var_name = models.CharField(
        max_length=255, help_text="Environment variable name this secret maps to"
    )

    source_file = models.CharField(
        max_length=512,
        blank=True,
        help_text="Source env file this secret was read from",
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_secret_configs"
        verbose_name = "Secret Config"
        verbose_name_plural = "Secret Configs"
        unique_together = [["cluster", "env_var_name"]]

    def __str__(self):
        return f"{self.env_var_name} -> {self.secret_name}"


class ServiceConnectNamespace(TimestampedModel):
    """
    Tracks Cloud Map namespaces used for ECS Service Connect.
    """

    cluster = models.OneToOneField(
        "ECSCluster",
        on_delete=models.CASCADE,
        related_name="service_connect_namespace",
    )

    namespace_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )
    namespace_name = models.CharField(max_length=255)
    namespace_arn = models.CharField(max_length=512, blank=True)
    namespace_type = models.CharField(
        max_length=20, default="HTTP", help_text="Namespace type: HTTP or DNS_PRIVATE"
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "remote_compose_service_connect_namespaces"
        verbose_name = "Service Connect Namespace"
        verbose_name_plural = "Service Connect Namespaces"

    def __str__(self):
        return f"{self.namespace_name} ({self.namespace_id})"
