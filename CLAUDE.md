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

Three tiers, each with its own runtime cost / dependency surface. Pick the
narrowest one that proves your change.

```bash
# Tier 1 — unit + contract (fast, default loop, ~30s, all-mock)
pytest tests/unit/ tests/contract/ -q

# Tier 2 — integration (real terraform binary + moto/LocalStack, ~3-5min)
pytest tests/integration/ -q

# Tier 3 — e2e (real AWS, real money + time, gated)
RC_E2E=1 pytest tests/e2e/ -q
```

Markers (registered in pyproject.toml):
- `unit`, `contract`, `integration`, `e2e` — tier identification
- `slow` — anything over ~5s wall-clock; use `-m "not slow"` to skip

Conventions:
- Test settings: `tests/settings.py` (in-memory SQLite, test encryption key)
- Fixtures: `tests/conftest.py`
- Model factories: `tests/factories.py` (factory-boy)
- Mock SSH/AWS clients in tests
- Every file under `tests/integration/` declares `pytestmark = pytest.mark.integration`
  at module level so the marker is inherited by every test in the file.
- Same convention for `tests/e2e/`.

CI & enforcement (what the gate checks — see `.github/workflows/`):
- `ci.yml` (blocking, every PR/push): `lint` (black --check + flake8, config
  in `.flake8` — black owns line length, E501 ignored) · `fast-tier`
  (unit+contract on ubuntu py3.11/3.12 + macOS py3.12) · `coverage`
  (fast-tier with `--cov-fail-under=57` — a ratchet floor, raise as the suite
  grows). Branch protection (repo settings) makes these required to merge.
- Random order: `pytest-randomly` auto-activates, so the gate runs in a
  randomized order each time and prints `--randomly-seed=N` (reproduce a
  failure with `pytest -p randomly --randomly-seed=N`). The suite is
  order-independent — keep it that way.
- Runtime budget: a `conftest.py` hook fails the run if any unit/contract
  test's call phase exceeds 8s (override `RC_TEST_SLOW_BUDGET_S`) without
  `@pytest.mark.slow`. Either speed the test up or mark it slow.
- Hang guard: CI passes `--timeout=60` (pytest-timeout) on the fast tier.
- `flaky.yml` (nightly, non-blocking): runs the fast tier 3x with fresh
  seeds + a `--reruns` rerun report to surface flaky / order-dependent tests.

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


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files
- If `bd create` errors with `database not initialized: issue_prefix config is missing`,
  run `bd config set issue_prefix remote-compose` once. The dolt-backed runtime config
  doesn't round-trip cleanly through git on a fresh checkout (see `.beads/config.yaml`).

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
