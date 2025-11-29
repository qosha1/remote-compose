"""
Management command to rollback a deployment.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.services import DeploymentService
from remote_compose.models import Deployment


class Command(BaseCommand):
    help = 'Rollback to a previous deployment'

    def add_arguments(self, parser):
        parser.add_argument(
            '--deployment-id',
            type=int,
            required=True,
            help='ID of the deployment to rollback to'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Skip confirmation prompt'
        )
        parser.add_argument(
            '--timeout',
            type=int,
            default=600,
            help='Rollback timeout in seconds (default: 600)'
        )

    def handle(self, *args, **options):
        deployment_service = DeploymentService()

        try:
            deployment = deployment_service.get_deployment(options['deployment_id'])
        except Exception as e:
            raise CommandError(str(e))

        if deployment.status != Deployment.Status.SUCCESS:
            raise CommandError(
                f"Cannot rollback to deployment {deployment.id}: status is {deployment.status}"
            )

        self.stdout.write(f"\nRollback target:")
        self.stdout.write(f"  Deployment ID: {deployment.id}")
        self.stdout.write(f"  Project: {deployment.project_name}")
        self.stdout.write(f"  Target: {deployment.target.name}")
        self.stdout.write(f"  Version: {deployment.version or 'N/A'}")
        self.stdout.write(f"  Deployed at: {deployment.completed_at}")

        if not options['confirm']:
            confirm = input("\nProceed with rollback? [y/N]: ")
            if confirm.lower() != 'y':
                self.stdout.write(self.style.WARNING('Rollback cancelled.'))
                return

        self.stdout.write("\nStarting rollback...")

        try:
            rollback = deployment_service.rollback(
                deployment=deployment,
                deployed_by='cli',
                timeout=options['timeout'],
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"\n✓ Rollback successful!\n"
                    f"  Rollback ID: {rollback.id}\n"
                    f"  Status: {rollback.status}\n"
                    f"  Duration: {rollback.duration:.1f}s"
                )
            )

        except Exception as e:
            raise CommandError(f"Rollback failed: {e}")
