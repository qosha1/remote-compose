"""
Management command to tear down ECS services and optionally infrastructure.
"""

from django.core.management.base import BaseCommand, CommandError

from remote_compose.models import ECSCluster


class Command(BaseCommand):
    help = "Tear down ECS services and optionally infrastructure"

    def add_arguments(self, parser):
        parser.add_argument("--cluster", required=True, help="ECS cluster name")
        parser.add_argument(
            "--services-only",
            action="store_true",
            help="Only tear down ECS services (preserve infrastructure)",
        )
        parser.add_argument(
            "--include-infrastructure",
            action="store_true",
            help="Also destroy VPC, ALB, security groups, etc.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required flag to confirm destructive operation",
        )
        parser.add_argument(
            "--project-name", "-p", help="Only tear down services for this project"
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "This is a destructive operation. " "Add --confirm flag to proceed."
            )

        try:
            cluster = ECSCluster.objects.get(name=options["cluster"])
        except ECSCluster.DoesNotExist:
            raise CommandError(f"Cluster not found: {options['cluster']}")

        self.stdout.write(
            self.style.WARNING(f"Tearing down resources for cluster: {cluster.name}")
        )

        # Tear down ECS services
        self._teardown_services(cluster, options.get("project_name"))

        # Tear down infrastructure if requested
        if options["include_infrastructure"] and not options["services_only"]:
            self._teardown_infrastructure(cluster)

        self.stdout.write(self.style.SUCCESS("Teardown complete."))

    def _teardown_services(self, cluster, project_name=None):
        """Tear down ECS services."""
        from remote_compose.models import ECSService as ECSServiceModel
        from remote_compose.services.ecs_service import ECSService

        ecs_logic = ECSService()

        services = ECSServiceModel.objects.filter(cluster=cluster)
        if project_name:
            services = services.filter(name__startswith=f"{project_name}-")

        if not services.exists():
            self.stdout.write("  No services to tear down.")
            return

        self.stdout.write(f"  Tearing down {services.count()} services...")

        for svc in services:
            try:
                self.stdout.write(f"    Scaling down: {svc.name}")
                if svc.aws_service_arn:
                    try:
                        ecs_logic.scale_service(
                            cluster=cluster,
                            service_name=svc.name,
                            desired_count=0,
                        )
                    except Exception:
                        pass

                    try:
                        self.stdout.write(f"    Deleting service: {svc.name}")
                        ecs_logic.delete_service(
                            cluster=cluster,
                            service_name=svc.name,
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.WARNING(f"    Could not delete AWS service: {e}")
                        )

                svc.delete()
                self.stdout.write(self.style.SUCCESS(f"    Removed: {svc.name}"))

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"    Failed to remove {svc.name}: {e}")
                )

        # Clean up target groups
        from remote_compose.models import TargetGroupConfig

        tgs = TargetGroupConfig.objects.filter(cluster=cluster)
        if project_name:
            tgs = tgs.filter(target_group_name__startswith=project_name[:10])

        if tgs.exists():
            self.stdout.write(f"  Removing {tgs.count()} target groups...")
            from remote_compose.services.aws_client_factory import (
                get_aws_client_factory,
            )

            factory = get_aws_client_factory()
            elbv2 = factory.get_client(
                "elbv2",
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )

            for tg in tgs:
                try:
                    elbv2.delete_target_group(TargetGroupArn=tg.target_group_arn)
                    tg.delete()
                    self.stdout.write(f"    Removed TG: {tg.target_group_name}")
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"    Could not delete TG: {e}")
                    )

    def _teardown_infrastructure(self, cluster):
        """Tear down VPC, ALB, security groups, and other infrastructure."""
        self.stdout.write("  Tearing down infrastructure...")

        # Tear down ALB
        try:
            lb = cluster.load_balancer
            if lb:
                self.stdout.write(f"    Deleting ALB: {lb.alb_dns_name}")
                from remote_compose.services.aws_client_factory import (
                    get_aws_client_factory,
                )

                factory = get_aws_client_factory()
                elbv2 = factory.get_client(
                    "elbv2",
                    region=cluster.aws_region,
                    credential=cluster.aws_credential,
                )

                # Delete listeners first
                for arn in [lb.http_listener_arn, lb.https_listener_arn]:
                    if arn:
                        try:
                            elbv2.delete_listener(ListenerArn=arn)
                        except Exception:
                            pass

                # Delete ALB
                try:
                    elbv2.delete_load_balancer(LoadBalancerArn=lb.alb_arn)
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"    Could not delete ALB: {e}")
                    )

                lb.delete()
                self.stdout.write(self.style.SUCCESS("    ALB removed"))
        except Exception:
            pass

        # Tear down Service Connect namespace
        try:
            ns = cluster.service_connect_namespace
            if ns:
                self.stdout.write(f"    Deleting namespace: {ns.namespace_name}")
                from remote_compose.services.aws_client_factory import (
                    get_aws_client_factory,
                )

                factory = get_aws_client_factory()
                sd = factory.get_client(
                    "servicediscovery",
                    region=cluster.aws_region,
                    credential=cluster.aws_credential,
                )

                try:
                    sd.delete_namespace(Id=ns.namespace_id)
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"    Could not delete namespace: {e}")
                    )

                ns.delete()
                self.stdout.write(self.style.SUCCESS("    Namespace removed"))
        except Exception:
            pass

        # Tear down security groups
        from remote_compose.models import SecurityGroupConfig

        sgs = SecurityGroupConfig.objects.filter(cluster=cluster)
        if sgs.exists():
            self.stdout.write(f"    Deleting {sgs.count()} security groups...")
            from remote_compose.services.aws_client_factory import (
                get_aws_client_factory,
            )

            factory = get_aws_client_factory()
            ec2 = factory.get_client(
                "ec2",
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )

            for sg in sgs:
                try:
                    ec2.delete_security_group(GroupId=sg.security_group_id)
                    sg.delete()
                    self.stdout.write(f"      Removed SG: {sg.security_group_id}")
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(
                            f"      Could not delete SG {sg.security_group_id}: {e}"
                        )
                    )

        # Tear down VPC
        try:
            vpc = cluster.vpc_infrastructure
            if vpc and vpc.is_managed:
                self.stdout.write(f"    Deleting VPC: {vpc.vpc_id}")
                from remote_compose.services.vpc_service import VPCService

                vpc_service = VPCService()
                try:
                    vpc_service.teardown_vpc(vpc)
                    self.stdout.write(self.style.SUCCESS("    VPC removed"))
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"    Could not fully tear down VPC: {e}")
                    )
        except Exception:
            pass

        # Clean up secrets
        from remote_compose.models import SecretConfig

        secrets = SecretConfig.objects.filter(cluster=cluster)
        if secrets.exists():
            self.stdout.write(f"    Deleting {secrets.count()} managed secrets...")
            from remote_compose.services.aws_client_factory import (
                get_aws_client_factory,
            )

            factory = get_aws_client_factory()
            sm = factory.get_client(
                "secretsmanager",
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )

            for secret in secrets:
                try:
                    sm.delete_secret(
                        SecretId=secret.secret_arn,
                        ForceDeleteWithoutRecovery=True,
                    )
                    secret.delete()
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"      Could not delete secret: {e}")
                    )

            self.stdout.write(self.style.SUCCESS("    Secrets removed"))
