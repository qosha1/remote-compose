"""
Management command to deploy a Docker Compose application.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from remote_compose.services import TargetService, DeploymentService


class Command(BaseCommand):
    help = "Deploy a Docker Compose application to a remote target"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="Target name to deploy to")
        parser.add_argument(
            "--compose-file",
            "-f",
            default="docker-compose.yml",
            help="Path to docker-compose.yml file (default: docker-compose.yml)",
        )
        parser.add_argument("--project-name", "-p", help="Docker Compose project name")
        parser.add_argument("--env-file", help="Path to .env file")
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
            "--no-pull", action="store_true", help="Skip pulling images"
        )
        parser.add_argument(
            "--build", action="store_true", help="Build images before starting"
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=600,
            help="Deployment timeout in seconds (default: 600)",
        )

    def handle(self, *args, **options):
        target_service = TargetService()
        deployment_service = DeploymentService()

        # Get target
        try:
            target = target_service.get_target_by_name(options["target"])
        except Exception as e:
            raise CommandError(f"Target not found: {e}")

        # Validate compose file exists
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

        # Validate env file if provided
        env_file = options.get("env_file")
        if env_file and not os.path.exists(env_file):
            raise CommandError(f"Env file not found: {env_file}")

        self.stdout.write(f"Deploying to {target.name} ({target.host})...")
        self.stdout.write(f"Compose file: {compose_file}")

        if options.get("project_name"):
            self.stdout.write(f"Project name: {options['project_name']}")

        try:
            deployment = deployment_service.deploy(
                target=target,
                compose_file_path=compose_file,
                project_name=options.get("project_name"),
                environment=environment,
                env_file_path=env_file,
                version=options.get("version", ""),
                deployed_by="cli",
                timeout=options["timeout"],
                pull_images=not options["no_pull"],
                build_images=options["build"],
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"\n✓ Deployment successful!\n"
                    f"  ID: {deployment.id}\n"
                    f"  Status: {deployment.status}\n"
                    f"  Duration: {deployment.duration:.1f}s"
                )
            )

            if deployment.container_ids:
                self.stdout.write(f"  Containers: {len(deployment.container_ids)}")

        except Exception as e:
            raise CommandError(f"Deployment failed: {e}")
