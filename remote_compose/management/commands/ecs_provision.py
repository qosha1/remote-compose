"""
Management command to provision AWS infrastructure for multi-service ECS deployment.
"""

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster
from remote_compose.services.deployment_pipeline.pipeline import PipelineBuilder
from remote_compose.services.deployment_pipeline.context import PipelineContext


class Command(BaseCommand):
    help = 'Provision AWS infrastructure (VPC, ALB, security groups, secrets) for ECS deployment'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cluster',
            required=True,
            help='ECS cluster name'
        )
        parser.add_argument(
            '--compose-file',
            '-f',
            required=True,
            help='Path to docker-compose.yml file'
        )
        parser.add_argument(
            '--service-config',
            help='Path to service configuration YAML file'
        )
        parser.add_argument(
            '--secrets-file',
            action='append',
            dest='secrets_files',
            help='Path to env file with secrets (can be specified multiple times)'
        )
        parser.add_argument(
            '--certificate-arn',
            help='ACM certificate ARN for HTTPS'
        )
        parser.add_argument(
            '--vpc-cidr',
            default='10.0.0.0/16',
            help='VPC CIDR block (default: 10.0.0.0/16)'
        )
        parser.add_argument(
            '--project-name',
            '-p',
            help='Project name (default: directory name)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be provisioned without making changes'
        )

    def handle(self, *args, **options):
        # Get cluster
        try:
            cluster = ECSCluster.objects.get(name=options['cluster'])
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {options['cluster']}")

        # Validate compose file
        compose_file = options['compose_file']
        if not os.path.exists(compose_file):
            raise CommandError(f"Compose file not found: {compose_file}")

        # Project name
        project_name = options.get('project_name')
        if not project_name:
            project_name = Path(compose_file).parent.name

        # Validate secrets files
        secrets_files = options.get('secrets_files') or []
        for sf in secrets_files:
            if not os.path.exists(sf):
                raise CommandError(f"Secrets file not found: {sf}")

        self.stdout.write(f"Provisioning infrastructure for cluster: {cluster.name}")
        self.stdout.write(f"Region: {cluster.aws_region}")
        self.stdout.write(f"Compose file: {compose_file}")
        self.stdout.write(f"VPC CIDR: {options['vpc_cidr']}")

        if options['dry_run']:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be made"))

        # Build pipeline context
        context = PipelineContext(
            cluster=cluster,
            compose_file_path=Path(compose_file),
            project_name=project_name,
            dry_run=options['dry_run'],
            deployed_by='cli',
            vpc_cidr=options['vpc_cidr'],
            certificate_arn=options.get('certificate_arn'),
            secrets_files=secrets_files,
            service_config_path=options.get('service_config'),
        )

        # Build and execute pipeline
        pipeline = PipelineBuilder.infrastructure_provisioning()

        # Attach progress handler
        def progress_handler(event_type, **kwargs):
            step = kwargs.get('step', '')
            if event_type == 'step_started':
                self.stdout.write(f"  [{step}] Starting...")
            elif event_type == 'step_completed':
                result = kwargs.get('result')
                msg = result.message if result else 'Done'
                self.stdout.write(self.style.SUCCESS(f"  [{step}] {msg}"))
            elif event_type == 'step_skipped':
                result = kwargs.get('result')
                msg = result.message if result else 'Skipped'
                self.stdout.write(f"  [{step}] {msg}")
            elif event_type == 'step_failed':
                result = kwargs.get('result')
                msg = result.message if result else 'Failed'
                self.stdout.write(self.style.ERROR(f"  [{step}] {msg}"))

        pipeline.attach_event_handler(progress_handler)

        result = pipeline.execute(context)

        if result.success:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Infrastructure provisioning complete!"))
            self.stdout.write(f"  Duration: {result.duration_seconds:.1f}s")
            self.stdout.write(f"  Steps: {', '.join(result.completed_steps)}")

            if context.vpc_infrastructure:
                self.stdout.write(f"  VPC: {context.vpc_infrastructure.vpc_id}")
            if context.load_balancer:
                self.stdout.write(f"  ALB DNS: {context.load_balancer.alb_dns_name}")
            if context.service_connect_namespace:
                self.stdout.write(
                    f"  Service Connect: {context.service_connect_namespace.namespace_name}"
                )
            if context.secrets_arns:
                self.stdout.write(f"  Secrets: {len(context.secrets_arns)} configured")

            if context.warnings:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING(f"Warnings ({len(context.warnings)}):"))
                for w in context.warnings:
                    self.stdout.write(f"  - {w}")
        else:
            raise CommandError(
                f"Infrastructure provisioning failed at '{result.failed_step}': "
                f"{result.error}"
            )
