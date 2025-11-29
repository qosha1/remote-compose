"""
Service for orchestrating multi-service deployments across multiple targets.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction
from django.utils import timezone

from ..models import Deployment, DeploymentTarget, DeploymentLog
from ..conf import get_setting
from ..exceptions import (
    DeploymentError,
    ValidationError,
    RollbackError,
)
from .base import BaseService
from .deployment_service import DeploymentService

logger = logging.getLogger(__name__)


class DeploymentStrategy(str, Enum):
    """Deployment strategy for multi-service deployments."""
    SEQUENTIAL = 'sequential'  # Deploy one at a time
    PARALLEL = 'parallel'  # Deploy all at once
    ROLLING = 'rolling'  # Deploy in batches
    CANARY = 'canary'  # Deploy to canary target first


@dataclass
class ServiceDeployment:
    """Configuration for a single service deployment."""
    target_id: int
    compose_file_path: str
    project_name: str
    environment: Dict[str, str] = field(default_factory=dict)
    version: str = ''
    priority: int = 0  # Lower = higher priority
    depends_on: List[str] = field(default_factory=list)  # Project names this depends on
    health_check_timeout: int = 60
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestrationResult:
    """Result of an orchestrated deployment."""
    success: bool
    total_services: int
    successful_count: int
    failed_count: int
    deployments: List[Deployment]
    errors: List[Dict[str, Any]]
    duration_seconds: float
    strategy: str

    def to_dict(self) -> dict:
        return {
            'success': self.success,
            'total_services': self.total_services,
            'successful_count': self.successful_count,
            'failed_count': self.failed_count,
            'deployment_ids': [d.id for d in self.deployments],
            'errors': self.errors,
            'duration_seconds': self.duration_seconds,
            'strategy': self.strategy,
        }


class OrchestrationService(BaseService):
    """
    Service for orchestrating complex multi-service deployments.
    """

    def __init__(
        self,
        deployment_service: Optional[DeploymentService] = None,
        max_parallel: int = 5,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.deployment_service = deployment_service or DeploymentService()
        self.max_parallel = max_parallel

    def deploy_multiple(
        self,
        services: List[ServiceDeployment],
        strategy: DeploymentStrategy = DeploymentStrategy.SEQUENTIAL,
        deployed_by: str = '',
        rollback_on_failure: bool = True,
        batch_size: int = 2,
        canary_target_id: Optional[int] = None,
    ) -> OrchestrationResult:
        """
        Deploy multiple services with the specified strategy.

        Args:
            services: List of ServiceDeployment configurations
            strategy: Deployment strategy to use
            deployed_by: User performing the deployment
            rollback_on_failure: Rollback successful deployments if any fail
            batch_size: Batch size for rolling deployments
            canary_target_id: Target ID for canary deployments

        Returns:
            OrchestrationResult
        """
        start_time = timezone.now()
        self.log_info(f"Starting orchestrated deployment of {len(services)} services with strategy: {strategy}")

        if not services:
            raise ValidationError("No services to deploy")

        # Sort by priority and resolve dependencies
        sorted_services = self._sort_by_dependencies(services)

        deployments = []
        errors = []

        try:
            if strategy == DeploymentStrategy.SEQUENTIAL:
                deployments, errors = self._deploy_sequential(sorted_services, deployed_by)

            elif strategy == DeploymentStrategy.PARALLEL:
                deployments, errors = self._deploy_parallel(sorted_services, deployed_by)

            elif strategy == DeploymentStrategy.ROLLING:
                deployments, errors = self._deploy_rolling(sorted_services, deployed_by, batch_size)

            elif strategy == DeploymentStrategy.CANARY:
                if not canary_target_id:
                    raise ValidationError("Canary deployment requires canary_target_id")
                deployments, errors = self._deploy_canary(
                    sorted_services, deployed_by, canary_target_id
                )

            # Handle rollback if needed
            if errors and rollback_on_failure:
                self.log_warning(f"Deployment errors occurred, rolling back {len(deployments)} successful deployments")
                self._rollback_deployments(deployments, deployed_by)
                errors.append({
                    'action': 'rollback',
                    'message': f"Rolled back {len(deployments)} deployments due to failures",
                })

        except Exception as e:
            self.log_error(f"Orchestration failed: {e}")
            errors.append({
                'action': 'orchestration',
                'error': str(e),
            })

        end_time = timezone.now()
        duration = (end_time - start_time).total_seconds()

        success = len(errors) == 0
        successful_count = len([d for d in deployments if d.status == Deployment.Status.SUCCESS])

        result = OrchestrationResult(
            success=success,
            total_services=len(services),
            successful_count=successful_count,
            failed_count=len(services) - successful_count,
            deployments=deployments,
            errors=errors,
            duration_seconds=duration,
            strategy=strategy.value,
        )

        self.log_info(f"Orchestration completed: {successful_count}/{len(services)} successful in {duration:.1f}s")
        self.notify_observers('orchestration_completed', result=result)

        return result

    def _sort_by_dependencies(self, services: List[ServiceDeployment]) -> List[ServiceDeployment]:
        """
        Sort services by dependencies using Kahn's topological sort algorithm.

        This ensures services are deployed in the correct order based on their
        dependencies, while also respecting priority values for services at
        the same dependency level.

        Algorithm:
        1. Build adjacency list (graph) and count incoming edges (in_degree)
        2. Start with nodes that have no dependencies (in_degree = 0)
        3. Process nodes in priority order, adding dependents when ready
        4. If we can't process all nodes, there's a circular dependency

        Time complexity: O(V + E) where V = services, E = dependencies
        """
        # Map service names to ServiceDeployment objects for quick lookup
        service_map = {s.project_name: s for s in services}

        # Track number of unmet dependencies for each service
        # in_degree[X] = count of services that X depends on (that haven't deployed yet)
        in_degree = {s.project_name: 0 for s in services}

        # Adjacency list: graph[A] = [B, C] means B and C depend on A
        # When A completes, we can decrement in_degree for B and C
        graph = {s.project_name: [] for s in services}

        # Build the dependency graph
        for service in services:
            for dep in service.depends_on:
                if dep in service_map:
                    # dep -> service (service depends on dep)
                    graph[dep].append(service.project_name)
                    in_degree[service.project_name] += 1

        # Initialize queue with services that have no dependencies
        # Use (priority, name) tuples so lower priority numbers deploy first
        result = []
        queue = [(s.priority, s.project_name) for s in services if in_degree[s.project_name] == 0]
        queue.sort()  # Sort by priority

        while queue:
            # Pop the highest priority (lowest number) service with no unmet dependencies
            _, name = queue.pop(0)
            result.append(service_map[name])

            # This service is now "deployed", so update dependents
            for dependent in graph[name]:
                in_degree[dependent] -= 1
                # If all dependencies are now met, add to queue
                if in_degree[dependent] == 0:
                    queue.append((service_map[dependent].priority, dependent))
                    queue.sort()  # Re-sort to maintain priority order

        # If we couldn't process all services, there must be a cycle
        if len(result) != len(services):
            raise ValidationError("Circular dependency detected in service configuration")

        return result

    def _deploy_sequential(
        self,
        services: List[ServiceDeployment],
        deployed_by: str,
    ) -> tuple:
        """Deploy services one at a time."""
        deployments = []
        errors = []

        for service in services:
            try:
                deployment = self._deploy_single_service(service, deployed_by)
                deployments.append(deployment)

                if deployment.status != Deployment.Status.SUCCESS:
                    errors.append({
                        'project_name': service.project_name,
                        'target_id': service.target_id,
                        'error': deployment.error_message,
                    })
                    break  # Stop on first failure

            except Exception as e:
                errors.append({
                    'project_name': service.project_name,
                    'target_id': service.target_id,
                    'error': str(e),
                })
                break

        return deployments, errors

    def _deploy_parallel(
        self,
        services: List[ServiceDeployment],
        deployed_by: str,
    ) -> tuple:
        """Deploy all services in parallel."""
        deployments = []
        errors = []

        with ThreadPoolExecutor(max_workers=self.max_parallel) as executor:
            futures = {
                executor.submit(self._deploy_single_service, service, deployed_by): service
                for service in services
            }

            for future in as_completed(futures):
                service = futures[future]
                try:
                    deployment = future.result()
                    deployments.append(deployment)

                    if deployment.status != Deployment.Status.SUCCESS:
                        errors.append({
                            'project_name': service.project_name,
                            'target_id': service.target_id,
                            'error': deployment.error_message,
                        })

                except Exception as e:
                    errors.append({
                        'project_name': service.project_name,
                        'target_id': service.target_id,
                        'error': str(e),
                    })

        return deployments, errors

    def _deploy_rolling(
        self,
        services: List[ServiceDeployment],
        deployed_by: str,
        batch_size: int,
    ) -> tuple:
        """Deploy services in batches."""
        deployments = []
        errors = []

        # Split into batches
        batches = [services[i:i + batch_size] for i in range(0, len(services), batch_size)]

        for batch_num, batch in enumerate(batches):
            self.log_info(f"Deploying batch {batch_num + 1}/{len(batches)}")

            # Deploy batch in parallel
            batch_deployments, batch_errors = self._deploy_parallel(batch, deployed_by)
            deployments.extend(batch_deployments)
            errors.extend(batch_errors)

            # Stop if batch had errors
            if batch_errors:
                self.log_warning(f"Batch {batch_num + 1} had errors, stopping rolling deployment")
                break

        return deployments, errors

    def _deploy_canary(
        self,
        services: List[ServiceDeployment],
        deployed_by: str,
        canary_target_id: int,
    ) -> tuple:
        """Deploy to canary target first, then to all others."""
        deployments = []
        errors = []

        # Separate canary and non-canary services
        canary_services = [s for s in services if s.target_id == canary_target_id]
        other_services = [s for s in services if s.target_id != canary_target_id]

        if not canary_services:
            raise ValidationError(f"No services configured for canary target {canary_target_id}")

        # Deploy to canary first
        self.log_info(f"Deploying {len(canary_services)} services to canary target")
        canary_deployments, canary_errors = self._deploy_sequential(canary_services, deployed_by)
        deployments.extend(canary_deployments)
        errors.extend(canary_errors)

        if canary_errors:
            self.log_error("Canary deployment failed, not proceeding to other targets")
            return deployments, errors

        self.log_info("Canary deployment successful, proceeding to other targets")

        # Deploy to remaining targets
        other_deployments, other_errors = self._deploy_parallel(other_services, deployed_by)
        deployments.extend(other_deployments)
        errors.extend(other_errors)

        return deployments, errors

    def _deploy_single_service(
        self,
        service: ServiceDeployment,
        deployed_by: str,
    ) -> Deployment:
        """Deploy a single service."""
        target = DeploymentTarget.objects.get(id=service.target_id)

        return self.deployment_service.deploy(
            target=target,
            compose_file_path=service.compose_file_path,
            project_name=service.project_name,
            environment=service.environment,
            version=service.version,
            deployed_by=deployed_by,
            metadata=service.metadata,
        )

    def _rollback_deployments(
        self,
        deployments: List[Deployment],
        deployed_by: str,
    ) -> None:
        """Rollback a list of deployments."""
        for deployment in reversed(deployments):
            if deployment.status == Deployment.Status.SUCCESS:
                try:
                    self.deployment_service.rollback(
                        deployment=deployment,
                        deployed_by=deployed_by,
                    )
                    self.log_info(f"Rolled back deployment {deployment.id}")
                except Exception as e:
                    self.log_error(f"Failed to rollback deployment {deployment.id}: {e}")

    def create_deployment_plan(
        self,
        services: List[ServiceDeployment],
        strategy: DeploymentStrategy = DeploymentStrategy.SEQUENTIAL,
    ) -> Dict[str, Any]:
        """
        Create a deployment plan without executing it.

        Args:
            services: List of ServiceDeployment configurations
            strategy: Deployment strategy to use

        Returns:
            Dict describing the deployment plan
        """
        sorted_services = self._sort_by_dependencies(services)

        plan = {
            'strategy': strategy.value,
            'total_services': len(services),
            'deployment_order': [],
        }

        for i, service in enumerate(sorted_services):
            try:
                target = DeploymentTarget.objects.get(id=service.target_id)
                target_name = target.name
            except DeploymentTarget.DoesNotExist:
                target_name = f"unknown-{service.target_id}"

            plan['deployment_order'].append({
                'order': i + 1,
                'project_name': service.project_name,
                'target': target_name,
                'version': service.version,
                'depends_on': service.depends_on,
                'priority': service.priority,
            })

        return plan

    def deploy_to_target_group(
        self,
        target_ids: List[int],
        compose_file_path: str,
        project_name: str,
        environment: Optional[Dict[str, str]] = None,
        version: str = '',
        deployed_by: str = '',
        strategy: DeploymentStrategy = DeploymentStrategy.ROLLING,
        batch_size: int = 2,
    ) -> OrchestrationResult:
        """
        Deploy the same service to multiple targets.

        Args:
            target_ids: List of target IDs to deploy to
            compose_file_path: Path to docker-compose.yml
            project_name: Project name
            environment: Environment variables
            version: Version string
            deployed_by: User performing deployment
            strategy: Deployment strategy
            batch_size: Batch size for rolling deployment

        Returns:
            OrchestrationResult
        """
        services = [
            ServiceDeployment(
                target_id=target_id,
                compose_file_path=compose_file_path,
                project_name=project_name,
                environment=environment or {},
                version=version,
            )
            for target_id in target_ids
        ]

        return self.deploy_multiple(
            services=services,
            strategy=strategy,
            deployed_by=deployed_by,
            batch_size=batch_size,
        )
