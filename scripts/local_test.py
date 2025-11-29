#!/usr/bin/env python3
"""
Local testing script for remote-compose library.

This script allows you to test the remote-compose library locally by:
1. Setting up a minimal Django environment
2. Creating a localhost deployment target
3. Deploying a docker-compose application from a local path

Usage:
    python scripts/local_test.py /path/to/your/repo
    python scripts/local_test.py /path/to/your/repo --compose-file docker-compose.dev.yml
    python scripts/local_test.py /path/to/your/repo --project-name myapp --version v1.0.0

Requirements:
    - Docker and docker-compose installed locally
    - SSH server running on localhost (for SSH mode) OR use --local flag
    - SSH key configured for localhost access (for SSH mode)
"""

import argparse
import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path

# Add the project root to the path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def setup_django():
    """Configure Django settings for standalone use."""
    import django
    from django.conf import settings

    if settings.configured:
        return

    # Create a temporary directory for the SQLite database
    temp_dir = tempfile.mkdtemp(prefix='remote_compose_test_')

    settings.configure(
        DEBUG=True,
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': os.path.join(temp_dir, 'test_db.sqlite3'),
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
            'DEPLOYMENT_TIMEOUT': 300,
            'SSH_CONNECT_TIMEOUT': 30,
            'SSH_COMMAND_TIMEOUT': 120,
            'MASK_SENSITIVE_LOGS': True,
        },
        CACHES={
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            }
        },
        SECRET_KEY='test-secret-key-for-local-testing-only',
    )

    django.setup()

    # Run migrations
    from django.core.management import call_command
    call_command('migrate', '--run-syncdb', verbosity=0)

    return temp_dir


def check_docker():
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            print("Error: Docker is not running or not accessible")
            print(result.stderr)
            return False
        return True
    except FileNotFoundError:
        print("Error: Docker is not installed")
        return False
    except subprocess.TimeoutExpired:
        print("Error: Docker command timed out")
        return False


def check_docker_compose():
    """Check if docker-compose is available."""
    # Try docker compose (v2)
    try:
        result = subprocess.run(
            ['docker', 'compose', 'version'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return 'docker compose'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try docker-compose (v1)
    try:
        result = subprocess.run(
            ['docker-compose', 'version'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return 'docker-compose'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("Error: docker-compose is not installed")
    return None


def check_ssh_localhost():
    """Check if SSH to localhost is available."""
    try:
        result = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'localhost', 'echo', 'ok'],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def find_compose_file(repo_path: str, compose_file: str = None) -> str:
    """Find the docker-compose file in the repository."""
    repo = Path(repo_path)

    if compose_file:
        path = repo / compose_file
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"Compose file not found: {path}")

    # Try common names
    common_names = [
        'docker-compose.yml',
        'docker-compose.yaml',
        'compose.yml',
        'compose.yaml',
        'docker-compose.dev.yml',
        'docker-compose.local.yml',
    ]

    for name in common_names:
        path = repo / name
        if path.exists():
            return str(path)

    raise FileNotFoundError(
        f"No docker-compose file found in {repo_path}. "
        f"Tried: {', '.join(common_names)}"
    )


def create_local_target(use_ssh: bool = True):
    """Create a localhost deployment target."""
    from remote_compose.models import DeploymentTarget

    # Check if target already exists
    try:
        target = DeploymentTarget.objects.get(name='localhost')
        print(f"Using existing target: localhost ({target.host})")
        return target
    except DeploymentTarget.DoesNotExist:
        pass

    if use_ssh:
        # Create SSH-based localhost target
        target = DeploymentTarget.objects.create(
            name='localhost',
            host='127.0.0.1',
            port=22,
            username=os.environ.get('USER', 'root'),
            target_type=DeploymentTarget.TargetType.SSH,
            environment=DeploymentTarget.Environment.DEVELOPMENT,
            description='Local development machine (SSH)',
            health_status=DeploymentTarget.HealthStatus.UNKNOWN,
            metadata={'created_by': 'local_test.py'},
        )
    else:
        # Create local target (will need special handling)
        target = DeploymentTarget.objects.create(
            name='localhost',
            host='127.0.0.1',
            port=0,  # No SSH
            username=os.environ.get('USER', 'root'),
            target_type='local',  # Custom type
            environment=DeploymentTarget.Environment.DEVELOPMENT,
            description='Local development machine (direct)',
            health_status=DeploymentTarget.HealthStatus.HEALTHY,
            metadata={'created_by': 'local_test.py', 'local_mode': True},
        )

    print(f"Created target: localhost")
    return target


def deploy_local(
    repo_path: str,
    compose_file: str = None,
    project_name: str = None,
    version: str = '',
    env_vars: dict = None,
    pull_images: bool = True,
    build_images: bool = False,
):
    """Deploy a docker-compose application locally using the library."""
    from remote_compose.services import DeploymentService, TargetService
    from remote_compose.models import Deployment

    # Find compose file
    compose_path = find_compose_file(repo_path, compose_file)
    print(f"\nCompose file: {compose_path}")

    # Get or create project name
    if not project_name:
        project_name = Path(repo_path).name
    print(f"Project name: {project_name}")

    # Create localhost target
    target = create_local_target(use_ssh=True)

    # Test target connection
    target_service = TargetService()
    print("\nTesting connection to localhost...")

    try:
        result = target_service.test_connection(target)
        if result['success']:
            print(f"  Connection OK: {result['message']}")
        else:
            print(f"  Connection FAILED: {result['message']}")
            print("\nTip: Make sure SSH is enabled and you can 'ssh localhost' without password")
            return None
    except Exception as e:
        print(f"  Connection error: {e}")
        print("\nTip: Make sure SSH is enabled and you can 'ssh localhost' without password")
        return None

    # Deploy
    deployment_service = DeploymentService()

    print(f"\nStarting deployment...")
    print(f"  Target: {target.name} ({target.host})")
    print(f"  Project: {project_name}")
    if version:
        print(f"  Version: {version}")
    if env_vars:
        print(f"  Environment: {len(env_vars)} variables")

    try:
        deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_path,
            project_name=project_name,
            environment=env_vars or {},
            version=version,
            deployed_by='local_test',
            pull_images=pull_images,
            build_images=build_images,
            timeout=300,
        )

        print(f"\n{'=' * 50}")
        if deployment.status == Deployment.Status.SUCCESS:
            print(f"DEPLOYMENT SUCCESSFUL!")
            print(f"{'=' * 50}")
            print(f"  Deployment ID: {deployment.id}")
            print(f"  Status: {deployment.status}")
            print(f"  Duration: {deployment.duration:.1f}s")
            if deployment.container_ids:
                print(f"  Containers: {len(deployment.container_ids)}")
                for cid in deployment.container_ids[:5]:
                    print(f"    - {cid[:12]}")
            if deployment.service_status:
                print(f"\n  Service Status:")
                for svc, status in deployment.service_status.items():
                    state = status.get('state', 'unknown')
                    print(f"    - {svc}: {state}")
        else:
            print(f"DEPLOYMENT FAILED!")
            print(f"{'=' * 50}")
            print(f"  Status: {deployment.status}")
            print(f"  Error: {deployment.error_message}")

        return deployment

    except Exception as e:
        print(f"\n{'=' * 50}")
        print(f"DEPLOYMENT ERROR!")
        print(f"{'=' * 50}")
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def deploy_local_direct(
    repo_path: str,
    compose_file: str = None,
    project_name: str = None,
    version: str = '',
    env_vars: dict = None,
    pull_images: bool = True,
    build_images: bool = False,
):
    """
    Deploy directly without SSH (bypasses the library for local testing).

    This is a fallback when SSH to localhost is not available.
    """
    compose_path = find_compose_file(repo_path, compose_file)
    print(f"\nCompose file: {compose_path}")

    if not project_name:
        project_name = Path(repo_path).name
    print(f"Project name: {project_name}")

    compose_cmd = check_docker_compose()
    if not compose_cmd:
        return None

    print(f"\nUsing: {compose_cmd}")
    print(f"Starting deployment (direct mode)...")

    # Build command
    if compose_cmd == 'docker compose':
        cmd = ['docker', 'compose', '-f', compose_path, '-p', project_name]
    else:
        cmd = ['docker-compose', '-f', compose_path, '-p', project_name]

    # Pull images
    if pull_images:
        print("  Pulling images...")
        pull_cmd = cmd + ['pull']
        result = subprocess.run(pull_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  Warning: Pull failed: {result.stderr}")

    # Build if requested
    if build_images:
        print("  Building images...")
        build_cmd = cmd + ['build']
        result = subprocess.run(build_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  Warning: Build failed: {result.stderr}")

    # Start containers
    print("  Starting containers...")
    up_cmd = cmd + ['up', '-d']

    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    result = subprocess.run(up_cmd, capture_output=True, text=True, env=env)

    print(f"\n{'=' * 50}")
    if result.returncode == 0:
        print(f"DEPLOYMENT SUCCESSFUL!")
        print(f"{'=' * 50}")
        print(result.stdout)

        # Get container status
        ps_cmd = cmd + ['ps']
        ps_result = subprocess.run(ps_cmd, capture_output=True, text=True)
        if ps_result.returncode == 0:
            print("\nContainer Status:")
            print(ps_result.stdout)
    else:
        print(f"DEPLOYMENT FAILED!")
        print(f"{'=' * 50}")
        print(f"Error: {result.stderr}")

    return result.returncode == 0


def stop_deployment(project_name: str):
    """Stop a deployed project."""
    compose_cmd = check_docker_compose()
    if not compose_cmd:
        return

    if compose_cmd == 'docker compose':
        cmd = ['docker', 'compose', '-p', project_name, 'down']
    else:
        cmd = ['docker-compose', '-p', project_name, 'down']

    print(f"\nStopping {project_name}...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        print("Stopped successfully")
    else:
        print(f"Error: {result.stderr}")


def main():
    parser = argparse.ArgumentParser(
        description='Test remote-compose library with local docker-compose deployments',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Deploy from a local repo
    python scripts/local_test.py /path/to/my-app

    # Use a specific compose file
    python scripts/local_test.py /path/to/my-app -f docker-compose.dev.yml

    # With custom project name and version
    python scripts/local_test.py /path/to/my-app -p myproject --version v1.0.0

    # Pass environment variables
    python scripts/local_test.py /path/to/my-app -e DEBUG=true -e API_KEY=test

    # Use direct mode (no SSH, bypass library)
    python scripts/local_test.py /path/to/my-app --direct

    # Stop a deployment
    python scripts/local_test.py --stop myproject
        """
    )

    parser.add_argument(
        'repo_path',
        nargs='?',
        help='Path to the repository containing docker-compose.yml'
    )
    parser.add_argument(
        '-f', '--compose-file',
        help='Name of the docker-compose file (default: auto-detect)'
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
        dest='env_vars',
        help='Environment variables (KEY=VALUE)'
    )
    parser.add_argument(
        '--no-pull',
        action='store_true',
        help='Skip pulling images'
    )
    parser.add_argument(
        '--build',
        action='store_true',
        help='Build images before starting'
    )
    parser.add_argument(
        '--direct',
        action='store_true',
        help='Use direct docker-compose commands (bypass library)'
    )
    parser.add_argument(
        '--stop',
        metavar='PROJECT',
        help='Stop a deployed project'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='Show status of all deployments'
    )

    args = parser.parse_args()

    # Handle stop command
    if args.stop:
        stop_deployment(args.stop)
        return

    # Require repo_path for deployment
    if not args.repo_path:
        parser.print_help()
        print("\nError: repo_path is required for deployment")
        sys.exit(1)

    # Validate repo path
    repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo_path):
        print(f"Error: Not a directory: {repo_path}")
        sys.exit(1)

    # Check Docker
    print("Checking prerequisites...")
    if not check_docker():
        sys.exit(1)
    print("  Docker: OK")

    compose_cmd = check_docker_compose()
    if not compose_cmd:
        sys.exit(1)
    print(f"  Docker Compose: OK ({compose_cmd})")

    # Parse environment variables
    env_vars = {}
    if args.env_vars:
        for env_var in args.env_vars:
            if '=' not in env_var:
                print(f"Error: Invalid environment variable: {env_var}")
                print("  Expected format: KEY=VALUE")
                sys.exit(1)
            key, value = env_var.split('=', 1)
            env_vars[key] = value

    if args.direct:
        # Direct mode - bypass library
        print("\nUsing direct mode (bypassing remote-compose library)")
        deploy_local_direct(
            repo_path=repo_path,
            compose_file=args.compose_file,
            project_name=args.project_name,
            version=args.version,
            env_vars=env_vars,
            pull_images=not args.no_pull,
            build_images=args.build,
        )
    else:
        # Library mode - use remote-compose
        print("\nUsing remote-compose library mode")

        # Check SSH to localhost
        if check_ssh_localhost():
            print("  SSH to localhost: OK")
        else:
            print("  SSH to localhost: NOT AVAILABLE")
            print("\nTo use library mode, enable SSH to localhost:")
            print("  1. Enable Remote Login in System Preferences > Sharing")
            print("  2. Add your SSH key: ssh-copy-id localhost")
            print("\nOr use --direct flag to bypass the library")
            sys.exit(1)

        # Setup Django and database
        print("\nSetting up test environment...")
        temp_dir = setup_django()
        print(f"  Database: {temp_dir}/test_db.sqlite3")

        try:
            deploy_local(
                repo_path=repo_path,
                compose_file=args.compose_file,
                project_name=args.project_name,
                version=args.version,
                env_vars=env_vars,
                pull_images=not args.no_pull,
                build_images=args.build,
            )
        finally:
            # Cleanup temp database (optional - keep for debugging)
            # shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"\nTest database preserved at: {temp_dir}")


if __name__ == '__main__':
    main()
