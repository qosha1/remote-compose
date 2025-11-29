"""
Infrastructure provisioning pipeline steps.

Handles VPC, security groups, ALB, Service Connect, secrets,
and IAM setup for multi-service ECS deployments.
"""

import json

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class ProvisionVPCStep(PipelineStep):
    """
    Provision VPC infrastructure for ECS deployment.

    Creates VPC with public/private subnets, IGW, NAT gateway,
    and route tables. Uses get-or-create pattern for idempotency.
    """

    def __init__(self):
        super().__init__("ProvisionVPC")

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok(
                f"[DRY RUN] Would provision VPC with CIDR {context.vpc_cidr}"
            )

        vpc_service = context.services.vpc

        try:
            vpc_infra = vpc_service.provision_vpc(
                cluster=context.cluster,
                vpc_cidr=context.vpc_cidr,
                region=context.cluster.aws_region,
                credential=context.cluster.aws_credential,
            )

            context.vpc_infrastructure = vpc_infra

            # Update cluster with VPC/subnet info
            context.cluster.vpc_id = vpc_infra.vpc_id
            context.cluster.subnet_ids = vpc_infra.private_subnet_ids
            context.cluster.save(update_fields=['vpc_id', 'subnet_ids', 'updated_at'])

            context.track_resource(
                resource_type='vpc',
                resource_id=vpc_infra.vpc_id,
            )

            return StepResult.ok(
                f"VPC provisioned: {vpc_infra.vpc_id} "
                f"({len(vpc_infra.public_subnet_ids)} public, "
                f"{len(vpc_infra.private_subnet_ids)} private subnets)"
            )

        except Exception as e:
            return StepResult.fail(f"VPC provisioning failed: {e}", error=e)


class CreateSecurityGroupsStep(PipelineStep):
    """
    Create security groups for ALB, ECS tasks, database, cache, and EFS.
    """

    def __init__(self):
        super().__init__("CreateSecurityGroups")

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok(
                "[DRY RUN] Would create security groups (ALB, ECS, DB, Cache, EFS)"
            )

        if not context.vpc_infrastructure:
            return StepResult.fail("No VPC infrastructure available")

        sg_service = context.services.security_group

        try:
            sg_map = sg_service.provision_security_groups(
                cluster=context.cluster,
                vpc_id=context.vpc_infrastructure.vpc_id,
                region=context.cluster.aws_region,
                credential=context.cluster.aws_credential,
            )

            context.security_groups = sg_map

            # Update cluster security groups
            context.cluster.security_group_ids = list(sg_map.values())
            context.cluster.save(update_fields=['security_group_ids', 'updated_at'])

            return StepResult.ok(
                f"Security groups created: {', '.join(sg_map.keys())}"
            )

        except Exception as e:
            return StepResult.fail(
                f"Security group creation failed: {e}", error=e
            )


class ProvisionALBStep(PipelineStep):
    """
    Provision Application Load Balancer for public-facing services.

    Only runs if there are services marked as public.
    """

    def __init__(self):
        super().__init__("ProvisionALB")

    def should_run(self, context: PipelineContext) -> bool:
        return bool(context.public_services)

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            services = ', '.join(context.public_services.keys())
            return StepResult.ok(
                f"[DRY RUN] Would provision ALB for services: {services}"
            )

        if not context.vpc_infrastructure:
            return StepResult.fail("No VPC infrastructure for ALB")

        alb_sg = context.security_groups.get('alb')
        if not alb_sg:
            return StepResult.fail("No ALB security group available")

        alb_service = context.services.alb

        try:
            lb_config = alb_service.provision_alb(
                cluster=context.cluster,
                vpc_infrastructure=context.vpc_infrastructure,
                security_group_id=alb_sg,
                certificate_arn=context.certificate_arn,
                region=context.cluster.aws_region,
                credential=context.cluster.aws_credential,
            )

            context.load_balancer = lb_config

            context.track_resource(
                resource_type='alb',
                resource_id=lb_config.alb_arn,
            )

            return StepResult.ok(
                f"ALB provisioned: {lb_config.alb_dns_name}"
            )

        except Exception as e:
            return StepResult.fail(f"ALB provisioning failed: {e}", error=e)


class SetupServiceConnectStep(PipelineStep):
    """
    Set up Cloud Map namespace for ECS Service Connect.
    """

    def __init__(self):
        super().__init__("SetupServiceConnect")

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok(
                f"[DRY RUN] Would create Service Connect namespace: "
                f"{context.cluster.name}"
            )

        sc_service = context.services.service_connect

        try:
            namespace = sc_service.get_or_create_namespace(
                cluster=context.cluster,
                region=context.cluster.aws_region,
                credential=context.cluster.aws_credential,
            )

            context.service_connect_namespace = namespace

            context.track_resource(
                resource_type='cloud_map_namespace',
                resource_id=namespace.namespace_id,
            )

            return StepResult.ok(
                f"Service Connect namespace: {namespace.namespace_name}"
            )

        except Exception as e:
            return StepResult.fail(
                f"Service Connect setup failed: {e}", error=e
            )


class ProvisionSecretsStep(PipelineStep):
    """
    Push environment secrets to AWS Secrets Manager.

    Reads env files and creates secrets for each variable.
    """

    def __init__(self):
        super().__init__("ProvisionSecrets")

    def should_run(self, context: PipelineContext) -> bool:
        return bool(context.secrets_files)

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok(
                f"[DRY RUN] Would provision secrets from "
                f"{len(context.secrets_files)} env file(s)"
            )

        secrets_service = context.services.secrets
        total_secrets = 0

        try:
            for env_file in context.secrets_files:
                arns = secrets_service.push_env_file(
                    cluster=context.cluster,
                    env_file_path=env_file,
                    region=context.cluster.aws_region,
                    credential=context.cluster.aws_credential,
                )
                context.secrets_arns.update(arns)
                total_secrets += len(arns)

            return StepResult.ok(
                f"Provisioned {total_secrets} secrets from "
                f"{len(context.secrets_files)} file(s)"
            )

        except Exception as e:
            return StepResult.fail(
                f"Secret provisioning failed: {e}", error=e
            )


class SetupIAMRolesStep(PipelineStep):
    """
    Ensure ECS task execution role has required policies.

    Adds Secrets Manager read permissions if secrets are configured.
    """

    def __init__(self):
        super().__init__("SetupIAMRoles")

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok("[DRY RUN] Would configure IAM roles")

        if not context.cluster.task_execution_role_arn:
            context.add_warning(
                "No task execution role configured on cluster. "
                "ECS tasks may not have permission to pull secrets or images."
            )
            return StepResult.ok("Skipped IAM setup: no execution role configured")

        # If we have secrets, ensure the execution role can read them
        if not context.secrets_arns:
            return StepResult.ok("IAM roles verified (no secrets to configure)")

        from ...aws_client_factory import get_aws_client_factory

        factory = get_aws_client_factory()
        iam = factory.get_client('iam',
            region=context.cluster.aws_region,
            credential=context.cluster.aws_credential,
        )

        try:
            # Extract role name from ARN
            role_arn = context.cluster.task_execution_role_arn
            role_name = role_arn.split('/')[-1]

            # Create inline policy for secrets access
            policy_name = f"{context.cluster.name}-secrets-access"
            secret_arns = list(context.secrets_arns.values())

            policy_document = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "secretsmanager:GetSecretValue",
                        ],
                        "Resource": secret_arns,
                    }
                ]
            }

            iam.put_role_policy(
                RoleName=role_name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_document),
            )

            return StepResult.ok(
                f"IAM policy updated: {len(secret_arns)} secret(s) accessible"
            )

        except Exception as e:
            context.add_warning(f"IAM policy update failed: {e}")
            return StepResult.ok(
                f"IAM setup warning: {e} (deployment will continue)"
            )
