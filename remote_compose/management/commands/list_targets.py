"""
Management command to list deployment targets.
"""

from django.core.management.base import BaseCommand

from remote_compose.services import TargetService


class Command(BaseCommand):
    help = 'List deployment targets'

    def add_arguments(self, parser):
        parser.add_argument(
            '--environment',
            choices=['development', 'staging', 'production'],
            help='Filter by environment'
        )
        parser.add_argument(
            '--active-only',
            action='store_true',
            help='Show only active targets'
        )
        parser.add_argument(
            '--verbose',
            '-v',
            action='store_true',
            help='Show detailed information'
        )

    def handle(self, *args, **options):
        service = TargetService()

        targets = service.list_targets(
            environment=options.get('environment'),
            is_active=True if options['active_only'] else None,
        )

        if not targets.exists():
            self.stdout.write(self.style.WARNING('No targets found.'))
            return

        self.stdout.write(f"\nDeployment Targets ({targets.count()}):")
        self.stdout.write("-" * 60)

        for target in targets:
            status_icon = "✓" if target.health_status == 'healthy' else "✗" if target.health_status == 'unhealthy' else "?"
            active_icon = "" if target.is_active else " [INACTIVE]"

            self.stdout.write(
                f"\n{status_icon} {target.name}{active_icon}"
            )
            self.stdout.write(f"  Host: {target.username}@{target.host}:{target.port}")
            self.stdout.write(f"  Environment: {target.environment}")
            self.stdout.write(f"  Health: {target.health_status}")

            if options['verbose']:
                self.stdout.write(f"  Type: {target.target_type}")
                if target.aws_instance_id:
                    self.stdout.write(f"  AWS Instance: {target.aws_instance_id}")
                if target.description:
                    self.stdout.write(f"  Description: {target.description}")
                self.stdout.write(f"  Created: {target.created_at}")
                if target.last_health_check:
                    self.stdout.write(f"  Last Health Check: {target.last_health_check}")

        self.stdout.write("")
