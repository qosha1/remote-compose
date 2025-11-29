"""
Management command to deploy a multi-service Docker Compose application to ECS.
"""

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster
from remote_compose.services.deployment_pipeline.pipeline import PipelineBuilder
from remote_compose.services.deployment_pipeline.context import PipelineContext


class Command(BaseCommand):
    help = 'Deploy a multi-service Docker Compose application to AWS ECS'

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
            '--project-name',
            '-p',
            help='Project name (default: directory name)'
        )
        parser.add_argument(
            '--service-config',
            help='Path to service configuration YAML (per-service CPU/memory/type)'
        )
        parser.add_argument(
            '--secrets-file',
            action='append',
            dest='secrets_files',
            help='Path to env file with secrets (can be specified multiple times)'
        )
        parser.add_argument(
            '--image-tag',
            default='latest',
            help='Docker image tag (default: latest)'
        )
        parser.add_argument(
            '--version',
            default='',
            help='Version label for this deployment'
        )
        parser.add_argument(
            '--timeout',
            type=int,
            default=600,
            help='Timeout waiting for services to stabilize (default: 600s)'
        )
        parser.add_argument(
            '--no-wait',
            action='store_true',
            help='Do not wait for services to stabilize'
        )
        parser.add_argument(
            '--no-build',
            action='store_true',
            help='Skip image building (use existing images in ECR)'
        )
        parser.add_argument(
            '--force-rebuild',
            action='store_true',
            help='Force rebuild all images even if cached'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deployed without making changes'
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

        # Validate service config
        service_config = options.get('service_config')
        if service_config and not os.path.exists(service_config):
            raise CommandError(f"Service config not found: {service_config}")

        # Project name
        project_name = options.get('project_name')
        if not project_name:
            project_name = Path(compose_file).parent.name

        # Validate secrets files
        secrets_files = options.get('secrets_files') or []
        for sf in secrets_files:
            if not os.path.exists(sf):
                raise CommandError(f"Secrets file not found: {sf}")

        self.stdout.write(f"Deploying to ECS cluster: {cluster.name} ({cluster.aws_region})")
        self.stdout.write(f"Compose file: {compose_file}")
        self.stdout.write(f"Project: {project_name}")
        self.stdout.write(f"Image tag: {options['image_tag']}")

        if options['dry_run']:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be made"))

        # Build pipeline context
        context = PipelineContext(
            cluster=cluster,
            compose_file_path=Path(compose_file),
            project_name=project_name,
            image_tag=options['image_tag'],
            version=options.get('version', ''),
            deployed_by='cli',
            build_images=not options['no_build'],
            force_rebuild=options['force_rebuild'],
            wait_for_stable=not options['no_wait'],
            timeout=options['timeout'],
            dry_run=options['dry_run'],
            service_config_path=service_config,
            secrets_files=secrets_files,
        )

        # Build and execute pipeline
        pipeline = PipelineBuilder.multi_service_deployment()

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
            elif event_type == 'service_deployed':
                svc = kwargs.get('service', '')
                action = kwargs.get('action', 'deployed')
                self.stdout.write(f"    -> {svc}: {action}")
            elif event_type == 'service_stable':
                svc = kwargs.get('service', '')
                running = kwargs.get('running', 0)
                desired = kwargs.get('desired', 0)
                self.stdout.write(f"    -> {svc}: {running}/{desired} tasks running")

        pipeline.attach_event_handler(progress_handler)

        result = pipeline.execute(context)

        if result.success:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Multi-service deployment complete!"))
            self.stdout.write(f"  Duration: {result.duration_seconds:.1f}s")
            self.stdout.write(f"  Services: {len(context.ecs_services)}")

            if context.service_order:
                self.stdout.write(f"  Order: {' -> '.join(context.service_order)}")

            if context.load_balancer:
                self.stdout.write(f"  ALB: {context.load_balancer.alb_dns_name}")

            for svc_name, svc_model in context.ecs_services.items():
                status = getattr(svc_model, 'status', 'unknown')
                running = getattr(svc_model, 'running_count', '?')
                desired = getattr(svc_model, 'desired_count', '?')
                self.stdout.write(
                    f"  {svc_name}: {status} ({running}/{desired} tasks)"
                )

            if context.warnings:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING(f"Warnings ({len(context.warnings)}):"))
                for w in context.warnings[:10]:
                    self.stdout.write(f"  - {w}")
                if len(context.warnings) > 10:
                    self.stdout.write(f"  ... and {len(context.warnings) - 10} more")
        else:
            raise CommandError(
                f"Deployment failed at '{result.failed_step}': {result.error}"
            )
