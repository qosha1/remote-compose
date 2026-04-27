"""
Management command for ECS service operations.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster, ECSService as ECSServiceModel
from remote_compose.services import ECSService as ECSServiceAPI, ECSDeploymentService


class Command(BaseCommand):
    help = 'Manage ECS services'

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest='action', help='Action to perform')

        # List services
        list_parser = subparsers.add_parser('list', help='List ECS services')
        list_parser.add_argument('--cluster', help='Filter by cluster name')

        # Show service
        show_parser = subparsers.add_parser('show', help='Show service details')
        show_parser.add_argument('name', help='Service name')
        show_parser.add_argument('--cluster', required=True, help='Cluster name')

        # Scale service
        scale_parser = subparsers.add_parser('scale', help='Scale a service')
        scale_parser.add_argument('name', help='Service name')
        scale_parser.add_argument('--cluster', required=True, help='Cluster name')
        scale_parser.add_argument('--count', type=int, required=True, help='Desired task count')
        scale_parser.add_argument('--no-wait', action='store_true', help='Do not wait for scaling')

        # Restart service
        restart_parser = subparsers.add_parser('restart', help='Force new deployment')
        restart_parser.add_argument('name', help='Service name')
        restart_parser.add_argument('--cluster', required=True, help='Cluster name')
        restart_parser.add_argument('--no-wait', action='store_true', help='Do not wait')

        # Delete service
        delete_parser = subparsers.add_parser('delete', help='Delete a service')
        delete_parser.add_argument('name', help='Service name')
        delete_parser.add_argument('--cluster', required=True, help='Cluster name')
        delete_parser.add_argument('--force', action='store_true', help='Force delete')

        # Logs
        logs_parser = subparsers.add_parser('logs', help='Show recent deployment logs')
        logs_parser.add_argument('name', help='Service name')
        logs_parser.add_argument('--cluster', required=True, help='Cluster name')
        logs_parser.add_argument('--limit', type=int, default=50, help='Number of log entries')

    def handle(self, *args, **options):
        action = options.get('action')

        if not action:
            self.print_help('manage.py', 'ecs_service')
            return

        if action == 'list':
            self.handle_list(options)
        elif action == 'show':
            self.handle_show(options)
        elif action == 'scale':
            self.handle_scale(options)
        elif action == 'restart':
            self.handle_restart(options)
        elif action == 'delete':
            self.handle_delete(options)
        elif action == 'logs':
            self.handle_logs(options)

    def _get_service(self, name, cluster_name):
        try:
            cluster = ECSCluster.objects.get(name=cluster_name)
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {cluster_name}")

        try:
            return ECSServiceModel.objects.get(name=name, cluster=cluster)
        except ECSServiceModel.DoesNotExist:
            raise CommandError(f"Service not found: {name} in cluster {cluster_name}")

    def handle_list(self, options):
        queryset = ECSServiceModel.objects.all()

        if options.get('cluster'):
            queryset = queryset.filter(cluster__name=options['cluster'])

        if not queryset.exists():
            self.stdout.write("No services found")
            return

        self.stdout.write(f"{'Service':<25} {'Cluster':<20} {'Status':<12} {'Running':<10} {'Desired':<10}")
        self.stdout.write("-" * 80)

        for service in queryset:
            self.stdout.write(
                f"{service.name:<25} {service.cluster.name:<20} "
                f"{service.status:<12} {service.running_count:<10} {service.desired_count:<10}"
            )

    def handle_show(self, options):
        service = self._get_service(options['name'], options['cluster'])

        deployment_service = ECSDeploymentService()

        try:
            status = deployment_service.get_service_status(service)

            self.stdout.write(f"\nService: {status['service_name']}")
            self.stdout.write(f"  Cluster: {status['cluster']}")
            self.stdout.write(f"  Status: {status['status']}")
            self.stdout.write(f"  Healthy: {status['is_healthy']}")
            self.stdout.write(f"  Task Definition: {status['task_definition']}")
            self.stdout.write(f"  Desired Count: {status['desired_count']}")
            self.stdout.write(f"  Running Count: {status['running_count']}")
            self.stdout.write(f"  Pending Count: {status['pending_count']}")

            if status.get('last_deployment'):
                self.stdout.write(f"  Last Deployment: {status['last_deployment']}")

            if status.get('tasks'):
                self.stdout.write(f"\n  Tasks ({len(status['tasks'])}):")
                for task in status['tasks']:
                    task_id = task['task_arn'].split('/')[-1][:12]
                    self.stdout.write(
                        f"    - {task_id}: {task['status']} "
                        f"(health: {task['health']})"
                    )

        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Could not fetch live status: {e}"))
            self.stdout.write(f"\nLocal data:")
            self.stdout.write(f"  Status: {service.status}")
            self.stdout.write(f"  Desired: {service.desired_count}")
            self.stdout.write(f"  Running: {service.running_count}")

    def handle_scale(self, options):
        service = self._get_service(options['name'], options['cluster'])
        count = options['count']

        self.stdout.write(
            f"Scaling {service.name} from {service.desired_count} to {count} tasks..."
        )

        deployment_service = ECSDeploymentService()

        try:
            service = deployment_service.scale(
                ecs_service=service,
                desired_count=count,
                wait_for_stable=not options['no_wait'],
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Scaled successfully: {service.running_count}/{service.desired_count} running"
                )
            )

        except Exception as e:
            raise CommandError(f"Scaling failed: {e}")

    def handle_restart(self, options):
        service = self._get_service(options['name'], options['cluster'])

        self.stdout.write(f"Forcing new deployment for {service.name}...")

        ecs_api = ECSServiceAPI()

        try:
            service = ecs_api.update_service(
                ecs_service=service,
                force_new_deployment=True,
            )

            if not options['no_wait']:
                self.stdout.write("Waiting for deployment to stabilize...")
                service = ecs_api.wait_for_service_stable(service)

            self.stdout.write(
                self.style.SUCCESS(
                    f"Restart complete: {service.running_count} tasks running"
                )
            )

        except Exception as e:
            raise CommandError(f"Restart failed: {e}")

    def handle_delete(self, options):
        service = self._get_service(options['name'], options['cluster'])

        if not options.get('force'):
            self.stdout.write(f"About to delete service: {service.name}")
            confirm = input("Type 'yes' to confirm: ")
            if confirm != 'yes':
                self.stdout.write("Cancelled")
                return

        self.stdout.write(f"Deleting service {service.name}...")

        ecs_api = ECSServiceAPI()

        try:
            ecs_api.delete_service(
                ecs_service=service,
                force=options.get('force', False),
            )
            self.stdout.write(self.style.SUCCESS(f"Service deleted: {service.name}"))

        except Exception as e:
            raise CommandError(f"Delete failed: {e}")

    def handle_logs(self, options):
        service = self._get_service(options['name'], options['cluster'])

        from remote_compose.models import DeploymentLog

        deployments = service.deployments.all()[:5]

        if not deployments:
            self.stdout.write("No deployment logs found")
            return

        for deployment in deployments:
            self.stdout.write(f"\n--- Deployment {deployment.id} ({deployment.status}) ---")
            self.stdout.write(f"Started: {deployment.created_at}")

            logs = DeploymentLog.objects.filter(
                deployment=deployment
            ).order_by('timestamp')[:options['limit']]

            for log in logs:
                # remote-compose-mps: model field is `log_level`, not `level`.
                level_style = {
                    'info': lambda x: x,
                    'warning': self.style.WARNING,
                    'error': self.style.ERROR,
                }.get(log.log_level, lambda x: x)

                self.stdout.write(
                    f"  [{log.timestamp.strftime('%H:%M:%S')}] {level_style(log.message)}"
                )
