"""
Multi-service deployment pipeline steps.

Handles deployment ordering, shared image detection, per-service
task definition creation, service creation with Service Connect,
and multi-service stability waiting.
"""

import time
import yaml

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class LoadServiceConfigStep(PipelineStep):
    """
    Load per-service configuration from a YAML file.

    Populates context.service_resources, context.service_counts,
    and context.public_services from the service config file.
    """

    def __init__(self):
        super().__init__("LoadServiceConfig")

    def should_run(self, context: PipelineContext) -> bool:
        return bool(context.service_config_path)

    def execute(self, context: PipelineContext) -> StepResult:
        try:
            from pathlib import Path
            config_path = Path(context.service_config_path)

            if not config_path.exists():
                return StepResult.fail(
                    f"Service config file not found: {context.service_config_path}"
                )

            config = yaml.safe_load(config_path.read_text())
            services_config = config.get('services', {})

            for svc_name, svc_conf in services_config.items():
                # Resource overrides
                context.service_resources[svc_name] = {
                    'cpu': svc_conf.get('cpu'),
                    'memory': svc_conf.get('memory'),
                    'type': svc_conf.get('type', 'application'),
                }

                # Desired count
                if 'desired_count' in svc_conf:
                    context.service_counts[svc_name] = svc_conf['desired_count']

                # Public services
                if svc_conf.get('public'):
                    context.public_services[svc_name] = {
                        'port': svc_conf.get('port', 80),
                        'health_check_path': svc_conf.get('health_check_path', '/health'),
                        'default_target': svc_conf.get('default_target', False),
                    }

            return StepResult.ok(
                f"Loaded config for {len(services_config)} services"
            )

        except Exception as e:
            return StepResult.fail(
                f"Failed to load service config: {e}", error=e
            )


class DetermineServiceOrderStep(PipelineStep):
    """
    Determine deployment order using topological sort of depends_on.
    """

    def __init__(self):
        super().__init__("DetermineServiceOrder")

    def execute(self, context: PipelineContext) -> StepResult:
        if not context.preprocessed:
            return StepResult.fail("No preprocessed compose data available")

        from ...compose_preprocessor import ComposePreprocessor

        preprocessor = ComposePreprocessor()

        try:
            order = preprocessor.get_deployment_order(context.preprocessed)
            context.service_order = order

            return StepResult.ok(
                f"Deployment order: {' -> '.join(order)}"
            )

        except Exception as e:
            return StepResult.fail(
                f"Failed to determine deployment order: {e}", error=e
            )


class DetectSharedImagesStep(PipelineStep):
    """
    Detect services that share the same Docker build context.

    Groups services by build context key (context path + dockerfile)
    so we only build each unique image once.
    """

    def __init__(self):
        super().__init__("DetectSharedImages")

    def should_run(self, context: PipelineContext) -> bool:
        return context.build_images and context.has_build_services

    def execute(self, context: PipelineContext) -> StepResult:
        import os

        build_services = context.preprocessed.get_build_services()
        build_groups = {}  # build_key -> [service_names]

        for svc_name, svc in build_services.items():
            if not svc.build_info:
                continue

            ctx_path = os.path.normpath(svc.build_info.context)
            dockerfile = svc.build_info.dockerfile
            build_key = f"{ctx_path}:{dockerfile}"

            if build_key not in build_groups:
                build_groups[build_key] = []
            build_groups[build_key].append(svc_name)

        # Initialize shared_images keys (values will be set during build)
        shared_count = 0
        for build_key, services in build_groups.items():
            if len(services) > 1:
                # Mark this as a shared build - first service is the "primary"
                context.shared_images[build_key] = ''  # URI set during build
                shared_count += 1

        if shared_count > 0:
            details = []
            for build_key, services in build_groups.items():
                if len(services) > 1:
                    details.append(f"{', '.join(services)} share {build_key}")
            return StepResult.ok(
                f"Detected {shared_count} shared image group(s): "
                f"{'; '.join(details)}"
            )

        return StepResult.ok("No shared images detected")


class ConvertToTaskDefinitionsStep(PipelineStep):
    """
    Convert preprocessed compose to per-service ECS task definitions.

    Creates one task definition per compose service.
    """

    def __init__(self):
        super().__init__("ConvertToTaskDefinitions")

    def execute(self, context: PipelineContext) -> StepResult:
        from ...compose_converter import ComposeToECSConverter

        converter = ComposeToECSConverter()

        try:
            task_defs = converter.convert_per_service(
                preprocessed=context.preprocessed,
                cluster=context.cluster,
                project_name=context.project_name,
                efs_config=context.efs_config,
                service_resources=context.service_resources,
                secrets_arns=context.secrets_arns,
                shared_images=context.shared_images,
                strict_mode=context.strict_mode,
                infrastructure_env=context.infrastructure_env,
            )
            for td in task_defs.values():
                td.save()
        except Exception as e:
            return StepResult.fail(
                f"Task definition conversion failed: {e}", error=e
            )

        for warning in converter.warnings:
            context.add_warning(warning)

        context.task_definitions = task_defs

        details = []
        for name, td in task_defs.items():
            details.append(f"{name}(CPU:{td.cpu},Mem:{td.memory})")

        return StepResult.ok(
            f"Created {len(task_defs)} task definitions: "
            f"{', '.join(details)}"
        )


class RegisterTaskDefinitionsStep(PipelineStep):
    """
    Register all per-service task definitions with AWS ECS.
    """

    def __init__(self):
        super().__init__("RegisterTaskDefinitions")

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok(
                f"[DRY RUN] Would register {len(context.task_definitions)} "
                f"task definitions"
            )

        ecs_service = context.services.ecs
        registered = 0

        for svc_name, task_def in context.task_definitions.items():
            try:
                registered_td = ecs_service.register_task_definition(task_def)
                context.task_definitions[svc_name] = registered_td

                context.track_resource(
                    resource_type='ecs_task_definition',
                    resource_id=registered_td.aws_task_definition_arn,
                    family=registered_td.name,
                    revision=registered_td.revision,
                )
                registered += 1

                self.emit_event(
                    'task_definition_registered',
                    service=svc_name,
                    arn=registered_td.aws_task_definition_arn,
                )

            except Exception as e:
                return StepResult.fail(
                    f"Task definition registration failed for "
                    f"'{svc_name}': {e}",
                    error=e,
                )

        return StepResult.ok(
            f"Registered {registered} task definitions"
        )


class CreateTargetGroupsStep(PipelineStep):
    """
    Create ALB target groups for public-facing services.
    """

    def __init__(self):
        super().__init__("CreateTargetGroups")

    def should_run(self, context: PipelineContext) -> bool:
        return bool(context.public_services) and context.load_balancer is not None

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return StepResult.ok(
                f"[DRY RUN] Would create {len(context.public_services)} "
                f"target groups"
            )

        if not context.vpc_infrastructure:
            return StepResult.fail("No VPC infrastructure for target groups")

        alb_service = context.services.alb
        created = 0

        for svc_name, svc_config in context.public_services.items():
            try:
                tg = alb_service.create_target_group(
                    cluster=context.cluster,
                    vpc_id=context.vpc_infrastructure.vpc_id,
                    service_name=svc_name,
                    port=svc_config['port'],
                    health_check_path=svc_config.get('health_check_path', '/health'),
                    region=context.cluster.aws_region,
                    credential=context.cluster.aws_credential,
                )

                context.target_groups[svc_name] = tg

                # If this is the default target, create/update listener rule
                if svc_config.get('default_target'):
                    listener_arn = (
                        context.load_balancer.https_listener_arn
                        or context.load_balancer.http_listener_arn
                    )
                    if listener_arn:
                        alb_service.create_listener_rule(
                            listener_arn=listener_arn,
                            target_group_arn=tg.target_group_arn,
                            priority=1,
                            region=context.cluster.aws_region,
                            credential=context.cluster.aws_credential,
                        )

                created += 1

            except Exception as e:
                return StepResult.fail(
                    f"Target group creation failed for '{svc_name}': {e}",
                    error=e,
                )

        return StepResult.ok(f"Created {created} target groups")


class CreateOrUpdateMultiServiceStep(PipelineStep):
    """
    Create or update ECS services in dependency order with Service Connect.
    """

    def __init__(self):
        super().__init__("CreateOrUpdateMultiService")
        self._created_services = []

    def execute(self, context: PipelineContext) -> StepResult:
        self._created_services = []
        if context.dry_run:
            order = context.service_order or list(context.task_definitions.keys())
            return StepResult.ok(
                f"[DRY RUN] Would create/update {len(order)} services "
                f"in order: {' -> '.join(order)}"
            )

        from ....models import ECSService as ECSServiceModel

        ecs_logic = context.services.ecs
        deployment_order = context.service_order or list(
            context.task_definitions.keys()
        )

        created = 0
        updated = 0

        for svc_name in deployment_order:
            task_def = context.task_definitions.get(svc_name)
            if not task_def:
                context.add_warning(
                    f"No task definition for service '{svc_name}', skipping"
                )
                continue

            try:
                # Determine desired count
                desired_count = context.service_counts.get(
                    svc_name, context.desired_count
                )

                # Determine service type
                svc_resources = context.service_resources.get(svc_name, {})
                service_type = svc_resources.get('type', 'application')

                # Check for existing service
                service_model = ECSServiceModel.objects.filter(
                    cluster=context.cluster,
                    name=f"{context.project_name}-{svc_name}",
                ).first()

                if service_model:
                    # Update existing
                    service_model.task_definition = task_def
                    service_model.desired_count = desired_count
                    service_model.service_type = service_type
                    service_model.save()

                    if service_model.aws_service_arn:
                        ecs_logic.update_service(
                            ecs_service=service_model,
                            task_definition=task_def,
                            desired_count=desired_count,
                            force_new_deployment=True,
                        )
                        updated += 1
                    else:
                        self._configure_service_connect(
                            service_model, context, svc_name, task_def
                        )
                        self._configure_load_balancer(
                            service_model, context, svc_name, task_def
                        )
                        ecs_logic.create_service(service_model)
                        self._created_services.append(svc_name)
                        created += 1
                else:
                    # Create new
                    service_model = ECSServiceModel(
                        name=f"{context.project_name}-{svc_name}",
                        cluster=context.cluster,
                        task_definition=task_def,
                        desired_count=desired_count,
                        service_type=service_type,
                        status=ECSServiceModel.ServiceStatus.PENDING,
                    )

                    self._configure_service_connect(
                        service_model, context, svc_name, task_def
                    )
                    self._configure_load_balancer(
                        service_model, context, svc_name, task_def
                    )

                    service_model.save()
                    ecs_logic.create_service(service_model)
                    self._created_services.append(svc_name)
                    created += 1

                context.ecs_services[svc_name] = service_model

                context.track_resource(
                    resource_type='ecs_service',
                    resource_id=service_model.aws_service_arn or svc_name,
                    name=service_model.name,
                )

                self.emit_event(
                    'service_deployed',
                    service=svc_name,
                    action='created' if svc_name in self._created_services else 'updated',
                )

            except Exception as e:
                return StepResult.fail(
                    f"Service create/update failed for '{svc_name}': {e}",
                    error=e,
                )

        return StepResult.ok(
            f"Services deployed: {created} created, {updated} updated"
        )

    def _configure_service_connect(self, service_model, context, svc_name, task_def):
        """Configure Service Connect on a service model."""
        if not context.service_connect_namespace:
            return

        service_model.service_connect_enabled = True
        service_model.service_connect_namespace = (
            context.service_connect_namespace.namespace_name
        )

        # Set port name from container port mappings
        if task_def.container_definitions:
            container = task_def.container_definitions[0]
            port_mappings = container.get('portMappings', [])
            if port_mappings:
                port = port_mappings[0].get('containerPort')
                service_model.service_connect_port_name = svc_name
                service_model.container_name_for_lb = container['name']
                service_model.container_port_for_lb = port

    def _configure_load_balancer(self, service_model, context, svc_name, task_def):
        """Configure load balancer target group for public services."""
        if svc_name not in context.public_services:
            return

        tg = context.target_groups.get(svc_name)
        if not tg:
            return

        service_model.load_balancer_arn = tg.target_group_arn

        if task_def.container_definitions:
            container = task_def.container_definitions[0]
            service_model.container_name_for_lb = container['name']
            port_mappings = container.get('portMappings', [])
            if port_mappings:
                service_model.container_port_for_lb = port_mappings[0].get(
                    'containerPort'
                )

    def cleanup(self, context: PipelineContext) -> None:
        """Scale down and delete newly created services on rollback."""
        if not self._created_services:
            return

        ecs_logic = context.services.ecs

        for svc_name in reversed(self._created_services):
            svc_model = context.ecs_services.get(svc_name)
            if not svc_model:
                continue

            try:
                ecs_logic.scale_service(
                    cluster=context.cluster,
                    service_name=svc_model.name,
                    desired_count=0,
                )
                ecs_logic.delete_service(
                    cluster=context.cluster,
                    service_name=svc_model.name,
                )
                svc_model.delete()
            except Exception:
                pass


class WaitForAllServicesStableStep(PipelineStep):
    """
    Wait for all ECS services to reach stable state.

    Polls each service and reports progress.
    """

    def __init__(self):
        super().__init__("WaitForAllServicesStable")

    def should_run(self, context: PipelineContext) -> bool:
        return context.wait_for_stable and not context.dry_run

    def execute(self, context: PipelineContext) -> StepResult:
        ecs_logic = context.services.ecs
        deployment_order = context.service_order or list(
            context.ecs_services.keys()
        )

        timeout = context.timeout
        start = time.time()

        for svc_name in deployment_order:
            svc_model = context.ecs_services.get(svc_name)
            if not svc_model or not svc_model.aws_service_arn:
                continue

            elapsed = time.time() - start
            remaining = max(30, timeout - int(elapsed))

            self.emit_event(
                'waiting_for_service',
                service=svc_name,
                remaining_timeout=remaining,
            )

            try:
                updated_model = ecs_logic.wait_for_service_stable(
                    ecs_service=svc_model,
                    timeout=remaining,
                )
                context.ecs_services[svc_name] = updated_model

                self.emit_event(
                    'service_stable',
                    service=svc_name,
                    running=updated_model.running_count,
                    desired=updated_model.desired_count,
                )

            except Exception as e:
                return StepResult.fail(
                    f"Service '{svc_name}' did not stabilize: {e}",
                    error=e,
                )

        # Build summary
        total_running = sum(
            s.running_count for s in context.ecs_services.values()
            if hasattr(s, 'running_count')
        )
        total_desired = sum(
            s.desired_count for s in context.ecs_services.values()
            if hasattr(s, 'desired_count')
        )

        elapsed = time.time() - start
        return StepResult.ok(
            f"All {len(context.ecs_services)} services stable: "
            f"{total_running}/{total_desired} tasks running "
            f"({elapsed:.0f}s)"
        )


class FinalizeMultiServiceDeploymentStep(PipelineStep):
    """
    Finalize multi-service deployment and update records.
    """

    def __init__(self):
        super().__init__("FinalizeMultiServiceDeployment")

    def execute(self, context: PipelineContext) -> StepResult:
        if context.dry_run:
            return self._dry_run_summary(context)

        from django.utils import timezone

        # Update deployment record
        if context.deployment and context.deployment.id:
            try:
                context.deployment.status = 'completed'
                context.deployment.completed_at = timezone.now()
                context.deployment.metadata.update({
                    'services_deployed': list(context.ecs_services.keys()),
                    'service_count': len(context.ecs_services),
                    'deployment_order': context.service_order,
                })
                context.deployment.save()
            except Exception as e:
                context.add_warning(f"Failed to update deployment record: {e}")

        # Generate summary
        parts = [f"{len(context.ecs_services)} services deployed"]

        if context.load_balancer:
            parts.append(f"ALB: {context.load_balancer.alb_dns_name}")

        if context.built_images:
            parts.append(f"{len(context.built_images)} images built")

        if context.warnings:
            parts.append(f"{len(context.warnings)} warnings")

        return StepResult.ok("Deployment complete: " + ", ".join(parts))

    def _dry_run_summary(self, context: PipelineContext) -> StepResult:
        parts = ["[DRY RUN] Multi-service deployment simulation complete"]

        if context.preprocessed:
            svc_count = len(context.preprocessed.get_active_services())
            parts.append(f"{svc_count} services")

        if context.service_order:
            parts.append(f"order: {' -> '.join(context.service_order)}")

        if context.public_services:
            parts.append(
                f"public: {', '.join(context.public_services.keys())}"
            )

        if context.warnings:
            parts.append(f"{len(context.warnings)} warnings")

        return StepResult.ok(", ".join(parts))
