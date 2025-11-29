"""
Management command to list deployments.
"""

from django.core.management.base import BaseCommand

from remote_compose.services import DeploymentService, TargetService


class Command(BaseCommand):
    help = 'List deployments'

    def add_arguments(self, parser):
        parser.add_argument(
            '--target',
            help='Filter by target name'
        )
        parser.add_argument(
            '--project',
            help='Filter by project name'
        )
        parser.add_argument(
            '--status',
            choices=['pending', 'running', 'success', 'failed', 'rolled_back', 'cancelled'],
            help='Filter by status'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=20,
            help='Maximum number of deployments to show (default: 20)'
        )
        parser.add_argument(
            '--verbose',
            '-v',
            action='store_true',
            help='Show detailed information'
        )

    def handle(self, *args, **options):
        deployment_service = DeploymentService()
        target_service = TargetService()

        # Get target if specified
        target = None
        if options.get('target'):
            try:
                target = target_service.get_target_by_name(options['target'])
            except Exception:
                self.stdout.write(self.style.ERROR(f"Target not found: {options['target']}"))
                return

        deployments = deployment_service.list_deployments(
            target=target,
            status=options.get('status'),
            project_name=options.get('project'),
            limit=options['limit'],
        )

        if not deployments:
            self.stdout.write(self.style.WARNING('No deployments found.'))
            return

        self.stdout.write(f"\nDeployments ({len(deployments)}):")
        self.stdout.write("-" * 80)

        for deployment in deployments:
            status_colors = {
                'success': self.style.SUCCESS,
                'failed': self.style.ERROR,
                'running': self.style.WARNING,
                'pending': self.style.NOTICE,
                'cancelled': self.style.NOTICE,
                'rolled_back': self.style.NOTICE,
            }
            color = status_colors.get(deployment.status, lambda x: x)

            self.stdout.write(
                f"\n#{deployment.id} {color(deployment.status.upper())}"
            )
            self.stdout.write(f"  Project: {deployment.project_name}")
            self.stdout.write(f"  Target: {deployment.target.name}")
            self.stdout.write(f"  Type: {deployment.deployment_type}")

            if deployment.version:
                self.stdout.write(f"  Version: {deployment.version}")

            self.stdout.write(f"  Started: {deployment.started_at or 'Not started'}")

            if deployment.duration:
                self.stdout.write(f"  Duration: {deployment.duration:.1f}s")

            if options['verbose']:
                if deployment.deployed_by:
                    self.stdout.write(f"  Deployed by: {deployment.deployed_by}")
                if deployment.error_message:
                    self.stdout.write(f"  Error: {deployment.error_message[:100]}")
                if deployment.container_ids:
                    self.stdout.write(f"  Containers: {len(deployment.container_ids)}")

        self.stdout.write("")
