"""
Management command to deploy to ECS.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster
from remote_compose.services import ECSDeploymentService


class Command(BaseCommand):
    help = "Deploy a Docker Compose application to AWS ECS"

    def add_arguments(self, parser):
        parser.add_argument(
            "--cluster", required=True, help="ECS cluster name to deploy to"
        )
        parser.add_argument(
            "--compose-file",
            "-f",
            default="docker-compose.yml",
            help="Path to docker-compose.yml file (default: docker-compose.yml)",
        )
        parser.add_argument(
            "--project-name",
            "-p",
            help="Project/service name (default: directory name)",
        )
        parser.add_argument(
            "--env",
            "-e",
            action="append",
            dest="env_vars",
            help="Environment variables (KEY=VALUE), can be specified multiple times",
        )
        parser.add_argument(
            "--version", default="", help="Version tag for this deployment"
        )
        parser.add_argument(
            "--desired-count",
            type=int,
            default=1,
            help="Number of tasks to run (default: 1)",
        )
        parser.add_argument("--cpu", help="CPU units (256, 512, 1024, 2048, 4096)")
        parser.add_argument("--memory", help="Memory in MB (512, 1024, 2048, etc.)")
        parser.add_argument(
            "--timeout",
            type=int,
            default=300,
            help="Timeout waiting for service stability (default: 300)",
        )
        parser.add_argument(
            "--no-wait",
            action="store_true",
            help="Do not wait for service to stabilize",
        )

    def handle(self, *args, **options):
        # Get cluster
        try:
            cluster = ECSCluster.objects.get(name=options["cluster"])
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {options['cluster']}")

        # Validate compose file
        compose_file = options["compose_file"]
        if not os.path.exists(compose_file):
            raise CommandError(f"Compose file not found: {compose_file}")

        # Parse environment variables
        environment = {}
        if options.get("env_vars"):
            for env_var in options["env_vars"]:
                if "=" not in env_var:
                    raise CommandError(
                        f"Invalid environment variable format: {env_var}"
                    )
                key, value = env_var.split("=", 1)
                environment[key] = value

        self.stdout.write(
            f"Deploying to ECS cluster: {cluster.name} ({cluster.aws_region})"
        )
        self.stdout.write(f"Compose file: {compose_file}")
        self.stdout.write(f"Launch type: {cluster.launch_type}")

        if options.get("project_name"):
            self.stdout.write(f"Project name: {options['project_name']}")

        deployment_service = ECSDeploymentService()

        try:
            deployment = deployment_service.deploy(
                cluster=cluster,
                compose_file_path=compose_file,
                project_name=options.get("project_name"),
                environment=environment if environment else None,
                version=options.get("version", ""),
                deployed_by="cli",
                desired_count=options["desired_count"],
                cpu=options.get("cpu"),
                memory=options.get("memory"),
                wait_for_stable=not options["no_wait"],
                timeout=options["timeout"],
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"\nDeployment successful!\n"
                    f"  ID: {deployment.id}\n"
                    f"  Status: {deployment.status}\n"
                    f"  Duration: {deployment.duration:.1f}s"
                )
            )

            if deployment.metadata.get("service_arn"):
                self.stdout.write(
                    f"  Service ARN: {deployment.metadata['service_arn']}"
                )
            if deployment.metadata.get("task_definition_arn"):
                self.stdout.write(
                    f"  Task Definition: {deployment.metadata['task_definition_arn']}"
                )

        except Exception as e:
            raise CommandError(f"Deployment failed: {e}")
