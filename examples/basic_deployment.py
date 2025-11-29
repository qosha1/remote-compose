"""
Basic deployment example for remote-compose.

This example demonstrates:
- Creating a deployment target
- Deploying a docker-compose application
- Checking deployment status
- Rolling back if needed
"""

from remote_compose.services import DeploymentService, TargetService
from remote_compose.models import DeploymentTarget, Deployment


def create_target():
    """Create a deployment target (remote server)."""
    target_service = TargetService()

    # Create a target with password authentication
    target = target_service.create(
        name='production-server',
        host='192.168.1.100',
        port=22,
        username='deploy',
        password='your-secure-password',  # Use SSH key in production!
    )

    # Test the connection
    success, message = target_service.test_connection(target)
    if success:
        print(f"Connected to {target.name}")
    else:
        print(f"Connection failed: {message}")

    return target


def create_target_with_ssh_key():
    """Create a target with SSH key authentication (recommended)."""
    target_service = TargetService()

    # Read SSH private key from file
    with open('/path/to/id_rsa', 'r') as f:
        private_key = f.read()

    target = target_service.create(
        name='production-server-ssh',
        host='192.168.1.100',
        port=22,
        username='deploy',
        ssh_private_key=private_key,
        # Optional: SSH key passphrase
        ssh_key_passphrase='key-passphrase',
    )

    return target


def deploy_application(target):
    """Deploy a docker-compose application to a target."""
    deployment_service = DeploymentService()

    # Basic deployment
    deployment = deployment_service.deploy(
        target=target,
        compose_file_path='/path/to/docker-compose.yml',
        project_name='myapp',
        version='v1.0.0',
        deployed_by='admin@example.com',
    )

    print(f"Deployment {deployment.id} status: {deployment.status}")

    if deployment.status == Deployment.Status.SUCCESS:
        print(f"Deployment successful!")
        print(f"Container IDs: {deployment.container_ids}")
    else:
        print(f"Deployment failed: {deployment.error_message}")

    return deployment


def deploy_with_environment(target):
    """Deploy with environment variables."""
    deployment_service = DeploymentService()

    deployment = deployment_service.deploy(
        target=target,
        compose_file_path='/path/to/docker-compose.yml',
        project_name='myapp',
        version='v1.0.0',
        deployed_by='admin@example.com',
        # Pass environment variables
        environment={
            'DATABASE_URL': 'postgres://user:pass@db:5432/myapp',
            'REDIS_URL': 'redis://redis:6379',
            'DEBUG': 'false',
        },
        # Or use an env file
        env_file_path='/path/to/.env.production',
    )

    return deployment


def deploy_with_options(target):
    """Deploy with additional options."""
    deployment_service = DeploymentService()

    deployment = deployment_service.deploy(
        target=target,
        compose_file_path='/path/to/docker-compose.yml',
        project_name='myapp',
        version='v2.0.0',
        deployed_by='admin@example.com',
        # Pull latest images before deploying
        pull_images=True,
        # Build images from Dockerfile
        build_images=False,
        # Custom timeout (default is 300 seconds)
        timeout=600,
        # Store metadata with the deployment
        metadata={
            'release_notes': 'Bug fixes and performance improvements',
            'ticket': 'JIRA-1234',
        },
    )

    return deployment


def check_deployment_status(deployment):
    """Check the live status of a deployment."""
    deployment_service = DeploymentService()

    status = deployment_service.get_status(deployment)

    print(f"Deployment ID: {status['deployment_id']}")
    print(f"Status: {status['status']}")
    print(f"Target: {status['target']}")

    # Live service status (if available)
    if 'live_service_status' in status:
        print("\nService Status:")
        for service_name, service_info in status['live_service_status'].items():
            print(f"  {service_name}: {service_info.get('state', 'unknown')}")


def rollback_deployment(deployment):
    """Rollback to a previous deployment."""
    deployment_service = DeploymentService()

    # Find the previous successful deployment
    previous = Deployment.objects.filter(
        target=deployment.target,
        project_name=deployment.project_name,
        status=Deployment.Status.SUCCESS,
    ).exclude(id=deployment.id).order_by('-completed_at').first()

    if previous:
        rollback = deployment_service.rollback(
            deployment=previous,
            deployed_by='admin@example.com',
        )
        print(f"Rolled back to deployment {previous.id}")
        return rollback
    else:
        print("No previous deployment to rollback to")
        return None


def stop_deployment(deployment):
    """Stop a running deployment."""
    deployment_service = DeploymentService()

    deployment_service.stop(
        deployment=deployment,
        deployed_by='admin@example.com',
    )
    print(f"Stopped deployment {deployment.id}")


def list_deployments():
    """List all deployments."""
    deployments = Deployment.objects.all().order_by('-created_at')[:10]

    for d in deployments:
        print(f"{d.id}: {d.project_name} @ {d.target.name} - {d.status} ({d.version})")


if __name__ == '__main__':
    # Example usage
    target = create_target()

    if target:
        deployment = deploy_application(target)
        check_deployment_status(deployment)
