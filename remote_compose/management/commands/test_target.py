"""
Management command to test target connection.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.services import TargetService


class Command(BaseCommand):
    help = "Test SSH connection to a deployment target"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Target name to test")

    def handle(self, *args, **options):
        service = TargetService()

        try:
            target = service.get_target_by_name(options["name"])
        except Exception as e:
            raise CommandError(str(e))

        self.stdout.write(f"Testing connection to {target.name} ({target.host})...")

        result = service.test_connection(target)

        if result["success"]:
            self.stdout.write(
                self.style.SUCCESS(f"✓ Connection successful: {result['message']}")
            )
        else:
            self.stdout.write(
                self.style.ERROR(f"✗ Connection failed: {result['message']}")
            )
