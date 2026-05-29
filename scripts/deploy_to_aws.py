#!/usr/bin/env python3
"""
Deploy to AWS EC2 instances using remote-compose.

This script:
1. Loads AWS credentials from a .env file
2. Discovers or connects to EC2 instances
3. Deploys docker-compose applications to them

Usage:
    # List available EC2 instances
    python scripts/deploy_to_aws.py --list-instances --env-file .django

    # Deploy to a specific instance by ID
    python scripts/deploy_to_aws.py /path/to/repo --instance-id i-1234567890abcdef0 --env-file .django

    # Deploy to an instance by name tag
    python scripts/deploy_to_aws.py /path/to/repo --instance-name my-server --env-file .django

    # Deploy to an existing target
    python scripts/deploy_to_aws.py /path/to/repo --target my-target --env-file .django
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

    with open(env_file_path, "r") as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue
            # Handle export VAR=value format
            if line.startswith("export "):
                line = line[7:]
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Remove quotes
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

    temp_dir = tempfile.mkdtemp(prefix="remote_compose_aws_")

    # Get encryption key from env or generate one for testing
    encryption_key = env_vars.get("REMOTE_COMPOSE_ENCRYPTION_KEY") or env_vars.get(
        "ENCRYPTION_KEY"
    )
    if not encryption_key:
        from cryptography.fernet import Fernet

        encryption_key = Fernet.generate_key().decode()
        print("Warning: No encryption key found, generated temporary key")

    settings.configure(
        DEBUG=True,
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": os.path.join(temp_dir, "aws_deploy.sqlite3"),
            }
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "remote_compose",
        ],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        REMOTE_COMPOSE={
            "ENCRYPTION_KEY": encryption_key,
            "DEPLOYMENT_TIMEOUT": 300,
            "SSH_CONNECT_TIMEOUT": 30,
            "SSH_COMMAND_TIMEOUT": 120,
            "SSH_AUTO_ADD_HOSTS": True,  # For new EC2 instances
            "AWS_DEFAULT_REGION": env_vars.get(
                "AWS_DEFAULT_REGION", env_vars.get("AWS_REGION", "us-east-1")
            ),
        },
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        SECRET_KEY="aws-deploy-script-key",
    )

    django.setup()

    from django.core.management import call_command

    call_command("migrate", "--run-syncdb", verbosity=0)

    return temp_dir


def check_aws_credentials():
    """Check if AWS credentials are configured."""
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")

    if not access_key or not secret_key:
        print("Error: AWS credentials not found in environment")
        print("Expected variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
        return False

    print("AWS credentials found:")
    print(f"  Access Key: {access_key[:4]}...{access_key[-4:]}")
    print(
        f"  Region: {os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-east-1'))}"
    )
    return True


def list_instances(region: str = None):
    """List available EC2 instances."""
    from remote_compose.services import AWSService

    aws_service = AWSService()
    region = region or os.environ.get(
        "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
    )

    print(f"\nDiscovering EC2 instances in {region}...")

    try:
        instances = aws_service.discover_instances(
            region=region,
            running_only=True,
        )

        if not instances:
            print("No running instances found")
            return []

        print(f"\nFound {len(instances)} running instance(s):\n")
        print(
            f"{'Instance ID':<22} {'Name':<30} {'Type':<12} {'Public IP':<16} {'State'}"
        )
        print("-" * 95)

        for inst in instances:
            name = inst.get("name", "-")[:28]
            print(
                f"{inst['instance_id']:<22} {name:<30} {inst.get('instance_type', '-'):<12} {inst.get('public_ip', '-'):<16} {inst.get('state', '-')}"
            )

        return instances

    except Exception as e:
        print(f"Error listing instances: {e}")
        return []


def find_instance_by_name(name: str, region: str = None) -> dict:
    """Find an EC2 instance by its Name tag."""
    from remote_compose.services import AWSService

    aws_service = AWSService()
    region = region or os.environ.get(
        "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
    )

    instances = aws_service.discover_instances(
        region=region,
        tag_filters={"tag:Name": name},
        running_only=True,
    )

    if not instances:
        raise ValueError(f"No running instance found with Name tag: {name}")

    if len(instances) > 1:
        print(f"Warning: Multiple instances found with name '{name}', using first one")

    return instances[0]


def find_compose_file(repo_path: str, compose_file: str = None) -> str:
    """Find the docker-compose file in the repository."""
    repo = Path(repo_path)

    if compose_file:
        path = repo / compose_file
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"Compose file not found: {path}")

    common_names = [
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ]

    for name in common_names:
        path = repo / name
        if path.exists():
            return str(path)

    raise FileNotFoundError(f"No docker-compose file found in {repo_path}")


def create_or_get_target(
    instance_id: str = None,
    instance_name: str = None,
    target_name: str = None,
    ssh_key_path: str = None,
    ssh_user: str = "ubuntu",
    region: str = None,
):
    """Create or get a deployment target."""
    from remote_compose.models import DeploymentTarget
    from remote_compose.services import AWSService

    # If target name provided, try to get existing target
    if target_name:
        try:
            target = DeploymentTarget.objects.get(name=target_name)
            print(f"Using existing target: {target.name} ({target.host})")
            return target
        except DeploymentTarget.DoesNotExist:
            pass

    # Need to create from EC2 instance
    aws_service = AWSService()
    region = region or os.environ.get(
        "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
    )

    # Find instance
    if instance_name:
        print(f"Looking for instance with Name tag: {instance_name}")
        instance = find_instance_by_name(instance_name, region)
        instance_id = instance["instance_id"]
    elif instance_id:
        print(f"Getting instance: {instance_id}")
        instance = aws_service.get_instance(instance_id, region)
    else:
        raise ValueError(
            "Must provide either --instance-id, --instance-name, or --target"
        )

    print(
        f"Found instance: {instance['instance_id']} ({instance.get('name', 'unnamed')})"
    )
    print(f"  Public IP: {instance.get('public_ip', 'N/A')}")
    print(f"  Type: {instance.get('instance_type')}")
    print(f"  Key Name: {instance.get('key_name')}")

    # Determine target name
    if not target_name:
        target_name = instance.get("name") or f"ec2-{instance_id[-8:]}"

    # SSH key
    if not ssh_key_path:
        # Try to find key based on instance key name
        key_name = instance.get("key_name")
        if key_name:
            possible_paths = [
                os.path.expanduser(f"~/.ssh/{key_name}.pem"),
                os.path.expanduser(f"~/.ssh/{key_name}"),
                f"/path/to/{key_name}.pem",
            ]
            for path in possible_paths:
                if os.path.exists(path):
                    ssh_key_path = path
                    break

    if not ssh_key_path:
        print(
            f"\nWarning: No SSH key path provided and couldn't find key for '{instance.get('key_name')}'"
        )
        print("You may need to specify --ssh-key /path/to/your-key.pem")

    # Create target
    print(f"\nCreating deployment target: {target_name}")

    target = aws_service.create_target_from_instance(
        instance_id=instance_id,
        target_name=target_name,
        ssh_key_path=ssh_key_path,
        ssh_username=ssh_user,
        region=region,
        validate_connection=True,
    )

    print(f"Target created: {target.name} ({target.host})")
    return target


def deploy(
    repo_path: str,
    target,
    compose_file: str = None,
    project_name: str = None,
    version: str = "",
    env_vars: dict = None,
    pull_images: bool = True,
    build_images: bool = False,
):
    """Deploy docker-compose application to target."""
    from remote_compose.services import DeploymentService
    from remote_compose.models import Deployment

    compose_path = find_compose_file(repo_path, compose_file)

    if not project_name:
        project_name = Path(repo_path).name

    print(f"\n{'=' * 60}")
    print("DEPLOYING TO AWS")
    print(f"{'=' * 60}")
    print(f"Target: {target.name} ({target.host})")
    print(f"Compose: {compose_path}")
    print(f"Project: {project_name}")
    if version:
        print(f"Version: {version}")
    print(f"{'=' * 60}\n")

    deployment_service = DeploymentService()

    try:
        deployment = deployment_service.deploy(
            target=target,
            compose_file_path=compose_path,
            project_name=project_name,
            environment=env_vars or {},
            version=version,
            deployed_by="aws-deploy-script",
            pull_images=pull_images,
            build_images=build_images,
            timeout=300,
        )

        print(f"\n{'=' * 60}")
        if deployment.status == Deployment.Status.SUCCESS:
            print("DEPLOYMENT SUCCESSFUL!")
            print(f"{'=' * 60}")
            print(f"  Deployment ID: {deployment.id}")
            print(f"  Duration: {deployment.duration:.1f}s")
            if deployment.container_ids:
                print(f"  Containers: {len(deployment.container_ids)}")
            print(f"\nAccess your app at: http://{target.host}")
        else:
            print("DEPLOYMENT FAILED!")
            print(f"{'=' * 60}")
            print(f"  Error: {deployment.error_message}")

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
        description="Deploy docker-compose applications to AWS EC2 instances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # List available EC2 instances
    python scripts/deploy_to_aws.py --list-instances --env-file .django

    # Deploy to instance by ID
    python scripts/deploy_to_aws.py examples/sample-app \\
        --instance-id i-1234567890abcdef0 \\
        --ssh-key ~/.ssh/my-key.pem \\
        --env-file .django

    # Deploy to instance by Name tag
    python scripts/deploy_to_aws.py /path/to/my-app \\
        --instance-name my-web-server \\
        --ssh-key ~/.ssh/my-key.pem \\
        --env-file .django

    # Deploy with project name and version
    python scripts/deploy_to_aws.py /path/to/my-app \\
        --instance-name prod-server \\
        --project-name myapp \\
        --version v1.0.0 \\
        --env-file .django
        """,
    )

    parser.add_argument(
        "repo_path",
        nargs="?",
        help="Path to the repository containing docker-compose.yml",
    )
    parser.add_argument(
        "--env-file",
        default=".django",
        help="Path to .env file with AWS credentials (default: .django)",
    )
    parser.add_argument(
        "--list-instances",
        action="store_true",
        help="List available EC2 instances and exit",
    )
    parser.add_argument("--instance-id", help="EC2 instance ID to deploy to")
    parser.add_argument("--instance-name", help="EC2 instance Name tag to deploy to")
    parser.add_argument("--target", help="Existing target name to deploy to")
    parser.add_argument(
        "--ssh-key", dest="ssh_key_path", help="Path to SSH private key file"
    )
    parser.add_argument(
        "--ssh-user", default="ubuntu", help="SSH username (default: ubuntu)"
    )
    parser.add_argument("--region", help="AWS region (default: from env or us-east-1)")
    parser.add_argument(
        "-f", "--compose-file", help="Docker compose file name (default: auto-detect)"
    )
    parser.add_argument(
        "-p",
        "--project-name",
        help="Docker Compose project name (default: directory name)",
    )
    parser.add_argument("--version", default="", help="Version tag for this deployment")
    parser.add_argument(
        "-e",
        "--env",
        action="append",
        dest="deploy_env_vars",
        help="Environment variables for deployment (KEY=VALUE)",
    )
    parser.add_argument("--no-pull", action="store_true", help="Skip pulling images")
    parser.add_argument(
        "--build", action="store_true", help="Build images before starting"
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

    # List instances mode
    if args.list_instances:
        list_instances(args.region)
        return

    # Need repo path for deployment
    if not args.repo_path:
        parser.print_help()
        print("\nError: repo_path is required for deployment")
        sys.exit(1)

    repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo_path):
        print(f"Error: Not a directory: {repo_path}")
        sys.exit(1)

    # Need target specification
    if not args.instance_id and not args.instance_name and not args.target:
        print(
            "\nError: Must specify one of: --instance-id, --instance-name, or --target"
        )
        print("\nUse --list-instances to see available EC2 instances")
        sys.exit(1)

    # Parse deployment environment variables
    deploy_env = {}
    if args.deploy_env_vars:
        for env_var in args.deploy_env_vars:
            if "=" not in env_var:
                print(f"Error: Invalid environment variable: {env_var}")
                sys.exit(1)
            key, value = env_var.split("=", 1)
            deploy_env[key] = value

    # Create or get target
    try:
        target = create_or_get_target(
            instance_id=args.instance_id,
            instance_name=args.instance_name,
            target_name=args.target,
            ssh_key_path=args.ssh_key_path,
            ssh_user=args.ssh_user,
            region=args.region,
        )
    except Exception as e:
        print(f"\nError creating target: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    # Deploy
    deploy(
        repo_path=repo_path,
        target=target,
        compose_file=args.compose_file,
        project_name=args.project_name,
        version=args.version,
        env_vars=deploy_env,
        pull_images=not args.no_pull,
        build_images=args.build,
    )


if __name__ == "__main__":
    main()
