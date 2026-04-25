# remote-compose

**Deploy any `docker-compose.yml` to a real cloud stack with one command.**

```
  docker-compose.yml  ──▶  Provider  ──▶  terraform HCL  ──▶  terraform apply  ──▶  running stack
         +                (ecs / k8s / …)      │                      │
   rc.yml (v2)                                 │                      └─▶ backend state (s3 / gcs / local)
         +                                     │
   ImageBuilder                                └─▶ self-contained module — you can `cd terraform/ && terraform apply`
   ImagePusher                                     without rc, no lock-in
```

`rc` is a generator + convenience wrapper around terraform. It reads your
existing `docker-compose.yml`, applies tuning from a small `rc.yml`, builds
images, pushes to the registry, emits a complete terraform module, and runs
it. Then it gives you everyday verbs (`rc deploy`, `rc lifecycle migrate`,
`rc db push`, `rc destroy`) so you don't have to invent a playbook per
project.

The active branch is **`portable-deploy`**. The legacy v1 (Django-app +
SSH) is preserved below the portable section for users on `main`.

> **Status: alpha, hand-tested against a real production-grade Django stack
> (sentinal: postgres, redis, django, celery worker, celery beat, nginx
> fronting two subdomains, full data restore from a 569 MB local dump).**
> See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and the validation
> ladder. See [AGENTS.md](AGENTS.md) for how the project is developed.

---

## Why this exists

Most deploy tooling forces you to choose:

- **Cloud-specific knobs** (ECS task defs, Kubernetes manifests, Helm charts) — you write the same app config in three places.
- **Magic black-box PaaS** (Heroku, Fly, Render) — opinionated, locked-in, hard to escape.
- **Hand-rolled terraform** — flexible but a 500-line module per service.

`remote-compose` takes a different bet: **your `docker-compose.yml` already
describes the topology you want**. The deployer should consume that file
verbatim, ask only for the few things compose can't express (CPU/mem,
secrets, public hostname, EFS uid), and produce a clean terraform module
you fully own.

---

## Quick start

```bash
# 1. Install
pip install -e ".[ecs]"            # or [k8s], or [all]

# 2. In your app repo (alongside docker-compose.yml)
rc init                            # scaffold a v2 rc.yml
$EDITOR rc.yml                     # set project, provider, domain

# 3. Configure cloud creds (ECS example)
export AWS_PROFILE=myprofile

# 4. Preview → deploy → operate
rc plan                            # show what terraform would create
rc deploy                          # build images, terraform apply, force-rolls
rc secrets push                    # upload .env files into AWS Secrets Manager
rc lifecycle migrate               # run a named hook in a live container
rc status                          # ECS service health table
rc exec django -- /bin/bash        # interactive shell
rc db push /tmp/local-dump.dump    # seed the deployed db from a local dump
rc destroy --yes                   # tear it all down
```

Every command is **declarative + idempotent**. Re-running `rc deploy` after
no changes prints `no changes — infrastructure matches config`.

---

## What rc.yml v2 looks like

```yaml
version: 2
project: my-app
compose_file: docker-compose.yml
provider: ecs

provider_config:
  ecs:
    cluster: my-app-prod
    region: us-west-1
    aws_profile: myprofile
    vpc_cidr: 10.0.0.0/16
    route53_zone: rctest.example.com   # override if zone != domain[-2:]

terraform:
  output_dir: ./terraform/${provider}
  backend:
    type: s3                           # or local
    bucket: my-app-tf-state
    key: ecs.tfstate
    region: us-west-1

# Auto-creates the backup S3 bucket via terraform with versioning + AES256
# + 14-day expiration. Set bucket_managed: false to point at an existing
# bucket you own elsewhere.
backup:
  bucket: my-app-backups
  service: postgres                    # which container hosts pg_restore
  retention_days: 14                   # or "never"

# .env files become Secrets Manager JSON blobs; provider emits one
# task-def `secrets[]` entry per KEY using arn:KEY:: selectors so each
# key arrives as its own env var (vs one giant blob).
secrets:
  - name: django
    source: file
    path: .envs/.production/.django
  - name: postgres
    source: file
    path: .envs/.production/.postgres

# Compose-driven deploy set. Default: every compose service deploys with
# sensible defaults; rc.yml services[] is for overrides. Use exclude/include
# for dev-only services (ngrok, debug profiles, etc.).
compose:
  exclude: [ngrok]                     # mutually exclusive with include

services:
  postgres:
    type: infrastructure
    cpu: 512
    memory: 1024
    volumes:
      - name: pgdata
        mount: /var/lib/postgresql/data
        # Per-service posix_user on the EFS access point so initdb
        # can chown — postgres:17 alpine = uid 70, debian = 999.
        uid: 999
        gid: 999
        mode: "0700"

  django:
    type: application
    cpu: 1024
    memory: 2048
    port: 8001
    health_check_path: /api/v1/health/
    ephemeral_storage: 21              # GiB; FARGATE 21–200
    lifecycle:
      migrate:
        command: ["python", "manage.py", "migrate", "--noinput"]
        auto_on_deploy: true           # runs after every rc deploy
      createsuperuser:
        command: ["python", "manage.py", "createsuperuser", "--noinput"]
        run_once: true                 # skips when probe exits 0
        probe:
          - python
          - -c
          - |
            import os, django, sys
            django.setup()
            from django.contrib.auth import get_user_model
            sys.exit(0 if get_user_model().objects.filter(
                email=os.environ['DJANGO_SUPERUSER_EMAIL']
            ).exists() else 1)
      shell:
        command: ["python", "manage.py", "shell"]
        interactive: true              # forwards a TTY

  nginx:
    type: proxy
    cpu: 256
    memory: 512
    port: 80
    public: true
    default_target: true               # catches anything the host rules don't
    domain: app.example.com            # primary; this name routes here
    aliases:                           # extra hostnames same service answers for
      - api.app.example.com            #   (cert SANs + R53 records, no listener rules)
    health_check_path: /health
```

Full schema reference: [ARCHITECTURE.md § rc.yml v2 at a glance](ARCHITECTURE.md#rcyml-v2-at-a-glance).

---

## Feature index

What's built and live-verified on the `portable-deploy` branch:

### Provider abstraction

- **`Provider` ABC** — every cloud target (ECS today, K8s next) implements `emit_terraform`, `plan`, `deploy`, `redeploy`, `status`, `logs`, `exec`, `rollback`, `destroy` against a shared `DeployContext`.
- **`FakeProvider`** for tests — every contract test runs against both `ECSProvider` and `FakeProvider` so adding a new provider is a copy-paste exercise.
- **rc-test-* tag** — every project named `rc-test-*` gets `Environment=rc-test` tags + `force_destroy=true` on destructive resources, so test stacks always tear down clean.

### ECS provider — what terraform we generate

- VPC + 2 public + 2 private subnets, IGW, security groups, default routing
- ECS cluster with Container Insights (log group terraform-managed)
- Per-service: ECR repo, task def, ECS service, Cloud Map service-discovery entry
- ALB with HTTP→HTTPS redirect (when `domain` is set) + ACM cert + R53 alias records
- EFS file system + access point per stateful volume; per-service posix uid/gid/mode
- AWS Secrets Manager: one secret per `.env` file, JSON-blobbed, individual keys exposed via ECS `arn:KEY::` selectors
- ECS Exec wired (task role gets `ssmmessages:*`); `enable_execute_command = true` on every service
- ALB host-routing: per-service `domain` → ALB listener rule + per-service target group
- Single fronting service: `aliases:` adds cert SANs + R53 records without listener rules
- S3 backup bucket auto-created with versioning + lifecycle when `backup.bucket` is declared
- Stateful services (any with EFS) auto-set `deployment_minimum_healthy_percent = 0` so rolling deploys can't race-corrupt postgres data

### CLI

| command | does |
|---|---|
| `rc init` | scaffold a v2 rc.yml |
| `rc migrate --in rc.yml --out rc.v2.yml` | convert legacy v1 |
| `rc plan` | terraform plan summary |
| `rc deploy [--no-build]` | build, push, terraform apply, force-roll, run auto_on_deploy hooks |
| `rc destroy --yes` | terraform destroy |
| `rc status` | ECS service health table |
| `rc exec <service> -- <cmd...>` | run a one-off command inside a live task; reliable stdout via sentinels; full TTY when stdin is a tty |
| `rc lifecycle <hook> [<service>]` | run a named hook from rc.yml (resolves declarer; handles `run_once` probes) |
| `rc secrets push [--rollout/--no-rollout]` | parse each `.env` file → upload as JSON to its SM secret → force-rolls every service |
| `rc db backup` / `rc db restore` / `rc db list` | postgres backup round-trips through S3 (host-side presigned URLs; tasks just curl) |
| `rc db push <file>` | upload a local dump → exec `pg_restore` inside the deployed container; auto-detects format from extension (`.dump`, `.tar.gz`, `.sql`); bootstraps `curl + ca-certificates` in containers that don't ship them |
| `rc doctor` | preflight: terraform/docker/python/boto/AWS creds checked |
| `rc install` | platform package-manager fix for missing deps |

### Compose feature support

- `build:` with optional `target:` (multi-stage), `args:`, `dockerfile:` — relative dockerfile resolved against the build context (the natural compose semantic)
- `image:` — pre-built image used verbatim, ECR push skipped
- `command:` — overrides the container CMD
- `environment:` (dict or list) AND `env_file:` (list or single string, paths relative to compose dir, multiple files merge in declaration order, `environment:` map wins on conflict)
- `ports:` — when public, primary port goes to ALB target group; remaining ports become additional `containerPort`s in the task def, intra-VPC reachable via the existing tasks SG (use this for VNC, devtools, internal-only ports)
- `volumes:` — EFS-backed when declared in rc.yml with explicit `mount:` and uid/gid

### Lifecycle commands

Declarative one-off operations live in rc.yml as `services[*].lifecycle.<hook>`:

```yaml
lifecycle:
  migrate:
    command: ["python", "manage.py", "migrate", "--noinput"]
    auto_on_deploy: true        # rc deploy runs this after rollout
  createsuperuser:
    command: ["python", "manage.py", "createsuperuser", "--noinput"]
    run_once: true
    probe: [python, -c, "import sys; sys.exit(0 if user_exists() else 1)"]
  shell:
    command: ["python", "manage.py", "shell"]
    interactive: true           # TTY passthrough
```

`auto_on_deploy: true` runs the hook after every successful `rc deploy`,
in declaration order, with hook failures surfaced as warnings (not deploy
failures — rerun `rc lifecycle <hook>` for full output).

`run_once: true` runs the `probe:` first; non-zero exit ⇒ "not yet
done" ⇒ run the hook. Idempotent createsuperuser, fixture loading,
schema bootstrap.

### Local-data seeding (`rc db push`)

Spin up a test stack in a separate region, seed it with real data from a
local Docker volume, validate, tear down. Repeat. The flow:

```bash
docker exec my_postgres pg_dump -Fc -U postgres my_db > /tmp/seed.dump
rc deploy
rc secrets push
rc db push /tmp/seed.dump
```

`rc db push` uploads to the configured backup bucket via host-side boto3,
generates a presigned GET URL, exec's a sentinel-bracketed restore script
inside the deployed postgres container that downloads with curl (or
bootstraps curl via apt-get when the image doesn't ship it), runs
`pg_restore --no-owner --clean --if-exists`, and deletes the S3 staging
object on success.

---

## Mental model in 5 lines

1. **Compose is the topology.** Adding a service to `docker-compose.yml` deploys it (defaults: 256 CPU / 512 MB / `application` if it has ports, `worker` otherwise).
2. **rc.yml is the tuning.** Override CPU, memory, port, public, domain, lifecycle, secrets, volumes, EFS uid, etc. per-service.
3. **The provider is thin.** It generates a terraform module from the merged config; you can `cd terraform/ && terraform apply` without `rc` ever again.
4. **Secrets are JSON in SM.** Each `.env` file becomes one secret; each KEY in that file becomes a separate task-def env var via ECS JSON-key selectors.
5. **Test stacks are disposable.** `rc-test-*` projects auto-set `force_destroy=true` on every resource; `rc destroy` tears them down clean.

---

## Codebase map

```
remote_compose/
├── cli.py                           # legacy v1 commands + v2 dispatch
├── cli_v2.py                        # v2 CLI: load_rc_yml, build_deploy_context, dispatch_if_v2
├── config/
│   ├── v1_schema.py                 # legacy flat schema loader
│   ├── v2_schema.py                 # ServiceV2, RcConfigV2, ComposeConfig, BackupConfig, ...
│   └── migrate.py                   # v1 → v2 with warnings on stateful services
├── envfile.py                       # standalone .env parser (used by provider + rc db push + lifecycle)
├── provider/
│   ├── base.py                      # Provider ABC, ServiceSpec, DeployContext, ExecResult, ...
│   ├── fake.py                      # in-memory provider for the contract suite
│   └── ecs/
│       ├── provider.py              # ECSProvider implementation
│       ├── autosize.py              # EC2 capacity provider sizing
│       ├── ecr_auth.py              # ECR login for image push
│       └── templates/
│           ├── alb.tf.j2            # ALB + listeners + per-service target groups + host rules
│           ├── backend.tf.j2        # terraform backend
│           ├── backup.tf.j2         # S3 backup bucket + lifecycle
│           ├── capacity.tf.j2       # EC2 capacity provider
│           ├── cluster.tf.j2        # ECS cluster + container-insights log group
│           ├── domain.tf.j2         # ACM cert (with SANs) + R53 records
│           ├── efs.tf.j2            # EFS + access points (uid/gid/mode)
│           ├── iam.tf.j2            # task-execution + task roles + ssmmessages policy
│           ├── network.tf.j2        # VPC, subnets, IGW, route tables
│           ├── outputs.tf.j2        # ECR repo URLs, ALB DNS
│           ├── providers.tf.j2      # AWS provider block
│           ├── secrets.tf.j2        # SM secret placeholders
│           ├── security_groups.tf.j2
│           ├── service_discovery.tf.j2  # Cloud Map private namespace
│           ├── services.tf.j2       # ECS task def + service per compose service
│           └── variables.tf.j2
├── image/
│   ├── builder.py                   # docker build wrapper (handles relative dockerfile)
│   └── pusher.py                    # docker push to ECR/GCR/etc.
└── terraform/
    ├── backend.py                   # render_backend_block
    ├── emitter.py                   # Jinja2-based directory render
    └── runner.py                    # subprocess wrapper for terraform CLI

tests/
├── unit/                            # per-module unit tests
├── contract/test_provider_contract.py   # runs against ECSProvider + FakeProvider
├── integration/test_provider_ecs_terraform.py  # invokes real `terraform validate`
├── e2e/                             # opt-in real-AWS tests (RC_E2E=1)
└── fixtures/golden/ecs_minimal/     # byte-for-byte expected HCL output

examples/
├── demo-app/                        # FastAPI + worker + postgres + redis reference stack
└── sample-app/                      # minimal hello-world
```

[ARCHITECTURE.md § Layers](ARCHITECTURE.md#layers) has the import-rule
diagram.

---

## Build & test

```bash
# Dev install
pip install -e ".[ecs]"
pip install -r requirements/dev.txt

# Fast (12s): unit + contract
pytest tests/unit/ tests/contract/

# Adds: real `terraform init -backend=false && terraform validate`
pytest tests/integration/

# Full opt-in real-AWS suite (~25 min, requires creds)
RC_E2E=1 pytest -m e2e tests/e2e/

# Regenerate the byte-identical golden HCL fixture
python -m tests.unit.test_provider_ecs.test_golden --regenerate

# Linters
black remote_compose/
flake8 remote_compose/
```

The contract suite is the heart of provider parity. Any new provider
ships only when `pytest tests/contract/test_provider_contract.py` is
green against it.

---

## Roadmap / open work

Tracked in [beads](https://github.com/steveyegge/beads). To inspect:

```bash
bd ready                      # available work
bd show rc-e5u                # the umbrella epic
bd list --status=open         # everything still open
```

High-signal open items (as of this writing):

- **Kubernetes provider** (`rc-e5u.8`) — proves the multi-cloud claim
- **Private subnets + NAT** (`rc-e5u.25`) — currently public-subnet Fargate for cost
- **EFS encryption on fresh accounts** (`rc-e5u.26`) — KMS key bootstrap
- **`rc audit`** (`rc-e5u.37.4`) — post-destroy AWS-side cleanup verification
- **`rc db dump-local`** (`rc-e5u.37.3`) — wraps `docker exec pg_dump` with port autodiscovery
- **`rc compose import`** (`rc-e5u.41.3`) — scaffold rc.yml from a compose file
- **Framework presets** (`rc-e5u.35.7`) — auto-default lifecycle hooks for django/rails/phoenix/laravel
- **Provider auto-import of orphan log groups** (`rc-e5u.37.5`) — terraform import on first-run conflicts

---

## Design principles

1. **Be a generator, not a runtime.** Every piece of state we own should also be readable as plain terraform / plain JSON. Users escape `rc` cleanly.
2. **Compose is the contract.** Don't invent parallel config; consume the file the team already maintains.
3. **Test against real clouds.** Unit tests catch shape regressions; the real validator is `terraform validate` + a live e2e against `rc-test-*` projects.
4. **One-off operations get first-class commands.** Lifecycle hooks, db push, exec, secrets push — all CLI verbs, not bash scripts users have to copy.
5. **Reproducible test stacks.** `rc-test-*` namespaces auto-tear-down; isolation is a property of the project name, not user discipline.
6. **No backwards-compat ratchets in alpha.** When the right shape conflicts with the old shape, file a bead, change both at once. Backward-compat shims live only as long as we're sure they don't trap us.

See [AGENTS.md](AGENTS.md) for the day-to-day workflow.

---

## Related docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — full design, validation ladder, dependency graph, e2e setup
- [AGENTS.md](AGENTS.md) — workflow conventions for humans + AI agents
- [examples/demo-app/README.md](examples/demo-app/README.md) — runnable reference stack
- [CLAUDE.md](CLAUDE.md) — instructions for Claude Code when working in this repo

---

# Legacy v1 (pre-portable)

The content below describes the v1 SSH/Django-app deploy path on `main`.
The portable provider work above lives on `portable-deploy`. v1 still ships
for users on the older path; v2 is the active line.

## Features (v1)

- **Docker Context Management**: Create and manage Docker contexts for remote deployment targets
- **Docker Compose Deployment**: Deploy docker-compose.yml files to remote hosts via SSH
- **AWS EC2 Integration**: Auto-discover EC2 instances and create deployment targets
- **AWS ECS Integration**: Deploy to AWS ECS (Fargate or EC2) without SSH
- **Async Deployments**: Celery tasks for background deployment operations
- **Health Monitoring**: Continuous health checks for targets and deployments
- **Multi-Service Orchestration**: Deploy multiple services with sequential, parallel, rolling, or canary strategies
- **Rate Limiting**: Protect against deployment abuse with configurable rate limits
- **Audit Logging**: Track all deployment-related actions for compliance
- **Secure Credential Storage**: Fernet-encrypted storage for SSH keys and AWS credentials
- **Log Sanitization**: Automatic masking of sensitive data in logs
- **Webhooks & Notifications**: Slack, email, and custom webhook notifications
- **Deployment History**: Full deployment tracking with rollback capability

For the full v1 reference (Django settings, management commands, API
viewsets, etc.) see the file history of this README in `git log` —
the prior version is preserved at `git show main:README.md`.

## License

MIT — see [LICENSE](LICENSE) for terms.
