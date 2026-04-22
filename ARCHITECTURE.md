# Architecture

`remote-compose` deploys unmodified `docker-compose.yml` files to multiple
clouds by generating terraform modules and running them. Terraform owns the
infrastructure state; `remote-compose` owns image build/push, the `rc` CLI,
per-service operations (logs, exec, rollback), and rc.yml-based tuning.

## Quick start — 2 minutes

```bash
# 1. Install (with the provider extras you need)
pip install -e ".[ecs]"                  # or [k8s], or [all]

# 2. Verify the CLI is on $PATH
rc --help                                # if missing: python3 -m remote_compose.cli --help

# 3. In your app repo (the one with docker-compose.yml):
cd /path/to/your-app

# 4a. Upgrading from a legacy rc.yml? Migrate first:
rc migrate --in rc.yml --out rc.v2.yml

# 4b. Fresh project? Scaffold one:
rc init                                  # writes a v2 rc.yml template

# 5. Configure cloud creds (ECS example)
export AWS_PROFILE=myprofile
export AWS_REGION=us-west-2

# 6. Preview → deploy
rc plan
rc deploy
```

**Prereqs on your box:** `terraform >= 1.5`, `docker`, `python >= 3.9`, and
cloud creds for the provider you picked.

## Core idea

```
  docker-compose.yml  ──▶  Provider  ──▶  terraform HCL  ──▶  terraform apply  ──▶  running stack
         +                (ecs / k8s / …)      │                      │
   rc.yml (v2)                                 │                      └─▶ backend state (s3/gcs/local)
         +                                     │
   ImageBuilder                                └─▶ FR-7: self-contained module
         +                                         (user can `cd && terraform apply` without rc)
   ImagePusher
```

The compose file is the source of truth for services. `rc.yml v2` tunes how
the provider shapes resources (CPU/mem, launch type, secrets, domain).
Providers emit HCL — `rc` is a generator + convenience wrapper, never a lock-in.

## Data flow

What actually moves through the system on a deploy:

```
  ┌──────────────────────┐      ┌──────────────────────┐
  │ docker-compose.yml   │      │ rc.yml (v2)          │
  │  (services, images,  │      │  (provider, CPU/mem, │
  │   ports, volumes)    │      │   secrets, backend)  │
  └──────────┬───────────┘      └──────────┬───────────┘
             │                             │
             │     compose/loader          │     config/v2_schema.parse
             ▼                             ▼
           ┌───────────────────────────────────────────┐
           │          DeployContext                    │
           │  project, services{name→ServiceSpec},     │
           │  provider_config, tf_backend_config,      │
           │  secrets[SecretRef], working_dir          │
           └───────────────────┬───────────────────────┘
                               │
                               │ Provider.deploy(ctx)
                               ▼
       ┌───────────────────────┴──────────────────────────┐
       │                                                  │
       ▼                                                  ▼
  ┌──────────────┐                               ┌─────────────────┐
  │ HCL files    │◀──── TerraformEmitter ────┤ ServiceSpec list │
  │ (main.tf,    │       (Jinja2, sorted,    │  (view objects   │
  │  services.tf,│        deterministic)     │   for templates) │
  │  alb.tf, …)  │                           └─────────────────┘
  └──────┬───────┘
         │ terraform init / plan / apply (subprocess)
         ▼
  ┌──────────────┐                           ┌──────────────────┐
  │ tf state     │─── terraform output ─────▶│ DeployResult     │
  │ (s3/gcs/     │                            │  revision_id,    │
  │  local)      │                            │  outputs,        │
  └──────┬───────┘                            │  services,       │
         │ boto3 describe / logs /            │  duration_s      │
         │ kubectl / execute_command          └──────────────────┘
         ▼
  ┌──────────────────────────────┐
  │  live cluster ◀─▶ boto3 ─────┼─▶ StatusReport, logs stream, ExecResult
  └──────────────────────────────┘
```

**Invariant**: secret *values* never enter the HCL files or terraform state.
rc.yml carries *references* (path, ARN, k8s secret name). Providers plumb
these refs into the task definition's `secrets:` block so the cloud runtime
resolves them at container start.

## Dependency graph (what imports what)

```
  ┌────────────────────────────────────────────────────────────────┐
  │                           cli.py                               │
  └───────┬──────────────┬───────────────┬──────────────┬──────────┘
          │              │               │              │
          ▼              ▼               ▼              ▼
      config/       provider/        image/         (legacy)
      v2_schema     __init__,        builder,       services/
      migrate       base  (ABC)      pusher         …
                       │                             (being retired)
            ┌──────────┼───────────────────┐
            ▼          ▼                   ▼
         fake.py    ecs/provider.py    k8s/provider.py (future)
            │          │                   │
            │          ├──▶ boto3 [ecs]    ├──▶ kubernetes [k8s]
            │          │                   │
            └──────────┴─▶ terraform/      ◀─┘
                            runner         (subprocess wrapper)
                            emitter        (Jinja2 renderer)
                            backend        (HCL block generator)
```

Rules (enforced by human review + extras in `pyproject.toml`):
- **core never imports cloud SDKs**: `provider/base.py`, `provider/fake.py`,
  `config/*`, `terraform/*`, `image/*` all run with zero cloud deps.
- Each real provider is its own optional extra: `pip install remote-compose[ecs]`.
- `FakeProvider` + the contract test suite can run with neither extra installed.

## Action flow (per CLI command)

```
  rc migrate ──▶ config.v1_schema.load
              ──▶ config.migrate.migrate(v1_dict)
              ──▶ yaml.safe_dump(v2)

  rc plan    ──▶ load rc.yml v2
              ──▶ Provider.plan(ctx)
                    ├─ emit_terraform → HCL files
                    ├─ terraform init
                    └─ terraform plan → PlanResult(create/update/destroy)

  rc deploy  ──▶ load rc.yml v2
              ──▶ Provider.deploy(ctx)
                    ├─ emit_terraform
                    ├─ terraform init
                    ├─ ImageBuilder.build(spec)   (if any service has build:)
                    ├─ ImagePusher.push(tags)     (authenticator = provider-supplied)
                    ├─ terraform apply
                    └─ terraform output -json     → DeployResult

  rc redeploy──▶ (no terraform changes)
              ──▶ Provider.redeploy(ctx, services=None)
                    └─ provider-specific force-new-rollout
                       (ECS: ecs.update_service(forceNewDeployment=True))

  rc status  ──▶ Provider.status(ctx)
                    ├─ query live cluster (boto3 / kubectl)
                    └─ read terraform output for ingress URL

  rc logs    ──▶ Provider.logs(ctx, service, follow, tail)
                    └─ CloudWatch Logs / `kubectl logs`

  rc exec    ──▶ Provider.exec(ctx, service, command)
                    └─ ecs execute-command (SSM) / `kubectl exec`

  rc rollback──▶ Provider.rollback(ctx, to_revision)
                    ├─ local backend  → ProviderError (with actionable msg)
                    └─ remote backend → terraform state history restore

  rc destroy ──▶ Provider.destroy(ctx)
                    ├─ terraform init
                    └─ terraform destroy
```

Every command returns a typed result (`DeployResult`, `PlanResult`,
`StatusReport`, `ExecResult`) or raises a `ProviderError` subclass. The CLI
sanitizes errors and picks the exit code; `--verbose` promotes to traceback.

## Layers

```
remote_compose/
├── cli.py                       # Click entry point (rc deploy / plan / …)
│
├── config/                      # rc.yml parsing
│   ├── v1_schema.py             # legacy loader
│   ├── v2_schema.py             # dataclass model + validation
│   └── migrate.py               # v1 → v2 converter (used by `rc migrate`)
│
├── provider/                    # Pluggable cloud deployers
│   ├── base.py                  # Provider ABC + dataclasses (no cloud deps)
│   ├── __init__.py              # registry: register() / get() / available()
│   ├── fake.py                  # FakeProvider — in-memory, test baseline
│   └── ecs/                     # AWS ECS (Fargate today; EC2 in rc-e5u.13)
│       ├── provider.py          # ECSProvider class
│       └── templates/           # .tf.j2 Jinja templates
│
├── terraform/                   # Terraform subprocess + HCL tooling
│   ├── runner.py                # TerraformRunner + RecordingTerraformRunner
│   ├── emitter.py               # Jinja2 → .tf files (deterministic)
│   └── backend.py               # backend "..." {…} block emitter
│
├── image/                       # Provider-agnostic container build/push
│   ├── builder.py               # ImageBuilder (docker build)
│   └── pusher.py                # ImagePusher (docker push with auth hook)
│
└── services/                    # Legacy imperative deploy pipeline
    │                            # Retained on main; being retired provider-by-provider
    │                            # (see rc-e5u.10 for deprecation plan)
    └── ...
```

### Import rule

- `remote_compose.provider.base` and core — **zero** cloud SDK deps
- `remote_compose.provider.ecs.*` — imports `boto3` (optional extra `[ecs]`)
- `remote_compose.provider.k8s.*` (future) — imports `kubernetes` (`[k8s]`)
- FakeProvider + contract tests run with neither installed

## The Provider interface

Every provider implements the same 9 methods:

| Method | Purpose |
|---|---|
| `emit_terraform(ctx, out_dir)` | Write a self-contained terraform module. No apply. |
| `plan(ctx)` | Emit + `terraform plan`. Returns `PlanResult`. |
| `deploy(ctx)` | Emit + init + apply. Returns `DeployResult` with revision_id. |
| `redeploy(ctx, services=None)` | Force new task-def revision without config change. |
| `status(ctx)` | Live per-service state (desired/running/health). |
| `logs(ctx, service, follow, tail)` | Stream container logs. |
| `exec(ctx, service, command)` | Run a command in a live container. |
| `rollback(ctx, to_revision=None)` | Revert to prior deployed state. |
| `destroy(ctx)` | Remove everything `deploy` created. |

Contract semantics are enforced by `tests/contract/test_provider_contract.py`,
which parameterizes every test over every registered provider. FakeProvider
passes the suite offline; real providers pass the same suite plus network-only
tests (public ingress, persistent volumes, cross-service DNS) gated on backing
infrastructure (LocalStack / kind / real cloud).

## rc.yml v2 at a glance

```yaml
version: 2
project: myapp
compose_file: docker-compose.yml
provider: ecs

provider_config:
  ecs:
    region: us-west-2
    cluster: myapp-prod
    default_launch_type: FARGATE       # EC2 opt-in per service
    ec2_capacity:                      # used when any svc has launch_type: EC2
      instance_type: null              # null → auto-size from summed CPU/mem
      capacity_type: ON_DEMAND         # ON_DEMAND | SPOT | MIXED

terraform:
  output_dir: ./terraform/${provider}
  backend:
    type: s3
    bucket: myapp-tf-state
    key: myapp/ecs.tfstate
    region: us-west-2
    dynamodb_table: tf-locks

services:
  django:
    cpu: 1024
    memory: 4096
    replicas: 2
    type: application
    launch_type: FARGATE
    health_check_path: /health
  nginx:
    cpu: 256
    memory: 512
    type: proxy
    public: true
    port: 80

secrets:
  - { name: django, source: file, path: .envs/.production/.django }
  - { name: db,     source: aws_sm, arn: "arn:aws:secretsmanager:..." }
```

v1 configs upgrade cleanly via `rc migrate --in rc.yml --out rc.v2.yml`.
Unknown keys become warnings; unmigratable keys require `--force`.

## A deploy, end-to-end

```
  rc deploy
      │
      ▼
  load rc.yml v2  ─► DeployContext (project, compose, provider_config, tf_backend, services, secrets)
      │
      ▼
  Provider.deploy(ctx)
      ├── emit_terraform(ctx, out_dir)     # Jinja2 render from templates/
      ├── TerraformRunner.init()           # subprocess: terraform init
      ├── ImageBuilder.build(...)          # docker build (per service w/ build:)
      ├── ImagePusher.push(...)            # docker push to ECR/GCR/…
      ├── TerraformRunner.apply()          # subprocess: terraform apply
      └── TerraformRunner.output()         # collect ALB DNS, ECR URLs, etc.
      │
      ▼
  DeployResult { revision_id, services, duration_s, terraform_outputs }
```

The terraform module is **persistent and user-owned**: it sits at
`./terraform/<provider>/` by default. A user who wants to stop using `rc`
for infra changes can commit that directory and run `terraform apply`
directly thereafter. `rc logs`, `rc exec`, `rc redeploy` still work because
they read from terraform outputs + AWS APIs.

## Testing pyramid

| Tier | Marker | Runs against | Purpose |
|---|---|---|---|
| 1 Unit | `unit` | — | Pure logic: compose parse, rc.yml v2 validate, v1→v2 migrate, HCL rendering, backend block emission, service classes with mocks |
| 2 Contract | `contract` | FakeProvider + any registered real provider | Provider ABC semantics (idempotency, reconcile, destroy cleanup, HCL-emit determinism, secret leakage) |
| 3 Integration | `integration` | real terraform + LocalStack / kind | Provider-specific apply paths against simulated cloud infra |
| 4 E2E | `e2e` | real cloud (opt-in) | Smoke deploy to a live AWS/k8s cluster |

If `terraform` is missing or too old, the integration tests skip cleanly via
a sentinel that checks `terraform -version` succeeds. Run `rc doctor` to
diagnose and `rc doctor --fix` (or `rc install`) to install/upgrade.

## Validation ladder (provider verification)

Each rung catches a different class of bug. Run the earlier rungs in every
PR; run the later rungs in CI / before release.

| # | Command | Scope | Catches |
|---|---------|-------|---------|
| 1 | `pytest tests/unit/` | Unit | Template logic, schema validation, autosize math, each provider method called correctly |
| 2 | `pytest tests/unit/test_provider_ecs/test_golden.py` | Golden regression | Unintended template drift — byte-compares canonical emit against committed fixture |
| 3 | `pytest tests/unit/test_provider_ecs/test_hcl_structure.py` | HCL structural | Parse-level validity, resource inventory, referential integrity (`aws_x.y` points at defined resource) |
| 4 | `pytest tests/contract/` | Contract (FakeProvider) | Provider ABC semantics — idempotency, reconcile, destroy-cleanup, HCL-emit determinism, secret non-leakage |
| 5 | `RC_CONTRACT_PROVIDERS=ecs pytest tests/contract/` | Contract (real provider, emit-path) | Real provider passes the emit-only slice of the ABC |
| 6 | `pytest tests/integration/test_ecs_moto.py` | Moto integration | Real boto3 response shapes + API constraints (moto caught `logStreamNamePrefix`+`orderBy` bug in this repo) |
| 7 | `pytest tests/integration/test_provider_ecs_terraform.py` | Real terraform validate | HCL schema against the AWS provider plugin |
| 8 | `pytest tests/integration/ -k localstack` (future) | LocalStack apply | Cross-resource dependency satisfaction under a simulated cloud |
| 9 | `pytest tests/ -m e2e` (future) | Real cloud E2E | IAM/ACM/quota/ENI-scale issues only prod surfaces |

Regenerating the golden fixture after an intentional template change:
```bash
python -m tests.unit.test_provider_ecs.test_golden --regenerate
```

Rungs 1-6 run in the default CI (no terraform binary, no AWS creds).
Rung 7 requires `terraform >= 1.5`. Rung 8+ require Docker + LocalStack.
Rung 9 requires a sandbox AWS account.

## E2E test isolation (rung 9 setup)

E2E tests deploy, mutate, and destroy real AWS infrastructure. They are
pinned to `us-east-1` and scoped by tags so they cannot touch anything in
your prod regions.

**Invariants the system enforces:**

1. Every resource emitted by the ECS provider for a project named
   `rc-test-<anything>` carries two tags in addition to the normal set:
   `Project=rc-test-<anything>` and `Environment=rc-test`.
2. IAM roles, instance profiles, and ECR repos created for these projects
   are name-prefixed `rc-test-` (IAM is global — no tag scoping at create).
3. The reap script (`scripts/reap_test_region.py`) finds every such resource
   in `us-east-1` and deletes them in dependency-safe order, independent of
   terraform state.

**Runbook:**

```bash
# 1. Set up AWS creds for the test account (same account, scoped role)
export AWS_PROFILE=rc-sandbox           # or whatever the user's debuggai-like profile is

# 2. Sanity check us-east-1 is empty of rc-test-*
scripts/reap_test_region.py --dry-run

# 3. Run the E2E suite (opt-in)
RC_E2E=1 pytest -m e2e tests/e2e/

# 4. Post-run: force-reap anything the tests leaked
scripts/reap_test_region.py

# 5. Emergency kill — if tests wedge with resources live:
scripts/reap_test_region.py --dry-run          # inspect
scripts/reap_test_region.py                    # execute
```

**Recommended IAM scoping:** attach `docs/sandbox_iam_policy.json` to the
role assumed by the test credentials. It region-locks mutations to
`us-east-1`, constrains IAM resource names to `rc-test-*`, and prevents
accidental blast into other regions.

**Cost bound:** a full 3-service Fargate stack + ALB runs <$0.10/hr. Tests
should provision, verify, and destroy within ~10 minutes; the reap cron
(optional) sweeps any leaks nightly.

## Adding a new provider

1. Create `remote_compose/provider/<name>/__init__.py` and `provider.py`.
2. Subclass `Provider`; set `name = "<name>"`.
3. Drop `.tf.j2` templates in `provider/<name>/templates/`.
4. Use the shared `TerraformEmitter`, `TerraformRunner`, `ImageBuilder`,
   `ImagePusher` — no new runtime primitives needed.
5. Implement all 9 ABC methods. Contract tests will run automatically.
6. Call `register("<name>", <Class>)` in your package `__init__.py` once the
   contract suite passes against a real backing cluster (LocalStack, kind,
   or a sandbox cloud account).
7. Register optional extras in `pyproject.toml`: `[project.optional-dependencies] <name> = [...]`.

## Current state

- **Closed & tested**: Provider ABC, FakeProvider, rc.yml v2 + migrate CLI,
  terraform/image modules, ECSProvider (emit_terraform + lifecycle methods).
  **366 unit tests pass, 9 skip (real-terraform/AWS tiers).**
- **Open** (tracked in bd): EC2 launch type, EFS, secrets integration, custom
  domain + ACM/Route 53, contract-suite enrollment, Kubernetes provider,
  legacy-service deprecation.
- **Legacy**: the imperative 15-step pipeline in `remote_compose/services/`
  and `remote_compose/management/commands/` remains functional on `main`.
  It is deprecated in-place and retired provider-by-provider per rc-e5u.10.

See `bd list --status=open` for the authoritative in-flight work.
