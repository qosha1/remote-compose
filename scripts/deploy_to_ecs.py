#!/usr/bin/env python3
"""
Deploy to AWS ECS using remote-compose.

This script uses deploy_full() to handle complex docker-compose files with:
- Build contexts (builds and pushes images to ECR)
- YAML anchors and aliases
- EFS volumes for persistent storage

Pipeline Steps:
    [1/6] Preprocessing compose file - Resolves YAML anchors and validates
    [2/6] Creating ECR repositories - Creates ECR repos for services with build contexts
    [3/6] Building and pushing images - Builds Docker images and pushes to ECR
    [4/6] Creating EFS for persistent volumes - Creates EFS filesystem for named volumes
    [5/6] Creating ECS task definition - Generates ECS-compatible task definition
    [6/6] Deploying ECS service - Deploys the service to the cluster

Usage:
    # List available ECS clusters
    python scripts/deploy_to_ecs.py --list-clusters --env-file .django

    # Create a new ECS cluster
    python scripts/deploy_to_ecs.py --create-cluster my-cluster --env-file .django

    # Deploy to an existing cluster
    python scripts/deploy_to_ecs.py /path/to/repo --cluster my-cluster --env-file .django

    # Deploy with custom resource allocation
    python scripts/deploy_to_ecs.py /path/to/repo --cluster my-cluster --cpu 512 --memory 1024

    # Preview deployment without executing
    python scripts/deploy_to_ecs.py /path/to/repo --cluster my-cluster --dry-run

    # Deploy with custom image tag
    python scripts/deploy_to_ecs.py /path/to/repo --cluster my-cluster --image-tag v1.2.3

    # Skip image building (use existing images)
    python scripts/deploy_to_ecs.py /path/to/repo --cluster my-cluster --no-build

    # Skip EFS creation for volumes
    python scripts/deploy_to_ecs.py /path/to/repo --cluster my-cluster --no-efs

Examples:
    # Deploy sample app to a new cluster
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --create-cluster sample-cluster \\
        --env-file .django

    # Deploy with multiple tasks
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster sample-cluster \\
        --desired-count 2 \\
        --env-file .django
"""

import argparse
import os
import sys
from pathlib import Path

# Add the project root to the path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_env_file(env_file_path: str) -> dict:
    """Load environment variables from a file."""
    env_vars = {}

    if not os.path.exists(env_file_path):
        print(f"Error: Env file not found: {env_file_path}")
        sys.exit(1)

    with open(env_file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if value and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                env_vars[key] = value
                os.environ[key] = value

    return env_vars


def setup_django(env_vars: dict):
    """Configure Django settings for standalone use."""
    import django
    from django.conf import settings
    import tempfile

    if settings.configured:
        return

    temp_dir = tempfile.mkdtemp(prefix='remote_compose_ecs_')

    encryption_key = env_vars.get('REMOTE_COMPOSE_ENCRYPTION_KEY') or env_vars.get('ENCRYPTION_KEY')
    if not encryption_key:
        from cryptography.fernet import Fernet
        encryption_key = Fernet.generate_key().decode()
        print("Warning: No encryption key found, generated temporary key")

    settings.configure(
        DEBUG=True,
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': os.path.join(temp_dir, 'ecs_deploy.sqlite3'),
            }
        },
        INSTALLED_APPS=[
            'django.contrib.contenttypes',
            'django.contrib.auth',
            'remote_compose',
        ],
        DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
        USE_TZ=True,
        REMOTE_COMPOSE={
            'ENCRYPTION_KEY': encryption_key,
            'DEPLOYMENT_TIMEOUT': 300,
            'AWS_DEFAULT_REGION': env_vars.get('AWS_DEFAULT_REGION', env_vars.get('AWS_REGION', 'us-east-1')),
        },
        CACHES={
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            }
        },
        SECRET_KEY='ecs-deploy-script-key',
    )

    django.setup()

    from django.core.management import call_command
    call_command('migrate', '--run-syncdb', verbosity=0)

    return temp_dir


def check_aws_credentials():
    """Check if AWS credentials are configured."""
    access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    if not access_key or not secret_key:
        print("Error: AWS credentials not found in environment")
        print("Expected variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
        return False

    print(f"AWS credentials found:")
    print(f"  Access Key: {access_key[:4]}...{access_key[-4:]}")
    print(f"  Region: {os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-east-1'))}")
    return True


def list_clusters(region: str = None):
    """List ECS clusters."""
    from remote_compose.services import ECSService
    from remote_compose.models import ECSCluster

    ecs_service = ECSService()
    region = region or os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-east-1'))

    print(f"\n=== ECS Clusters in {region} ===\n")

    # List from AWS
    print("AWS Clusters:")
    try:
        aws_clusters = ecs_service.list_clusters(region=region)
        if aws_clusters:
            print(f"{'Name':<30} {'Status':<15} {'Services':<10} {'Tasks':<10}")
            print("-" * 70)
            for cluster in aws_clusters:
                print(f"{cluster['name']:<30} {cluster['status']:<15} "
                      f"{cluster['active_services']:<10} {cluster['running_tasks']:<10}")
        else:
            print("  No clusters found in AWS")
    except Exception as e:
        print(f"  Error listing AWS clusters: {e}")

    # List locally tracked
    print("\nLocally Tracked Clusters:")
    local_clusters = ECSCluster.objects.all()
    if local_clusters:
        for cluster in local_clusters:
            print(f"  - {cluster.name} ({cluster.aws_region}) - {cluster.status}")
    else:
        print("  No clusters tracked locally")


def create_cluster(name: str, region: str = None, launch_type: str = 'fargate'):
    """Create a new ECS cluster."""
    from remote_compose.services import ECSService
    from remote_compose.models import ECSCluster

    ecs_service = ECSService()
    region = region or os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-east-1'))

    print(f"\nCreating ECS cluster: {name} in {region}")
    print(f"Launch type: {launch_type}")

    try:
        cluster = ecs_service.create_cluster(
            name=name,
            region=region,
            capacity_providers=['FARGATE', 'FARGATE_SPOT'] if launch_type == 'fargate' else None,
        )

        cluster.launch_type = ECSCluster.LaunchType.FARGATE if launch_type == 'fargate' else ECSCluster.LaunchType.EC2
        cluster.save()

        print(f"\nCluster created successfully!")
        print(f"  Name: {cluster.name}")
        print(f"  ARN: {cluster.aws_cluster_arn}")
        print(f"  Region: {cluster.aws_region}")

        # Setup networking
        print("\nConfiguring networking...")
        ecs_service.sync_cluster_networking(cluster)
        print(f"  VPC: {cluster.vpc_id}")
        print(f"  Subnets: {cluster.subnet_ids}")
        print(f"  Security Groups: {cluster.security_group_ids}")

        return cluster

    except Exception as e:
        print(f"\nError creating cluster: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_or_import_cluster(name: str, region: str = None):
    """Get existing cluster or import from AWS."""
    from remote_compose.models import ECSCluster
    from remote_compose.services import ECSService

    # Check local first
    try:
        return ECSCluster.objects.get(name=name)
    except ECSCluster.DoesNotExist:
        pass

    # Try to import from AWS
    ecs_service = ECSService()
    region = region or os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-east-1'))

    print(f"Cluster '{name}' not found locally, checking AWS...")

    try:
        cluster = ecs_service.import_cluster(
            cluster_name_or_arn=name,
            region=region,
        )
        print(f"Imported cluster from AWS: {cluster.name}")

        # Setup networking
        ecs_service.sync_cluster_networking(cluster)
        return cluster

    except Exception as e:
        print(f"Error: Cluster not found: {e}")
        return None


def find_compose_file(repo_path: str, compose_file: str = None) -> str:
    """Find the docker-compose file in the repository."""
    repo = Path(repo_path)

    if compose_file:
        path = repo / compose_file
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"Compose file not found: {path}")

    common_names = [
        'docker-compose.yml',
        'docker-compose.yaml',
        'compose.yml',
        'compose.yaml',
    ]

    for name in common_names:
        path = repo / name
        if path.exists():
            return str(path)

    raise FileNotFoundError(f"No docker-compose file found in {repo_path}")


def deploy(
    repo_path: str,
    cluster,
    compose_file: str = None,
    project_name: str = None,
    version: str = '',
    env_vars: dict = None,
    desired_count: int = 1,
    cpu: str = None,
    memory: str = None,
    no_wait: bool = False,
    timeout: int = 300,
    build: bool = True,
    force_rebuild: bool = False,
    image_tag: str = 'latest',
    create_efs: bool = True,
    strict: bool = False,
    dry_run: bool = False,
):
    """Deploy docker-compose application to ECS using the pipeline architecture.

    This method handles complex docker-compose files with:
    - Build contexts (builds and pushes images to ECR)
    - YAML anchors and aliases
    - EFS volumes for persistent storage
    - Automatic rollback on failure
    """
    from remote_compose.services import ECSDeploymentService

    compose_path = find_compose_file(repo_path, compose_file)

    if not project_name:
        project_name = Path(repo_path).name

    print(f"\n{'=' * 60}")
    if dry_run:
        print("DRY RUN - PREVIEWING DEPLOYMENT TO AWS ECS")
    else:
        print("DEPLOYING TO AWS ECS (Pipeline Architecture)")
    print(f"{'=' * 60}")
    print(f"Cluster: {cluster.name} ({cluster.aws_region})")
    print(f"Launch Type: {cluster.launch_type}")
    print(f"Compose: {compose_path}")
    print(f"Project: {project_name}")
    print(f"Desired Count: {desired_count}")
    if version:
        print(f"Version: {version}")
    if cpu:
        print(f"CPU: {cpu}")
    if memory:
        print(f"Memory: {memory}MB")
    print(f"\nBuild Options:")
    print(f"  Build Images: {build}")
    print(f"  Force Rebuild: {force_rebuild}")
    print(f"  Image Tag: {image_tag}")
    print(f"\nVolume Options:")
    print(f"  Create EFS: {create_efs}")
    print(f"\nBehavior:")
    print(f"  Strict Mode: {strict}")
    print(f"  Dry Run: {dry_run}")
    print(f"{'=' * 60}\n")

    deployment_service = ECSDeploymentService()

    # Event handler to print pipeline progress
    def pipeline_event_handler(event_type, **kwargs):
        step = kwargs.get('step', '')
        message = kwargs.get('message', '')
        if event_type == 'step_started':
            print(f"[STEP] Starting: {step}")
        elif event_type == 'step_completed':
            print(f"[DONE] {step}: {message}")
        elif event_type == 'step_skipped':
            print(f"[SKIP] {step}: {message}")
        elif event_type == 'step_failed':
            print(f"[FAIL] {step}: {message}")
        elif event_type == 'rollback_started':
            print(f"\n[ROLLBACK] Cleaning up resources...")
        elif event_type == 'step_cleanup_completed':
            print(f"[CLEANUP] {step}")
        elif event_type == 'pipeline_completed':
            duration = kwargs.get('duration', 0)
            print(f"\n[COMPLETE] Pipeline finished in {duration:.1f}s")
        elif event_type == 'pipeline_failed':
            failed_step = kwargs.get('failed_step', 'unknown')
            print(f"\n[FAILED] Pipeline failed at step: {failed_step}")

    try:
        deployment = deployment_service.deploy_with_pipeline(
            cluster=cluster,
            compose_file_path=compose_path,
            project_name=project_name,
            environment=env_vars or {},
            version=version,
            deployed_by='ecs-deploy-script',
            desired_count=desired_count,
            cpu=cpu,
            memory=memory,
            wait_for_stable=not no_wait,
            timeout=timeout,
            build_images=build,
            force_rebuild=force_rebuild,
            image_tag=image_tag,
            create_efs_for_volumes=create_efs,
            strict_mode=strict,
            dry_run=dry_run,
            event_handler=pipeline_event_handler,
        )

        print(f"\n{'=' * 60}")
        if deployment and deployment.status == 'success':
            print("DEPLOYMENT SUCCESSFUL!")
            print(f"{'=' * 60}")
            if deployment.id:
                print(f"  Deployment ID: {deployment.id}")
            duration = deployment.duration if deployment.duration else getattr(deployment, '_duration', None)
            if duration:
                print(f"  Duration: {duration:.1f}s")
            if deployment.metadata.get('service_arn'):
                print(f"  Service ARN: {deployment.metadata['service_arn']}")
            if deployment.metadata.get('task_definition_arn'):
                print(f"  Task Definition: {deployment.metadata['task_definition_arn']}")
            if deployment.metadata.get('running_count'):
                print(f"  Running Tasks: {deployment.metadata['running_count']}")
        elif deployment:
            print("DEPLOYMENT COMPLETED!")
            print(f"{'=' * 60}")
            if dry_run:
                print("  (Dry run - no actual changes made)")
        else:
            print("DEPLOYMENT FAILED!")
            print(f"{'=' * 60}")

        return deployment

    except Exception as e:
        print(f"\n{'=' * 60}")
        print("DEPLOYMENT ERROR!")
        print(f"{'=' * 60}")
        print(f"  {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    parser = argparse.ArgumentParser(
        description='Deploy docker-compose applications to AWS ECS',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # List ECS clusters
    python scripts/deploy_to_ecs.py --list-clusters --env-file .django

    # Create a new Fargate cluster
    python scripts/deploy_to_ecs.py --create-cluster my-app-cluster --env-file .django

    # Deploy to existing cluster
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --env-file .django

    # Deploy with custom resources
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --cpu 512 \\
        --memory 1024 \\
        --desired-count 2 \\
        --env-file .django

    # Preview deployment without executing (dry run)
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --dry-run \\
        --env-file .django

    # Deploy with custom image tag and force rebuild
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --image-tag v1.2.3 \\
        --force-rebuild \\
        --env-file .django

    # Deploy without building images (use existing)
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --no-build \\
        --env-file .django

    # Deploy without EFS volumes
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --no-efs \\
        --env-file .django

    # Deploy in strict mode (fail on warnings)
    python scripts/deploy_to_ecs.py examples/sample-app \\
        --cluster my-app-cluster \\
        --strict \\
        --env-file .django
        """
    )

    parser.add_argument(
        'repo_path',
        nargs='?',
        help='Path to the repository containing docker-compose.yml'
    )
    parser.add_argument(
        '--env-file',
        default='.django',
        help='Path to .env file with AWS credentials (default: .django)'
    )
    parser.add_argument(
        '--list-clusters',
        action='store_true',
        help='List available ECS clusters and exit'
    )
    parser.add_argument(
        '--create-cluster',
        metavar='NAME',
        help='Create a new ECS cluster with this name'
    )
    parser.add_argument(
        '--cluster',
        help='Existing ECS cluster name to deploy to'
    )
    parser.add_argument(
        '--region',
        help='AWS region (default: from env or us-east-1)'
    )
    parser.add_argument(
        '--launch-type',
        choices=['fargate', 'ec2'],
        default='fargate',
        help='ECS launch type (default: fargate)'
    )
    parser.add_argument(
        '-f', '--compose-file',
        help='Docker compose file name (default: auto-detect)'
    )
    parser.add_argument(
        '-p', '--project-name',
        help='Docker Compose project name (default: directory name)'
    )
    parser.add_argument(
        '--version',
        default='',
        help='Version tag for this deployment'
    )
    parser.add_argument(
        '-e', '--env',
        action='append',
        dest='deploy_env_vars',
        help='Environment variables for deployment (KEY=VALUE)'
    )
    parser.add_argument(
        '--desired-count',
        type=int,
        default=1,
        help='Number of tasks to run (default: 1)'
    )
    parser.add_argument(
        '--cpu',
        help='CPU units (256, 512, 1024, 2048, 4096)'
    )
    parser.add_argument(
        '--memory',
        help='Memory in MB (512, 1024, 2048, etc.)'
    )
    parser.add_argument(
        '--no-wait',
        action='store_true',
        help='Do not wait for deployment to stabilize'
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=300,
        help='Timeout waiting for stability in seconds (default: 300)'
    )

    # Build options
    parser.add_argument(
        '--build',
        action='store_true',
        default=True,
        help='Build images for services with build contexts (default: True)'
    )
    parser.add_argument(
        '--no-build',
        action='store_false',
        dest='build',
        help='Skip image building, use existing images'
    )
    parser.add_argument(
        '--force-rebuild',
        action='store_true',
        help='Force rebuild images even if they exist in ECR'
    )
    parser.add_argument(
        '--image-tag',
        default='latest',
        help='Tag for built images (default: latest)'
    )

    # EFS options
    parser.add_argument(
        '--create-efs',
        action='store_true',
        default=True,
        help='Create EFS for named volumes (default: True)'
    )
    parser.add_argument(
        '--no-efs',
        action='store_false',
        dest='create_efs',
        help='Skip EFS creation, named volumes will be empty'
    )

    # Behavior
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Fail on any compatibility warnings'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview deployment without executing'
    )

    args = parser.parse_args()

    # Load environment file
    print(f"Loading environment from: {args.env_file}")
    env_vars = load_env_file(args.env_file)

    # Check AWS credentials
    if not check_aws_credentials():
        sys.exit(1)

    # Setup Django
    print("\nInitializing...")
    setup_django(env_vars)

    # List clusters mode
    if args.list_clusters:
        list_clusters(args.region)
        return

    # Create cluster mode
    if args.create_cluster:
        cluster = create_cluster(
            args.create_cluster,
            args.region,
            args.launch_type,
        )
        if not cluster:
            sys.exit(1)

        # If no repo path, just create and exit
        if not args.repo_path:
            print("\nCluster created. Use --cluster to deploy to it.")
            return

        # Continue to deploy
        args.cluster = args.create_cluster

    # Need cluster for deployment
    if not args.cluster and not args.create_cluster:
        if args.repo_path:
            print("\nError: Must specify --cluster or --create-cluster")
            print("Use --list-clusters to see available clusters")
            sys.exit(1)
        else:
            parser.print_help()
            sys.exit(1)

    # Get cluster
    cluster = get_or_import_cluster(args.cluster, args.region)
    if not cluster:
        print(f"\nCluster not found: {args.cluster}")
        print("Use --create-cluster to create a new cluster")
        sys.exit(1)

    # Need repo path for deployment
    if not args.repo_path:
        parser.print_help()
        print("\nError: repo_path is required for deployment")
        sys.exit(1)

    repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo_path):
        print(f"Error: Not a directory: {repo_path}")
        sys.exit(1)

    # Parse deployment environment variables
    deploy_env = {}
    if args.deploy_env_vars:
        for env_var in args.deploy_env_vars:
            if '=' not in env_var:
                print(f"Error: Invalid environment variable: {env_var}")
                sys.exit(1)
            key, value = env_var.split('=', 1)
            deploy_env[key] = value

    # Deploy
    deploy(
        repo_path=repo_path,
        cluster=cluster,
        compose_file=args.compose_file,
        project_name=args.project_name,
        version=args.version,
        env_vars=deploy_env,
        desired_count=args.desired_count,
        cpu=args.cpu,
        memory=args.memory,
        no_wait=args.no_wait,
        timeout=args.timeout,
        build=args.build,
        force_rebuild=args.force_rebuild,
        image_tag=args.image_tag,
        create_efs=args.create_efs,
        strict=args.strict,
        dry_run=args.dry_run,
    )


if __name__ == '__main__':
    main()
