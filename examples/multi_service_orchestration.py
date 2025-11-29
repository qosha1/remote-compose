"""
Multi-service orchestration example.

This example demonstrates:
- Deploying multiple services across multiple targets
- Using different deployment strategies
- Handling dependencies between services
- Creating deployment plans
"""

from remote_compose.services import (
    OrchestrationService,
    ServiceDeployment,
    DeploymentStrategy,
)
from remote_compose.models import DeploymentTarget


def deploy_with_dependencies():
    """Deploy multiple services respecting their dependencies."""
    orchestration_service = OrchestrationService(max_parallel=3)

    # Define services with dependencies
    # The database must deploy before the backend and API
    # The backend and API must deploy before the frontend
    services = [
        ServiceDeployment(
            target_id=1,
            compose_file_path='/app/services/database/docker-compose.yml',
            project_name='database',
            priority=0,  # Lower number = higher priority
            depends_on=[],  # No dependencies
        ),
        ServiceDeployment(
            target_id=2,
            compose_file_path='/app/services/backend/docker-compose.yml',
            project_name='backend',
            priority=1,
            depends_on=['database'],  # Depends on database
            environment={
                'DATABASE_URL': 'postgres://user:pass@db-server:5432/app',
            },
        ),
        ServiceDeployment(
            target_id=3,
            compose_file_path='/app/services/api/docker-compose.yml',
            project_name='api',
            priority=1,
            depends_on=['database'],  # Also depends on database
            environment={
                'DATABASE_URL': 'postgres://user:pass@db-server:5432/app',
            },
        ),
        ServiceDeployment(
            target_id=4,
            compose_file_path='/app/services/frontend/docker-compose.yml',
            project_name='frontend',
            priority=2,
            depends_on=['backend', 'api'],  # Depends on both backend and API
            environment={
                'API_URL': 'https://api.example.com',
                'BACKEND_URL': 'https://backend.example.com',
            },
        ),
    ]

    # Deploy sequentially (respects dependencies)
    result = orchestration_service.deploy_multiple(
        services=services,
        strategy=DeploymentStrategy.SEQUENTIAL,
        deployed_by='admin@example.com',
        rollback_on_failure=True,  # Rollback all if any fail
    )

    print(f"Deployment result: {result.success}")
    print(f"Successful: {result.successful_count}/{result.total_services}")

    if result.errors:
        print("Errors:")
        for error in result.errors:
            print(f"  - {error}")

    return result


def deploy_parallel():
    """Deploy multiple independent services in parallel."""
    orchestration_service = OrchestrationService(max_parallel=5)

    # Services without dependencies can be deployed in parallel
    services = [
        ServiceDeployment(
            target_id=i,
            compose_file_path=f'/app/services/worker-{i}/docker-compose.yml',
            project_name=f'worker-{i}',
            environment={'WORKER_ID': str(i)},
        )
        for i in range(1, 6)
    ]

    result = orchestration_service.deploy_multiple(
        services=services,
        strategy=DeploymentStrategy.PARALLEL,
        deployed_by='admin@example.com',
    )

    print(f"Deployed {result.successful_count} workers in {result.duration_seconds:.1f}s")
    return result


def deploy_rolling():
    """Deploy using rolling strategy (in batches)."""
    orchestration_service = OrchestrationService(max_parallel=2)

    # Deploy to multiple servers in rolling fashion
    services = [
        ServiceDeployment(
            target_id=target_id,
            compose_file_path='/app/docker-compose.yml',
            project_name='webapp',
            version='v2.0.0',
        )
        for target_id in range(1, 11)  # 10 servers
    ]

    result = orchestration_service.deploy_multiple(
        services=services,
        strategy=DeploymentStrategy.ROLLING,
        deployed_by='admin@example.com',
        batch_size=2,  # Deploy 2 at a time
    )

    print(f"Rolling deployment complete: {result.successful_count}/{result.total_services}")
    return result


def deploy_canary():
    """Deploy using canary strategy (test on one server first)."""
    orchestration_service = OrchestrationService()

    canary_target_id = 1  # The canary server

    services = [
        ServiceDeployment(
            target_id=target_id,
            compose_file_path='/app/docker-compose.yml',
            project_name='webapp',
            version='v2.0.0',
        )
        for target_id in range(1, 6)  # 5 servers, #1 is canary
    ]

    result = orchestration_service.deploy_multiple(
        services=services,
        strategy=DeploymentStrategy.CANARY,
        deployed_by='admin@example.com',
        canary_target_id=canary_target_id,
    )

    if result.success:
        print("Canary deployment successful! All servers updated.")
    else:
        print("Canary deployment failed - other servers not updated.")

    return result


def create_deployment_plan():
    """Preview deployment plan without executing."""
    orchestration_service = OrchestrationService()

    services = [
        ServiceDeployment(
            target_id=1,
            compose_file_path='/app/db/docker-compose.yml',
            project_name='database',
            priority=0,
        ),
        ServiceDeployment(
            target_id=2,
            compose_file_path='/app/api/docker-compose.yml',
            project_name='api',
            priority=1,
            depends_on=['database'],
        ),
        ServiceDeployment(
            target_id=3,
            compose_file_path='/app/web/docker-compose.yml',
            project_name='frontend',
            priority=2,
            depends_on=['api'],
        ),
    ]

    # Create plan without executing
    plan = orchestration_service.create_deployment_plan(
        services=services,
        strategy=DeploymentStrategy.SEQUENTIAL,
    )

    print("Deployment Plan:")
    print(f"Strategy: {plan['strategy']}")
    print(f"Total Services: {plan['total_services']}")
    print("\nDeployment Order:")
    for item in plan['deployment_order']:
        print(f"  {item['order']}. {item['project_name']} -> {item['target']}")
        if item['depends_on']:
            print(f"     (depends on: {', '.join(item['depends_on'])})")

    return plan


def deploy_to_server_group():
    """Deploy same service to multiple servers."""
    orchestration_service = OrchestrationService()

    # Get all production server IDs
    target_ids = list(
        DeploymentTarget.objects.filter(
            name__startswith='prod-',
            is_active=True,
        ).values_list('id', flat=True)
    )

    result = orchestration_service.deploy_to_target_group(
        target_ids=target_ids,
        compose_file_path='/app/docker-compose.yml',
        project_name='webapp',
        version='v2.0.0',
        environment={'ENV': 'production'},
        deployed_by='admin@example.com',
        strategy=DeploymentStrategy.ROLLING,
        batch_size=2,
    )

    print(f"Deployed to {result.successful_count}/{len(target_ids)} servers")
    return result


if __name__ == '__main__':
    # Preview the plan first
    plan = create_deployment_plan()

    print("\n" + "=" * 50 + "\n")

    # Execute deployment
    result = deploy_with_dependencies()
