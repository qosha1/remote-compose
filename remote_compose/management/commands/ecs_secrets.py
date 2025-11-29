"""
Management command to manage AWS Secrets Manager secrets for ECS deployments.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster


class Command(BaseCommand):
    help = 'Manage AWS Secrets Manager secrets for ECS deployments'

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest='action', help='Action to perform')

        # Push action
        push_parser = subparsers.add_parser('push', help='Push env file values to Secrets Manager')
        push_parser.add_argument('--cluster', required=True, help='ECS cluster name')
        push_parser.add_argument(
            '--env-file',
            required=True,
            help='Path to env file with secrets'
        )

        # List action
        list_parser = subparsers.add_parser('list', help='List managed secrets')
        list_parser.add_argument('--cluster', required=True, help='ECS cluster name')

        # Show action
        show_parser = subparsers.add_parser('show', help='Show secret keys (not values)')
        show_parser.add_argument('--cluster', required=True, help='ECS cluster name')
        show_parser.add_argument('--name', help='Secret name to show')

    def handle(self, *args, **options):
        action = options.get('action')
        if not action:
            self.stdout.write("Usage: ecs_secrets {push|list|show}")
            return

        if action == 'push':
            self._handle_push(options)
        elif action == 'list':
            self._handle_list(options)
        elif action == 'show':
            self._handle_show(options)

    def _get_cluster(self, options):
        try:
            return ECSCluster.objects.get(name=options['cluster'])
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {options['cluster']}")

    def _handle_push(self, options):
        cluster = self._get_cluster(options)
        env_file = options['env_file']

        if not os.path.exists(env_file):
            raise CommandError(f"Env file not found: {env_file}")

        from remote_compose.services.secrets_service import SecretsService

        secrets_service = SecretsService()

        self.stdout.write(f"Pushing secrets from {env_file} to cluster {cluster.name}...")

        try:
            arns = secrets_service.push_env_file(
                cluster=cluster,
                env_file_path=env_file,
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )

            self.stdout.write(self.style.SUCCESS(f"Pushed {len(arns)} secrets:"))
            for name, arn in sorted(arns.items()):
                self.stdout.write(f"  {name} -> {arn}")

        except Exception as e:
            raise CommandError(f"Failed to push secrets: {e}")

    def _handle_list(self, options):
        cluster = self._get_cluster(options)

        from remote_compose.models import SecretConfig

        secrets = SecretConfig.objects.filter(cluster=cluster).order_by('env_var_name')

        if not secrets.exists():
            self.stdout.write("No managed secrets found.")
            return

        self.stdout.write(f"Managed secrets for cluster {cluster.name}:")
        self.stdout.write("")
        self.stdout.write(f"{'Env Variable':<30} {'Secret Name':<50} {'Source'}")
        self.stdout.write("-" * 100)

        for secret in secrets:
            self.stdout.write(
                f"{secret.env_var_name:<30} {secret.secret_name:<50} "
                f"{secret.source_file or '-'}"
            )

        self.stdout.write("")
        self.stdout.write(f"Total: {secrets.count()} secrets")

    def _handle_show(self, options):
        cluster = self._get_cluster(options)

        from remote_compose.services.secrets_service import SecretsService

        secrets_service = SecretsService()

        try:
            managed = secrets_service.list_managed_secrets(
                cluster=cluster,
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )

            if not managed:
                self.stdout.write("No secrets found in AWS Secrets Manager.")
                return

            name_filter = options.get('name')

            self.stdout.write(f"Secrets in cluster {cluster.name}:")
            self.stdout.write("")

            for secret in managed:
                if name_filter and name_filter not in secret.get('name', ''):
                    continue
                self.stdout.write(f"  Name: {secret.get('name', 'unknown')}")
                self.stdout.write(f"  ARN:  {secret.get('arn', 'unknown')}")
                if secret.get('keys'):
                    self.stdout.write(f"  Keys: {', '.join(secret['keys'])}")
                self.stdout.write("")

        except Exception as e:
            raise CommandError(f"Failed to list secrets: {e}")
