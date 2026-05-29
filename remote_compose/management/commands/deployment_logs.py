"""
Management command to view deployment logs.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.services import DeploymentService
from remote_compose.models import Deployment


class Command(BaseCommand):
    help = "View logs for a deployment"

    def add_arguments(self, parser):
        parser.add_argument(
            "--deployment-id", type=int, required=True, help="Deployment ID"
        )
        parser.add_argument("--service", help="Specific service to show logs for")
        parser.add_argument(
            "--tail",
            type=int,
            default=100,
            help="Number of lines to show (default: 100)",
        )
        parser.add_argument(
            "--internal",
            action="store_true",
            help="Show internal deployment logs instead of container logs",
        )

    def handle(self, *args, **options):
        deployment_service = DeploymentService()

        try:
            deployment = deployment_service.get_deployment(options["deployment_id"])
        except Exception as e:
            raise CommandError(str(e))

        if options["internal"]:
            # Show internal deployment logs
            self.stdout.write(f"\nDeployment Logs for #{deployment.id}:")
            self.stdout.write("-" * 60)

            for log in deployment.logs.all()[: options["tail"]]:
                level_colors = {
                    "info": self.style.SUCCESS,
                    "warning": self.style.WARNING,
                    "error": self.style.ERROR,
                    "debug": lambda x: x,
                }
                color = level_colors.get(log.log_level, lambda x: x)

                self.stdout.write(
                    f"[{log.timestamp}] {color(log.log_level.upper())}: {log.message}"
                )
                if log.command:
                    self.stdout.write(f"  Command: {log.command}")
                if log.output:
                    self.stdout.write(f"  Output: {log.output[:200]}...")

        else:
            # Show container logs from remote host
            if deployment.status not in [
                Deployment.Status.SUCCESS,
                Deployment.Status.RUNNING,
            ]:
                raise CommandError(
                    f"Cannot get container logs: deployment status is {deployment.status}"
                )

            self.stdout.write(f"\nContainer Logs for #{deployment.id}:")
            if options.get("service"):
                self.stdout.write(f"Service: {options['service']}")
            self.stdout.write("-" * 60)

            try:
                logs = deployment_service.get_logs(
                    deployment=deployment,
                    service=options.get("service"),
                    tail=options["tail"],
                )
                self.stdout.write(logs)

            except Exception as e:
                raise CommandError(f"Failed to get logs: {e}")
