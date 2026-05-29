"""
Management command for ECS cluster operations.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster
from remote_compose.services import ECSService


class Command(BaseCommand):
    help = "Manage ECS clusters"

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", help="Action to perform")

        # List clusters
        list_parser = subparsers.add_parser("list", help="List ECS clusters")
        list_parser.add_argument("--region", help="Filter by AWS region")
        list_parser.add_argument(
            "--aws", action="store_true", help="List clusters from AWS"
        )

        # Create cluster
        create_parser = subparsers.add_parser("create", help="Create a new ECS cluster")
        create_parser.add_argument("name", help="Cluster name")
        create_parser.add_argument("--region", default="us-east-1", help="AWS region")
        create_parser.add_argument(
            "--launch-type",
            choices=["fargate", "ec2"],
            default="fargate",
            help="Launch type (default: fargate)",
        )

        # Import cluster
        import_parser = subparsers.add_parser(
            "import", help="Import existing AWS cluster"
        )
        import_parser.add_argument("cluster_name", help="AWS cluster name or ARN")
        import_parser.add_argument("--local-name", help="Local name for the cluster")
        import_parser.add_argument("--region", default="us-east-1", help="AWS region")

        # Show cluster
        show_parser = subparsers.add_parser("show", help="Show cluster details")
        show_parser.add_argument("name", help="Cluster name")

        # Delete cluster
        delete_parser = subparsers.add_parser("delete", help="Delete a cluster")
        delete_parser.add_argument("name", help="Cluster name")
        delete_parser.add_argument(
            "--delete-aws", action="store_true", help="Also delete in AWS"
        )
        delete_parser.add_argument("--force", action="store_true", help="Force delete")

    def handle(self, *args, **options):
        action = options.get("action")

        if not action:
            self.print_help("manage.py", "ecs_cluster")
            return

        ecs_service = ECSService()

        if action == "list":
            self.handle_list(ecs_service, options)
        elif action == "create":
            self.handle_create(ecs_service, options)
        elif action == "import":
            self.handle_import(ecs_service, options)
        elif action == "show":
            self.handle_show(ecs_service, options)
        elif action == "delete":
            self.handle_delete(ecs_service, options)

    def handle_list(self, ecs_service, options):
        if options.get("aws"):
            self.stdout.write("Listing clusters from AWS...\n")
            clusters = ecs_service.list_clusters(region=options.get("region"))

            if not clusters:
                self.stdout.write("No clusters found in AWS")
                return

            self.stdout.write(
                f"{'Name':<30} {'Status':<15} {'Services':<10} {'Tasks':<10}"
            )
            self.stdout.write("-" * 70)

            for cluster in clusters:
                self.stdout.write(
                    f"{cluster['name']:<30} {cluster['status']:<15} "
                    f"{cluster['active_services']:<10} {cluster['running_tasks']:<10}"
                )
        else:
            queryset = ECSCluster.objects.all()
            if options.get("region"):
                queryset = queryset.filter(aws_region=options["region"])

            if not queryset.exists():
                self.stdout.write("No clusters tracked locally")
                return

            self.stdout.write(
                f"{'Name':<25} {'AWS Name':<25} {'Region':<15} {'Status':<12} {'Type':<10}"
            )
            self.stdout.write("-" * 90)

            for cluster in queryset:
                self.stdout.write(
                    f"{cluster.name:<25} {cluster.aws_cluster_name:<25} "
                    f"{cluster.aws_region:<15} {cluster.status:<12} {cluster.launch_type:<10}"
                )

    def handle_create(self, ecs_service, options):
        name = options["name"]
        region = options["region"]
        launch_type = options["launch_type"]

        self.stdout.write(f"Creating ECS cluster: {name} in {region}...")

        try:
            cluster = ecs_service.create_cluster(
                name=name,
                region=region,
                capacity_providers=(
                    ["FARGATE", "FARGATE_SPOT"] if launch_type == "fargate" else None
                ),
            )

            cluster.launch_type = (
                ECSCluster.LaunchType.FARGATE
                if launch_type == "fargate"
                else ECSCluster.LaunchType.EC2
            )
            cluster.save()

            self.stdout.write(self.style.SUCCESS(f"\nCluster created: {cluster.name}"))
            self.stdout.write(f"  ARN: {cluster.aws_cluster_arn}")
            self.stdout.write(f"  Region: {cluster.aws_region}")
            self.stdout.write(f"  Launch Type: {cluster.launch_type}")

        except Exception as e:
            raise CommandError(f"Failed to create cluster: {e}")

    def handle_import(self, ecs_service, options):
        cluster_name = options["cluster_name"]
        local_name = options.get("local_name")
        region = options["region"]

        self.stdout.write(f"Importing cluster: {cluster_name}...")

        try:
            cluster = ecs_service.import_cluster(
                cluster_name_or_arn=cluster_name,
                local_name=local_name,
                region=region,
            )

            self.stdout.write(self.style.SUCCESS(f"\nCluster imported: {cluster.name}"))
            self.stdout.write(f"  ARN: {cluster.aws_cluster_arn}")
            self.stdout.write(f"  Region: {cluster.aws_region}")

        except Exception as e:
            raise CommandError(f"Failed to import cluster: {e}")

    def handle_show(self, ecs_service, options):
        try:
            cluster = ECSCluster.objects.get(name=options["name"])
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {options['name']}")

        self.stdout.write(f"\nCluster: {cluster.name}")
        self.stdout.write(f"  AWS Name: {cluster.aws_cluster_name}")
        self.stdout.write(f"  ARN: {cluster.aws_cluster_arn or 'Not registered'}")
        self.stdout.write(f"  Region: {cluster.aws_region}")
        self.stdout.write(f"  Status: {cluster.status}")
        self.stdout.write(f"  Launch Type: {cluster.launch_type}")
        self.stdout.write(f"  Managed: {cluster.is_managed}")
        self.stdout.write(f"  VPC: {cluster.vpc_id or 'Not configured'}")
        self.stdout.write(f"  Subnets: {cluster.subnet_ids or 'Not configured'}")
        self.stdout.write(
            f"  Security Groups: {cluster.security_group_ids or 'Not configured'}"
        )

        # Show services
        services = cluster.services.filter(status="active")
        if services:
            self.stdout.write(f"\n  Services ({services.count()}):")
            for service in services:
                self.stdout.write(
                    f"    - {service.name}: {service.running_count}/{service.desired_count} running"
                )

    def handle_delete(self, ecs_service, options):
        try:
            cluster = ECSCluster.objects.get(name=options["name"])
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {options['name']}")

        if not options.get("force"):
            self.stdout.write(f"About to delete cluster: {cluster.name}")
            if options.get("delete_aws"):
                self.stdout.write(
                    self.style.WARNING("This will also delete the cluster in AWS!")
                )
            confirm = input("Type 'yes' to confirm: ")
            if confirm != "yes":
                self.stdout.write("Cancelled")
                return

        try:
            ecs_service.delete_cluster(
                cluster=cluster,
                delete_in_aws=options.get("delete_aws", False),
                force=options.get("force", False),
            )
            self.stdout.write(self.style.SUCCESS(f"Cluster deleted: {options['name']}"))
        except Exception as e:
            raise CommandError(f"Failed to delete cluster: {e}")
