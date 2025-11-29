"""
Management command to create a deployment target.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import DeploymentTarget
from remote_compose.services import TargetService


class Command(BaseCommand):
    help = 'Create a new deployment target'

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            required=True,
            help='Unique name for the target'
        )
        parser.add_argument(
            '--host',
            required=True,
            help='Remote host address (IP or hostname)'
        )
        parser.add_argument(
            '--user',
            default='ubuntu',
            help='SSH username (default: ubuntu)'
        )
        parser.add_argument(
            '--port',
            type=int,
            default=22,
            help='SSH port (default: 22)'
        )
        parser.add_argument(
            '--ssh-key',
            dest='ssh_key_path',
            help='Path to SSH private key file'
        )
        parser.add_argument(
            '--environment',
            choices=['development', 'staging', 'production'],
            default='development',
            help='Target environment (default: development)'
        )
        parser.add_argument(
            '--description',
            default='',
            help='Description of the target'
        )
        parser.add_argument(
            '--aws-instance-id',
            dest='aws_instance_id',
            help='AWS EC2 instance ID (optional)'
        )
        parser.add_argument(
            '--aws-region',
            dest='aws_region',
            help='AWS region (optional)'
        )
        parser.add_argument(
            '--no-validate',
            action='store_true',
            help='Skip SSH connection validation'
        )

    def handle(self, *args, **options):
        service = TargetService()

        try:
            # Map environment string to enum
            env_map = {
                'development': DeploymentTarget.Environment.DEVELOPMENT,
                'staging': DeploymentTarget.Environment.STAGING,
                'production': DeploymentTarget.Environment.PRODUCTION,
            }

            target = service.create_target(
                name=options['name'],
                host=options['host'],
                username=options['user'],
                port=options['port'],
                ssh_key_path=options.get('ssh_key_path'),
                environment=env_map[options['environment']],
                description=options.get('description', ''),
                aws_instance_id=options.get('aws_instance_id'),
                aws_region=options.get('aws_region'),
                validate_connection=not options['no_validate'],
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Created deployment target: {target.name} ({target.host})"
                )
            )

            if options['no_validate']:
                self.stdout.write(
                    self.style.WARNING(
                        "Connection was not validated. Run 'python manage.py test_target --name {}' to verify.".format(target.name)
                    )
                )

        except Exception as e:
            raise CommandError(str(e))
