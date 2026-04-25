# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Note**: This project uses [bd (beads)](https://github.com/steveyegge/beads)
for issue tracking. Use `bd` commands instead of markdown TODOs.
See AGENTS.md for workflow details.

## NO MARKDOWN FILES

You MUST use `bd` instead of generic markdown files for all summaries, task tracking, architecture reviews, etc. This will keep all project work in sync.

## Project Overview

Django Remote Compose is a Django reusable app for deploying Docker Compose applications to remote hosts via SSH and AWS ECS. It provides production-grade deployment with async support, health monitoring, multi-service orchestration, and audit logging.

## Build & Test Commands

```bash
# Install for development
pip install -e .
pip install -e ".[celery]"  # with async support
pip install -r requirements/dev.txt  # dev dependencies

# Run tests
pytest tests/ -v
pytest tests/ --cov=remote_compose --cov-report=html

# Run single test file
pytest tests/unit/test_services/test_compose_service.py -v

# Run specific test class
pytest tests/unit/test_services/test_compose_service.py::TestComposeServiceSecurity -v

# Code quality
black remote_compose/
flake8 remote_compose/
```

## Architecture

### Layer Structure

```
remote_compose/
├── models/         # Django models (12 models) - data layer
├── services/       # Business logic (20+ services) - service layer
├── management/     # Django CLI commands
├── tasks/          # Celery async tasks
├── api/            # DRF REST API (viewsets, serializers)
├── utils/          # SSH and crypto utilities
└── exceptions.py   # Hierarchical exception classes with error codes
```

### Core Service Pattern

Services use dependency injection and observer pattern (via BaseService):

```python
class DeploymentService(BaseService):
    def __init__(self, target_service=None, compose_service=None):
        self.target_service = target_service or TargetService()
        # ...
```

### Key Services

- **DeploymentService**: Main orchestrator for SSH-based deployments
- **ECSDeploymentService**: AWS ECS deployment orchestration
- **ComposeToECSConverter**: Converts docker-compose.yml → ECS task definitions
- **OrchestrationService**: Multi-service deployments with strategies (SEQUENTIAL, PARALLEL, ROLLING, CANARY)
- **HealthService**: Target and deployment health checks
- **CredentialService**: Fernet-encrypted credential storage/retrieval
- **AWSClientFactory**: Boto3 client pooling (singleton)

### Deployment Pipeline

Modular pipeline system in `services/deployment_pipeline/`:
- `PipelineStep` abstract base class
- `PipelineContext` shared state
- Steps: Initialization → Preprocessing → ECR → Build → EFS → ECS

### Exception Hierarchy (by error code range)

- 1000-1999: Validation errors
- 2000-2999: SSH connection errors
- 3000-3999: Docker errors
- 4000-4999: Deployment errors
- 5000-5999: Credential errors
- 6000-6999: AWS errors
- 7000-7999: ECS errors
- 8000-8999: ECR errors
- 9000-9999: EFS errors

### Models

Key models in `models/`:
- **DeploymentTarget**: Remote servers (SSH, TCP, Unix socket, ECS types)
- **Deployment**: Deployment tracking with status, rollback chains
- **SecureCredential**: Fernet-encrypted SSH keys and AWS credentials
- **ECSCluster/ECSTaskDefinition/ECSService**: AWS ECS resources
- **ECRRepository/EFSFileSystem**: AWS container registry and storage
- **AuditLog**: Compliance tracking

### Configuration

Settings via `REMOTE_COMPOSE` dict in Django settings. Key settings:
- `ENCRYPTION_KEY`: Required for credential encryption
- `SSH_AUTO_ADD_HOSTS`: False by default (strict host key verification)
- Rate limiting disabled by default
- Audit logging enabled by default

## Testing

- Test settings: `tests/settings.py` (in-memory SQLite, test encryption key)
- Fixtures: `tests/conftest.py`
- Model factories: `tests/factories.py` (factory-boy)
- Mock SSH/AWS clients in tests

## Scripts

- `scripts/deploy_to_ecs.py`: CLI tool for ECS deployments
- `scripts/local_test.py`: Local testing without SSH
- `scripts/deploy_to_aws.py`: AWS deployment helper

## Management Commands

Core commands in `management/commands/`:
- `create_target`, `list_targets`, `test_target`: Target management
- `deploy`, `list_deployments`, `deployment_logs`, `rollback`: Deployment ops
- `ecs_cluster`, `ecs_deploy`, `ecs_service`: ECS management

Never attribute Claude / AI in git commits — no `Co-Authored-By`, no `Generated with Claude` footer, plain message only.
