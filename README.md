# Django Remote Compose

A robust Django reusable app for deploying Docker Compose applications to remote AWS EC2 servers via SSH. Features async deployments, health monitoring, multi-service orchestration, and comprehensive audit logging.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Usage Guide](#usage-guide)
  - [Basic Deployment](#basic-deployment)
  - [Async Deployments with Celery](#async-deployments-with-celery)
  - [Health Monitoring](#health-monitoring)
  - [Multi-Service Orchestration](#multi-service-orchestration)
  - [Notifications and Webhooks](#notifications-and-webhooks)
- [Management Commands](#management-commands)
- [API Reference](#api-reference)
- [Testing](#testing)
- [Security](#security)
- [License](#license)

## Features

- **Docker Context Management**: Create and manage Docker contexts for remote deployment targets
- **Docker Compose Deployment**: Deploy docker-compose.yml files to remote hosts via SSH
- **AWS EC2 Integration**: Auto-discover EC2 instances and create deployment targets
- **AWS ECS Integration**: Deploy to AWS ECS (Fargate or EC2) without SSH - automatically converts docker-compose to ECS task definitions
- **Async Deployments**: Celery tasks for background deployment operations
- **Health Monitoring**: Continuous health checks for targets and deployments
- **Multi-Service Orchestration**: Deploy multiple services with sequential, parallel, rolling, or canary strategies
- **Rate Limiting**: Protect against deployment abuse with configurable rate limits
- **Audit Logging**: Track all deployment-related actions for compliance
- **Secure Credential Storage**: Fernet-encrypted storage for SSH keys and AWS credentials
- **Log Sanitization**: Automatic masking of sensitive data in logs
- **Webhooks & Notifications**: Slack, email, and custom webhook notifications
- **Deployment History**: Full deployment tracking with rollback capability

## Installation

### From source:

```bash
git clone https://github.com/your-org/remote-compose.git
cd remote-compose
pip install -e .
```

### Install with Celery support (for async deployments):

```bash
pip install -e ".[celery]"
```

### Install development dependencies:

```bash
pip install -r requirements/dev.txt
```

## Quick Start

### 1. Add to Django settings

```python
INSTALLED_APPS = [
    # ... your apps
    'remote_compose',
]

# Generate encryption key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
REMOTE_COMPOSE = {
    'ENCRYPTION_KEY': 'your-fernet-key-here',  # REQUIRED
}
```

### 2. Run migrations

```bash
python manage.py migrate remote_compose
```

### 3. Create a deployment target

```bash
# Using management command
python manage.py create_target \
    --name prod-server \
    --host ec2-54-123-45-67.compute-1.amazonaws.com \
    --user ubuntu \
    --ssh-key ~/.ssh/prod.pem

# Or via Python
from remote_compose.services import TargetService

target_service = TargetService()
target = target_service.create_target(
    name='prod-server',
    host='54.123.45.67',
    username='ubuntu',
    ssh_key_path='/path/to/key.pem',
    validate_connection=True,  # Test SSH connection
)
```

### 4. Deploy your application

```bash
# Using management command
python manage.py deploy \
    --target prod-server \
    --compose-file docker-compose.yml \
    --project myapp \
    --version v1.0.0

# Or via Python
from remote_compose.services import DeploymentService

deployment_service = DeploymentService()
deployment = deployment_service.deploy(
    target=target,
    compose_file_path='./docker-compose.yml',
    project_name='myapp',
    version='v1.0.0',
    environment={'DEBUG': 'false'},
    deployed_by='admin',
)

print(f"Deployment {deployment.id} status: {deployment.status}")
```

## Configuration

Add these settings to your Django `settings.py` under `REMOTE_COMPOSE`:

```python
REMOTE_COMPOSE = {
    # ===================
    # REQUIRED SETTINGS
    # ===================

    # Fernet encryption key for credential storage
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    'ENCRYPTION_KEY': 'your-32-byte-url-safe-base64-key',

    # ===================
    # SSH SETTINGS
    # ===================

    'SSH_CONNECTION_TIMEOUT': 30,      # Connection timeout (seconds)
    'SSH_COMMAND_TIMEOUT': 300,        # Command execution timeout (seconds)
    'SSH_RETRY_ATTEMPTS': 3,           # Retry attempts for failed connections
    'SSH_RETRY_DELAY': 5,              # Delay between retries (seconds)
    'SSH_AUTO_ADD_HOSTS': False,       # Auto-add unknown hosts (ONLY for trusted networks!)

    # ===================
    # DEPLOYMENT SETTINGS
    # ===================

    'DEPLOYMENT_TIMEOUT': 600,         # Overall deployment timeout (seconds)
    'MAX_CONCURRENT_DEPLOYMENTS': 5,   # Max parallel deployments per target
    'DEPLOYMENT_LOG_RETENTION_DAYS': 90,
    'ENABLE_ROLLBACK': True,

    # ===================
    # DOCKER SETTINGS
    # ===================

    'DOCKER_COMPOSE_COMMAND': 'docker compose',  # or 'docker-compose' for older versions
    'DOCKER_COMMAND': 'docker',

    # ===================
    # AWS SETTINGS
    # ===================

    'AWS_DEFAULT_REGION': 'us-east-1',
    'EC2_SYNC_INTERVAL': 3600,         # EC2 discovery interval (seconds)

    # ===================
    # RATE LIMITING
    # ===================

    'RATE_LIMIT_ENABLED': True,
    'RATE_LIMIT_DEPLOYMENTS_PER_MINUTE': 10,   # Global limit
    'RATE_LIMIT_DEPLOYMENTS_PER_TARGET': 5,    # Per target
    'RATE_LIMIT_DEPLOYMENTS_PER_USER': 20,     # Per user (5 min window)
    'RATE_LIMIT_ROLLBACKS_PER_MINUTE': 5,

    # ===================
    # AUDIT LOGGING
    # ===================

    'AUDIT_LOG_ENABLED': True,
    'AUDIT_LOG_TO_DATABASE': True,
    'AUDIT_LOG_FILE': '/var/log/remote-compose/audit.log',  # Optional file logging
    'AUDIT_LOG_RETENTION_DAYS': 365,

    # ===================
    # NOTIFICATIONS
    # ===================

    'NOTIFICATION_CHANNELS': ['webhook', 'slack'],  # Enabled channels
    'NOTIFICATION_WEBHOOK_URLS': [
        'https://your-app.com/webhooks/deployment',
    ],
    'SLACK_WEBHOOK_URL': 'https://hooks.slack.com/services/xxx/yyy/zzz',
    'WEBHOOK_ALLOW_ALL_DOMAINS': False,  # Set True only for testing
    'WEBHOOK_ALLOWED_DOMAINS': {'your-app.com', 'hooks.slack.com'},

    # ===================
    # HEALTH CHECKS
    # ===================

    'HEALTH_CHECK_INTERVAL': 300,      # 5 minutes
    'HEALTH_CHECK_TIMEOUT': 30,
    'HEALTH_CHECK_ENABLED': True,

    # ===================
    # ORCHESTRATION
    # ===================

    'ORCHESTRATION_MAX_PARALLEL': 5,   # Max parallel deployments in orchestration
    'ORCHESTRATION_BATCH_SIZE': 2,     # Batch size for rolling deployments

    # ===================
    # CELERY (for async)
    # ===================

    'CELERY_TASK_QUEUE': 'remote_compose',
    'CELERY_TASK_RETRY_DELAY': 30,
    'CELERY_TASK_MAX_RETRIES': 3,

    # ===================
    # SECURITY
    # ===================

    'ENCRYPT_CREDENTIALS': True,
    'MASK_SENSITIVE_LOGS': True,

    # ===================
    # HOOKS
    # ===================

    'PRE_DEPLOY_HOOK': 'myapp.hooks.pre_deploy',   # dotted path to callable
    'POST_DEPLOY_HOOK': 'myapp.hooks.post_deploy',
    'ON_FAILURE_HOOK': 'myapp.hooks.on_failure',
}
```

## Usage Guide

### Basic Deployment

```python
from remote_compose.services import (
    TargetService,
    DeploymentService,
    CredentialService,
)

# =====================
# 1. Store SSH Key Securely
# =====================
credential_service = CredentialService()

ssh_credential = credential_service.create_ssh_key(
    name='prod-ssh-key',
    key_path='/path/to/private/key.pem',
    description='Production server SSH key',
    created_by='admin',
)

# =====================
# 2. Create Deployment Target
# =====================
target_service = TargetService()

target = target_service.create_target(
    name='prod-web-server',
    host='54.123.45.67',
    username='ubuntu',
    port=22,
    ssh_key=ssh_credential,  # Use stored credential
    validate_connection=True,
    tags={'environment': 'production', 'role': 'web'},
)

# =====================
# 3. Deploy Application
# =====================
deployment_service = DeploymentService()

deployment = deployment_service.deploy(
    target=target,
    compose_file_path='./docker-compose.prod.yml',
    project_name='myapp',
    version='v1.2.3',
    environment={
        'DATABASE_URL': 'postgres://...',
        'REDIS_URL': 'redis://...',
    },
    deployed_by='admin@example.com',
    pull_images=True,
    build_images=False,
)

print(f"Deployment ID: {deployment.id}")
print(f"Status: {deployment.status}")
print(f"Duration: {deployment.duration}s")

# =====================
# 4. Check Status
# =====================
status = deployment_service.get_status(deployment)
print(f"Container IDs: {status['container_ids']}")
print(f"Services: {status['service_status']}")

# =====================
# 5. Get Logs
# =====================
logs = deployment_service.get_logs(deployment, tail=100)
print(logs)

# =====================
# 6. Rollback if needed
# =====================
if something_went_wrong:
    rollback = deployment_service.rollback(
        deployment=previous_deployment,
        deployed_by='admin@example.com',
    )
```

### Async Deployments with Celery

First, configure Celery in your Django project:

```python
# celery.py
from celery import Celery

app = Celery('myproject')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
```

Then use async tasks:

```python
from remote_compose.tasks import (
    deploy_async,
    rollback_async,
    check_deployment_health,
    cleanup_old_deployments,
)

# =====================
# Async Deployment
# =====================
result = deploy_async.delay(
    target_id=target.id,
    compose_file_path='/app/docker-compose.yml',
    project_name='myapp',
    version='v1.2.3',
    environment={'DEBUG': 'false'},
    deployed_by='admin',
    webhook_url='https://myapp.com/webhooks/deploy',  # Optional notification
)

# Check task status
print(f"Task ID: {result.id}")
print(f"Status: {result.status}")

# Get result when ready
if result.ready():
    deployment_result = result.get()
    print(f"Deployment ID: {deployment_result['deployment_id']}")

# =====================
# Async Rollback
# =====================
rollback_result = rollback_async.delay(
    deployment_id=deployment.id,
    deployed_by='admin',
)

# =====================
# Schedule Health Checks (in Celery Beat)
# =====================
# celerybeat_schedule.py
CELERY_BEAT_SCHEDULE = {
    'check-all-targets-health': {
        'task': 'remote_compose.tasks.check_all_targets_health',
        'schedule': 300.0,  # Every 5 minutes
    },
    'cleanup-old-deployments': {
        'task': 'remote_compose.tasks.cleanup_old_deployments',
        'schedule': 86400.0,  # Daily
        'kwargs': {'retention_days': 90},
    },
    'monitor-stale-deployments': {
        'task': 'remote_compose.tasks.monitor_stale_deployments',
        'schedule': 3600.0,  # Hourly
        'kwargs': {'max_running_hours': 24},
    },
}
```

### Health Monitoring

```python
from remote_compose.services import HealthService

health_service = HealthService()

# =====================
# Check Single Target
# =====================
result = health_service.check_target_health(target)
print(f"Healthy: {result.healthy}")
print(f"Message: {result.message}")

# =====================
# Check All Targets
# =====================
report = health_service.check_all_targets_health()
print(f"Total: {report.total_checked}")
print(f"Healthy: {report.healthy_count}")
print(f"Unhealthy: {report.unhealthy_count}")

for result in report.results:
    if not result.healthy:
        print(f"  UNHEALTHY: {result.target_name} - {result.message}")

# =====================
# Check Deployment Health
# =====================
deployment_health = health_service.check_deployment_health(deployment)
print(f"All services running: {deployment_health.healthy}")
print(f"Services: {deployment_health.details['services']}")

# =====================
# Find Unhealthy Targets
# =====================
unhealthy = health_service.get_unhealthy_targets()
for target in unhealthy:
    print(f"Target {target.name} is unhealthy: {target.health_message}")

# =====================
# Find Stale Deployments
# =====================
stale = health_service.get_stale_deployments(max_running_hours=24)
for deployment in stale:
    print(f"Deployment {deployment.id} has been running for too long")

# =====================
# Custom Health Check
# =====================
result = health_service.run_custom_health_check(
    deployment=deployment,
    command='curl -f http://localhost:8080/health',
    expected_exit_code=0,
)
```

### Multi-Service Orchestration

Deploy multiple services across multiple targets with dependency management:

```python
from remote_compose.services import (
    OrchestrationService,
    ServiceDeployment,
    DeploymentStrategy,
)

orchestration = OrchestrationService()

# =====================
# Define Services
# =====================
services = [
    # Database must be deployed first
    ServiceDeployment(
        target_id=db_target.id,
        compose_file_path='./database/docker-compose.yml',
        project_name='database',
        version='v1.0.0',
        priority=0,  # Highest priority (deploys first)
    ),
    # API depends on database
    ServiceDeployment(
        target_id=api_target.id,
        compose_file_path='./api/docker-compose.yml',
        project_name='api',
        version='v1.0.0',
        depends_on=['database'],  # Wait for database
        environment={'DATABASE_URL': 'postgres://...'},
    ),
    # Frontend depends on API
    ServiceDeployment(
        target_id=web_target.id,
        compose_file_path='./frontend/docker-compose.yml',
        project_name='frontend',
        version='v1.0.0',
        depends_on=['api'],
    ),
]

# =====================
# Sequential Deployment
# =====================
result = orchestration.deploy_multiple(
    services=services,
    strategy=DeploymentStrategy.SEQUENTIAL,
    deployed_by='admin',
    rollback_on_failure=True,  # Rollback all if any fails
)

print(f"Success: {result.success}")
print(f"Deployed: {result.successful_count}/{result.total_services}")
print(f"Duration: {result.duration_seconds}s")

# =====================
# Parallel Deployment (no dependencies)
# =====================
result = orchestration.deploy_multiple(
    services=independent_services,
    strategy=DeploymentStrategy.PARALLEL,
    deployed_by='admin',
)

# =====================
# Rolling Deployment
# =====================
result = orchestration.deploy_multiple(
    services=services,
    strategy=DeploymentStrategy.ROLLING,
    deployed_by='admin',
    batch_size=2,  # Deploy 2 at a time
)

# =====================
# Canary Deployment
# =====================
result = orchestration.deploy_multiple(
    services=all_services,
    strategy=DeploymentStrategy.CANARY,
    deployed_by='admin',
    canary_target_id=canary_server.id,  # Deploy here first
)

# =====================
# Deploy Same Service to Multiple Targets
# =====================
result = orchestration.deploy_to_target_group(
    target_ids=[server1.id, server2.id, server3.id],
    compose_file_path='./docker-compose.yml',
    project_name='myapp',
    version='v1.0.0',
    strategy=DeploymentStrategy.ROLLING,
    batch_size=1,  # One at a time for zero-downtime
)

# =====================
# Preview Deployment Plan
# =====================
plan = orchestration.create_deployment_plan(
    services=services,
    strategy=DeploymentStrategy.SEQUENTIAL,
)
print("Deployment order:")
for step in plan['deployment_order']:
    print(f"  {step['order']}. {step['project_name']} -> {step['target']}")
```

### Notifications and Webhooks

```python
from remote_compose.tasks import (
    send_deployment_notification,
    send_webhook,
)

# =====================
# Send Notification After Deployment
# =====================
send_deployment_notification.delay(
    deployment_id=deployment.id,
    event='deployment.completed',
    channels=['webhook', 'slack'],  # Or omit for all configured
)

# =====================
# Send Custom Webhook
# =====================
send_webhook.delay(
    webhook_url='https://myapp.com/webhooks/custom',
    event='custom.event',
    payload={
        'message': 'Something happened',
        'data': {'key': 'value'},
    },
)

# =====================
# Webhook Payload Format
# =====================
# Your webhook endpoint will receive:
{
    "event": "deployment.completed",
    "timestamp": "2024-01-15T10:30:00Z",
    "data": {
        "deployment_id": 123,
        "project_name": "myapp",
        "version": "v1.0.0",
        "status": "success",
        "target": {
            "id": 1,
            "name": "prod-server",
            "host": "54.123.45.67"
        },
        "deployed_by": "admin",
        "duration_seconds": 45.2
    }
}
```

### Audit Logging

```python
from remote_compose.services import AuditService, AuditAction

audit = AuditService()

# =====================
# Manual Audit Logging
# =====================
audit.log(
    action=AuditAction.DEPLOYMENT_STARTED,
    actor='admin@example.com',
    resource_type='deployment',
    resource_id=deployment.id,
    resource_name=deployment.project_name,
    ip_address='192.168.1.100',
    details={'version': 'v1.0.0'},
)

# =====================
# Query Audit Logs
# =====================
logs = audit.query_logs(
    action='deployment.completed',
    actor='admin@example.com',
    start_date=timezone.now() - timedelta(days=7),
    limit=100,
)

for log in logs:
    print(f"{log.timestamp} - {log.action} by {log.actor}")

# =====================
# Activity Summary
# =====================
summary = audit.get_activity_summary(hours=24)
print(f"Total events: {summary['total_events']}")
print(f"Success: {summary['success_count']}")
print(f"Failures: {summary['failure_count']}")
print(f"Actions: {summary['action_counts']}")

# =====================
# Cleanup Old Logs
# =====================
deleted = audit.cleanup_old_logs(retention_days=365)
print(f"Deleted {deleted} old audit logs")
```

### Rate Limiting

```python
from remote_compose.services import DeploymentRateLimiter, RateLimitExceeded

rate_limiter = DeploymentRateLimiter()

# =====================
# Check Before Deploying
# =====================
status = rate_limiter.check_deploy_allowed(
    target_id=target.id,
    user='admin',
)

print(f"Per-target remaining: {status['per_target'].remaining}")
print(f"Per-user remaining: {status['per_user'].remaining}")
print(f"Global remaining: {status['global'].remaining}")

# =====================
# Use with Deployment
# =====================
try:
    # This will raise RateLimitExceeded if limit reached
    rate_limiter.consume_deploy(
        target_id=target.id,
        user='admin',
    )

    # Proceed with deployment
    deployment = deployment_service.deploy(...)

except RateLimitExceeded as e:
    print(f"Rate limited! Retry after {e.retry_after} seconds")
```

### AWS ECS Deployments

Deploy docker-compose applications to AWS ECS (Fargate or EC2) without requiring SSH access. The library automatically converts your docker-compose.yml to ECS task definitions.

```python
from remote_compose.services import ECSService, ECSDeploymentService
from remote_compose.models import ECSCluster

# Create or import an ECS cluster
ecs_service = ECSService()

# Create a new Fargate cluster
cluster = ecs_service.create_cluster(
    name='my-app-cluster',
    region='us-east-1',
    capacity_providers=['FARGATE', 'FARGATE_SPOT'],
)

# Or import an existing cluster
cluster = ecs_service.import_cluster(
    cluster_name_or_arn='existing-cluster',
    region='us-east-1',
)

# Deploy a docker-compose application to ECS
deployment_service = ECSDeploymentService()

deployment = deployment_service.deploy(
    cluster=cluster,
    compose_file_path='/path/to/docker-compose.yml',
    project_name='myapp',
    desired_count=2,  # Run 2 tasks
    cpu='512',        # Fargate CPU units
    memory='1024',    # Memory in MB
    wait_for_stable=True,
    timeout=300,
)

print(f"Deployed to ECS: {deployment.metadata['service_arn']}")
```

#### Using the ECS Deploy Script

```bash
# List ECS clusters
python scripts/deploy_to_ecs.py --list-clusters --env-file .django

# Create a new Fargate cluster
python scripts/deploy_to_ecs.py --create-cluster my-cluster --env-file .django

# Deploy to an existing cluster
python scripts/deploy_to_ecs.py examples/sample-app \
    --cluster my-cluster \
    --env-file .django

# Deploy with custom resources and multiple tasks
python scripts/deploy_to_ecs.py examples/sample-app \
    --cluster my-cluster \
    --cpu 512 \
    --memory 1024 \
    --desired-count 2 \
    --env-file .django
```

#### ECS Management Commands

```bash
# Manage clusters
python manage.py ecs_cluster list
python manage.py ecs_cluster create my-cluster --region us-east-1
python manage.py ecs_cluster import existing-cluster --region us-east-1
python manage.py ecs_cluster show my-cluster
python manage.py ecs_cluster delete my-cluster --delete-aws

# Deploy to ECS
python manage.py ecs_deploy \
    --cluster my-cluster \
    --compose-file docker-compose.yml \
    --project-name myapp \
    --desired-count 2

# Manage services
python manage.py ecs_service list --cluster my-cluster
python manage.py ecs_service show myapp --cluster my-cluster
python manage.py ecs_service scale myapp --cluster my-cluster --count 3
python manage.py ecs_service restart myapp --cluster my-cluster
python manage.py ecs_service delete myapp --cluster my-cluster
```

#### Compose to ECS Conversion

The library automatically handles:
- Converting docker-compose services to ECS container definitions
- Port mappings, environment variables, and health checks
- Resource allocation (rounds up to valid Fargate CPU/memory combinations)
- Container dependencies (depends_on)
- CloudWatch Logs configuration

Limitations:
- Build contexts require pre-built images pushed to a registry (ECR, Docker Hub)
- Host volume mounts are not supported in Fargate (use EFS instead)
- Some docker-compose features have no ECS equivalent

## Management Commands

```bash
# Create a deployment target
python manage.py create_target \
    --name prod-server \
    --host 54.123.45.67 \
    --user ubuntu \
    --ssh-key ~/.ssh/key.pem \
    --port 22 \
    --tags environment=production,role=web

# List all targets
python manage.py list_targets
python manage.py list_targets --status healthy
python manage.py list_targets --environment production

# Test target connection
python manage.py test_target prod-server

# Deploy
python manage.py deploy \
    --target prod-server \
    --compose-file docker-compose.yml \
    --project myapp \
    --version v1.0.0 \
    --env DEBUG=false \
    --env DATABASE_URL=postgres://...

# List deployments
python manage.py list_deployments
python manage.py list_deployments --target prod-server
python manage.py list_deployments --status success

# View deployment logs
python manage.py deployment_logs 123  # deployment ID
python manage.py deployment_logs 123 --level error

# Rollback
python manage.py rollback 120  # rollback to deployment ID 120
```

## Testing

### Local Testing with Docker Compose

Test the library locally with your own docker-compose applications:

```bash
# Deploy a local repo using direct docker-compose (no SSH required)
python scripts/local_test.py /path/to/your/repo --direct

# Use a specific compose file
python scripts/local_test.py /path/to/your/repo -f docker-compose.dev.yml --direct

# With project name and version
python scripts/local_test.py /path/to/your/repo -p myapp --version v1.0.0 --direct

# Pass environment variables
python scripts/local_test.py /path/to/your/repo -e DEBUG=true -e API_KEY=test --direct

# Stop a deployment
python scripts/local_test.py --stop myapp

# Test with the included sample app
python scripts/local_test.py examples/sample-app --direct -p test-app
```

To use the full library mode (with SSH to localhost):
1. Enable Remote Login in System Preferences > Sharing (macOS) or ensure `sshd` is running
2. Add your SSH key: `ssh-copy-id localhost`
3. Run without `--direct` flag: `python scripts/local_test.py /path/to/repo`

### Run All Tests

```bash
# Install test dependencies
pip install -r requirements/dev.txt

# Run tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=remote_compose --cov-report=html

# Run specific test file
python -m pytest tests/unit/test_services/test_compose_service.py -v

# Run only security tests
python -m pytest tests/unit/test_services/test_compose_service.py::TestComposeServiceSecurity -v
```

### Test Configuration

Tests use a separate settings file (`tests/settings.py`):

```python
# The test settings automatically configure:
# - In-memory SQLite database
# - Test encryption key
# - SSH auto-add hosts (mocked anyway)
```

### Writing Tests

```python
import pytest
from unittest.mock import MagicMock, patch
from remote_compose.services import DeploymentService

@pytest.mark.django_db
class TestMyFeature:

    @pytest.fixture
    def mock_ssh(self, mocker):
        """Mock SSH client for all tests."""
        mock = mocker.patch('remote_compose.services.target_service.SSHClient')
        instance = mock.return_value
        instance.test_connection.return_value = (True, 'Success')
        instance.execute.return_value = MagicMock(success=True, stdout='OK')
        return mock

    def test_deployment(self, mock_ssh):
        service = DeploymentService()
        # ... test implementation
```

## Security

### SSH Host Key Verification

By default, the library uses strict host key verification:

```python
# Host must be in ~/.ssh/known_hosts or connection fails
REMOTE_COMPOSE = {
    'SSH_AUTO_ADD_HOSTS': False,  # Default - strict mode
}

# For trusted networks (e.g., private VPC), you can enable auto-add:
REMOTE_COMPOSE = {
    'SSH_AUTO_ADD_HOSTS': True,  # Only in trusted networks!
}
```

### Credential Encryption

All credentials are encrypted using Fernet symmetric encryption:

```python
# Generate a key
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())

# Store in settings (use environment variable in production!)
REMOTE_COMPOSE = {
    'ENCRYPTION_KEY': os.environ['REMOTE_COMPOSE_ENCRYPTION_KEY'],
}
```

### Log Sanitization

Sensitive data is automatically masked in logs:

```python
# Automatically masked:
# - Passwords, secrets, tokens
# - SSH private keys
# - AWS credentials
# - JWT tokens
# - URLs with credentials

# The sanitizer can be extended:
from remote_compose.services import LogSanitizer

sanitizer = LogSanitizer()
sanitizer.add_sensitive_field('my_custom_secret')
sanitizer.add_pattern(r'CUSTOM_\d{4}', '[REDACTED]')
```

### Command Injection Prevention

All user inputs are validated to prevent command injection:

- Project names must match Docker Compose naming rules
- Paths are validated for traversal attacks
- Environment variables are validated and protected vars cannot be overridden
- Shell values are properly escaped using `shlex.quote()`

### Webhook Security

Webhooks are protected against SSRF attacks:

```python
REMOTE_COMPOSE = {
    'WEBHOOK_ALLOW_ALL_DOMAINS': False,  # Default
    'WEBHOOK_ALLOWED_DOMAINS': {'hooks.slack.com', 'your-app.com'},
}
```

## License

MIT License - see LICENSE file for details.
