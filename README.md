<img width="1672" height="941" alt="ChatGPT Image May 8, 2026, 11_07_11 AM" src="https://github.com/user-attachments/assets/f4920e46-ee34-4824-a2d3-7debf3292898" />


# remote-compose

**Take any `docker-compose.yml` and put it on AWS — as a fresh dev box with Claude already running inside, or as production infrastructure.**

Two flavors of one tool:

### 🚀 `rc dev up` — disposable cloud dev environments with an agent inside

```bash
rc dev up alice \
  --repo https://github.com/owner/myapp \
  --compose docker-compose.yml \
  --gh-token "$(gh auth token)" --skip-permissions
```

In ~5 minutes you get a fresh EC2 box with: docker, your repo cloned, the compose stack running, and **Claude Code pre-authenticated in a tmux session** waiting for you to attach. `rc dev attach alice` drops you into it. One box per agent or per branch. Work in parallel on isolated infra. `rc dev destroy` when done.

Multi-repo deploys, env-file shipping, `gh`/`bd` pre-installed, shared SSH key + auth lifecycle — all built in.

### 🏗️ `rc deploy` — generate a real terraform module from your compose

```bash
rc up --from-compose docker-compose.yml
```

Reads your compose, asks for the handful of things compose can't express (CPU/memory, secrets, public hostname, EFS uid), generates a clean ECS terraform module, applies it. Then everyday verbs: `rc deploy`, `rc lifecycle migrate`, `rc db push`, `rc destroy`. The module is yours — `cd terraform/ && terraform apply` works without `rc`.

> **Status: alpha. Hand-tested against a real production Django stack.** Active branch: **`portable-deploy`**. Legacy v1 (SSH/Django-app) lives below for users on `main`. See [ARCHITECTURE.md](ARCHITECTURE.md) for design + validation, [AGENTS.md](AGENTS.md) for workflow.

---

## Why both modes share one tool

Same insight, two surfaces. Compose is already the spec for "how my services fit together." For dev we ship that spec to an EC2 box and start an agent inside. For production we render it to ECS terraform. You write the compose file once.

Most deploy tooling makes you choose between cloud-specific knobs (ECS task defs, k8s manifests, Helm), opinionated black-box PaaS (Heroku, Fly, Render), or hand-rolled terraform (flexible but a 500-line module per service). `remote-compose` takes the other bet: your compose file is the topology. The tool's job is to add the few things compose can't express and emit something clean — terraform you own, or an EC2 box that disappears when you're done with it.

---

## Quick start

> **Bootstrapping a fresh machine?** Run
> `bash scripts/bootstrap-from-zero.sh` instead of step 1 — it installs
> terraform via the platform package manager (brew/apt/dnf), creates a
> `.venv`, runs `pip install -e ".[ecs]"`, and verifies `rc doctor` is
> all-green. Idempotent — safe to re-run.

```bash
# 1. Install (only ECS provider ships today — k8s is roadmap)
pip install -e ".[ecs]"

# 2. In your app repo (alongside docker-compose.yml)
rc init --from-compose docker-compose.yml   # scaffold a v2 rc.yml from your compose
$EDITOR rc.yml                              # tweak cpu/memory/health checks

# 3. Configure cloud creds (ECS example)
export AWS_PROFILE=myprofile

# 4. One-shot: scaffold (if missing) → deploy → push secrets → ALB URL
rc up --from-compose docker-compose.yml     # the lazy path, idempotent

# 4b. Or step through it manually
rc plan                            # show what terraform would create
rc deploy                          # build images, terraform apply, force-rolls
rc secrets push                    # upload .env files into AWS Secrets Manager
rc lifecycle migrate               # run a named hook in a live container
rc status                          # ECS service health table
rc exec django -- /bin/bash        # interactive shell
rc db push /tmp/local-dump.dump    # seed the deployed db from a local dump
rc destroy --yes                   # tear it all down
```

To verify the documented commands exist as advertised, run
`bash scripts/test-readme-quickstart.sh` — it audits `rc --help` against
this section without touching AWS.

Every command is **declarative + idempotent**. Re-running `rc deploy` after
no changes prints `no changes — infrastructure matches config`.

---

## First-deploy walkthrough

If you want to verify rc actually works end-to-end against real AWS
before you commit to it, the repo ships a scripted acceptance trace
that takes a clean account → a fully-running production-shape Django +
celery + nginx stack → clean teardown. Single command, no aws-cli, no
sed, no `/tmp` dance.

```bash
# Prereqs: terraform installed (or run scripts/bootstrap-from-zero.sh
# first), an AWS profile with creds, and a Django+celery compose to
# point at. We use start-simpli (private repo) — substitute your own
# via the START_SIMPLI / COMPOSE_FILE / REGION / AWS_PROFILE_OVERRIDE
# env vars at the top of the script.

bash scripts/test-startsimpli-end-to-end.sh
```

What it does, step by step:

1. **`rc destroy --all-ephemeral`** — clean slate. Removes any prior
   ephemeral stacks from the local registry.
2. **`rc up --from-compose docker-compose.local.yml --aws-profile X
   --region Y --ttl 4h`** — single-command full deploy. Scaffolds an
   rc.yml from your compose, auto-fixes nginx for ECS Cloud Map
   (variable-based proxy_pass + VPC resolver), imports any orphan
   Container Insights log group, runs terraform apply, builds + pushes
   images, force-rolls services, pushes file-sourced secrets into
   Secrets Manager, runs auto_on_deploy lifecycle hooks (e.g.
   `python manage.py migrate --noinput`).
3. **`rc status` polling** — waits for all services to reach
   `health=healthy`.
4. **Plain `curl http://<ALB>/api/v1/health/`** — no Host: header
   rewrite, no `https`, no `--insecure`. The patient retry loop tolerates
   the ~60-90s window where ECS marks the task healthy but Django is
   still finishing migrations + collectstatic + runserver. When it
   returns 200, the body is checked for `{"celery":"healthy"}` —
   real workers responding to ping, not "no_workers".
5. **`rc destroy --yes`** — clean teardown. Removes every AWS resource
   tagged `Project=<this-stack>` and unregisters the entry from the
   ephemeral registry.
6. **`rc list --ephemeral`** — registry empty. No stale rows.

Scripted. Repeatable. The tracking bead is
[rc-e5u.46](.beads/issues.jsonl) (`bd show rc-e5u.46`).

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
    vpc_cidr: 10.0.0.0/16               # CIDR for the VPC rc creates (default mode)
    route53_zone: rctest.example.com   # override if zone != domain[-2:]

    # --- Deploy into an EXISTING VPC (optional) -------------------------------
    # By default rc creates its own VPC. Set vpc_id to deploy INTO an existing
    # one instead — use this when the stack must share a VPC + security group
    # with peer systems (so same-VPC SG references + Cloud Map DNS work, which
    # cross-VPC peering can't replicate). All four keys are opt-in; omit them
    # and behavior is unchanged.
    #   vpc_id: vpc-0abc123                       # adopt this VPC (vpc_cidr unused)
    #   public_subnet_ids: [subnet-a, subnet-b]   # >=2 AZs, PUBLIC (IGW route) —
    #                                             # ALB + Fargate (assign_public_ip)
    #   private_subnet_ids: [subnet-c, subnet-d]  # optional; defaults to public
    #   security_group_ids: [sg-mesh]             # extra SGs attached to every
    #                                             # task (join an existing mesh)
    # rc pre-flights the VPC + subnets against AWS before deploying. In adopt
    # mode rc creates NO VPC/IGW/subnets/route-tables and does NOT touch the
    # VPC's DHCP options, so cross-service discovery must use FQDNs
    # (<svc>.<project>.local) rather than short names.

    # --- App-IAM grants on the task role (optional) ---------------------------
    # rc emits ONE shared task role (aws_iam_role.task) for all services. By
    # default it can only open SSM exec channels. Use `iam` to grant it the
    # AWS access your app needs (S3 media, SQS, SES, ...) so you don't need an
    # out-of-band reconcile script. Omit `iam` and the emitted terraform is
    # byte-identical to before.
    #   iam:
    #     managed_policies:                 # attached as aws_iam_role_policy_attachment
    #       - arn:aws:iam::aws:policy/AmazonSESFullAccess
    #     statements:                       # one inline aws_iam_role_policy (task-app)
    #       - sid: S3Media                  # optional; auto-named AppGrant<N> if omitted
    #         actions: [s3:GetObject, s3:PutObject, s3:DeleteObject,
    #                   s3:ListBucket, s3:GetBucketLocation]
    #         resources: [arn:aws:s3:::my-bucket, arn:aws:s3:::my-bucket/*]
    #       - actions: [elasticfilesystem:ClientMount, elasticfilesystem:ClientWrite]
    #         resources: [arn:aws:elasticfilesystem:us-east-2:1234:file-system/fs-abc]
    #         condition:                    # optional IAM Condition block
    #           StringEquals:
    #             elasticfilesystem:AccessPointArn: arn:aws:...:access-point/fsap-abc

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

# CI/bootstrap IAM — the GitHub OIDC role CI assumes to trigger deploys. This is
# NOT a per-service runtime resource, so `rc bootstrap` emits it into a COMMITTED
# stack with its own terraform state (see "CI bootstrap" below). Strictly opt-in:
# omit the whole key and nothing changes. ${project}/${cluster} interpolate.
bootstrap:
  github_oidc_deploy_role:
    github_repo: my-org/my-app         # owner/repo (required)
    github_branch: main                # exact branch (StringEquals); "*" = any ref
    # role_name: my-ci-deploy          # default ${project}-github-deploy; set to
                                        #   match a live role for import -> no-op
    # create_oidc_provider: false       # default: adopt the account-global provider
    permissions:                       # each key -> a least-privilege IAM statement
      codebuild_project: ${project}-build
      ecr_namespace:     ${project}/*
      ecs_clusters:      [${cluster}, 'foundry-tenant-*']   # wildcard = StringLike
      pass_roles:        [${project}-task, ${project}-task-exec]

services:
  postgres:
    type: infrastructure
    cpu: 512
    memory: 1024
    # rc-7ga: exclude from the default `rc deploy` build+force-roll. A single-
    # task EFS service rolls with min_healthy=0, so rolling it on every app
    # deploy briefly drops its Cloud Map DNS record (dependents get [Errno -2]).
    # terraform still manages it; deploy deliberately with
    # `rc deploy --services postgres` when its image/config actually changes.
    auto_roll: false
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
    # Per-service env from an EXISTING Secrets Manager secret (rc-7yo). Each key
    # is wired as its own task-def secret (valueFrom <arn>:KEY::) on THIS
    # service only, and the arn is added to the task-exec GetSecretValue grant.
    # Keys are explicit (rc does not call AWS at emit time). Use this for a
    # pre-existing multi-key secret; use top-level `secrets:` when rc should
    # CREATE the secret from a file.
    env_from_secret:
      - arn: arn:aws:secretsmanager:us-east-2:123:secret:myapp/prod-env-django-AbC
        keys: [DATABASE_URL, REDIS_URL, DJANGO_SECRET_KEY]

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
| `rc bootstrap [--apply]` | emit + plan the committed GitHub-OIDC CI deploy-role stack from `bootstrap:` ([details](#ci-bootstrap--committed-deploy-role-stack)); `--apply` opt-in, never destroys |
| `rc deploy [--no-build]` | build, push, terraform apply, force-roll, run auto_on_deploy hooks |
| `rc destroy --yes` | terraform destroy |
| `rc status` | ECS service health table |
| `rc exec <service> -- <cmd...>` | run a one-off command inside a live task; reliable stdout via sentinels; full TTY when stdin is a tty |
| `rc lifecycle <hook> [<service>]` | run a named hook from rc.yml (resolves declarer; handles `run_once` probes) |
| `rc secrets push [--rollout/--no-rollout]` | parse each `.env` file → upload as JSON to its SM secret → force-rolls every service |
| `rc db backup` / `rc db restore` / `rc db list` | postgres backup round-trips through S3 (host-side presigned URLs; tasks just curl) |
| `rc db push <file>` | upload a local dump → exec `pg_restore` inside the deployed container; auto-detects format from extension (`.dump`, `.tar.gz`, `.sql`); bootstraps `curl + ca-certificates` in containers that don't ship them |
| `rc copilot import` | migrate an AWS Copilot app to rc.yml v2 + docker-compose; supports `--env <name>` for per-environment overrides ([guide](#aws-copilot-migration)) |
| `rc doctor` | preflight: terraform/docker/python/boto/AWS creds checked |
| `rc install` | platform package-manager fix for missing deps |

### CI bootstrap — committed deploy-role stack

The workload stack (`deploy/<project>/terraform/`) is regenerated every run and
usually gitignored. But the **CI deploy role** — the GitHub Actions OIDC role CI
assumes (via `sts:AssumeRoleWithWebIdentity`) to *trigger* deploys — is not a
per-service runtime resource. It must be tracked, and if it lives out-of-band via
the AWS CLI it drifts. `rc bootstrap` generates it from the rc.yml `bootstrap:`
section into a **separate, committed stack with its own terraform state** so it's
versioned alongside your code.

```bash
rc bootstrap            # emit bootstrap/terraform/ + terraform init + plan
rc bootstrap --apply    # also apply (opt-in; refuses to apply a plan that destroys)
```

- **Committed, separate state.** Emitted to `bootstrap/terraform/` (override with
  `bootstrap.output_dir`) — *outside* the regenerated workload tree, so you commit
  it. Its backend reuses the workload bucket/lock table but with a distinct key
  (`<project>/bootstrap.tfstate`). The stack's own `.gitignore` still excludes
  `*.tfstate` — state is never committed, only the `.tf`.
- **Permissions → least-privilege IAM.** Each `permissions` key derives one or more
  IAM statements with stable SIDs; region/account land as terraform data-source
  refs (`${data.aws_region.current.name}` / `…caller_identity…account_id`), so emit
  makes no AWS calls and stays deterministic:

  | key | grants |
  |---|---|
  | `codebuild_project: <name>` | `codebuild:StartBuild`/`BatchGetBuilds`/… on that project ARN |
  | `ecr_namespace: <ns>/*` | `ecr:GetAuthorizationToken` (Resource `*`) + push/pull on repos under the namespace |
  | `ecs_clusters: [a, 'b-*']` | `ecs:UpdateService`/`DescribeServices`/… scoped to each cluster's service+cluster ARNs (a wildcard entry like `foundry-tenant-*` carries straight into the ARN → StringLike-by-ARN) + `RegisterTaskDefinition`/… on `*` |
  | `pass_roles: [r1, r2]` | `iam:PassRole` on those role ARNs, conditioned to `ecs-tasks.amazonaws.com` |

- **OIDC provider: adopt by default.** The `token.actions.githubusercontent.com`
  provider is account-global (one per account) and CI already assumes it, so the
  stack references it via a data source. Set `create_oidc_provider: true` to have
  rc create it instead.
- **Adopting a live role (import → no-op).** Set `role_name` to the live role's name
  and import it before the first apply — the generated stack README spells out the
  exact `terraform import aws_iam_role.deploy …` / `aws_iam_role_policy.deploy …`
  commands. `rc bootstrap` should then plan a no-op (or a small, reviewable diff).

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

### AWS Copilot migration

AWS Copilot reaches **end-of-support on 2026-06-12**. Every team
running on Copilot needs a path off it. `rc copilot import` is that
path — it reads any `copilot/` directory tree (services, environments,
addons, pipelines) and writes a working `rc.yml` v2 + `docker-compose.yml`
+ `IMPORT_SUMMARY.md`.

```bash
rc copilot import \
    --from ./copilot \
    --out  . \
    --env  production \
    --project my-app
```

**What translates today:**

| Copilot construct | rc translation |
|---|---|
| `Backend Service` | private rc service (no public, no ALB) |
| `Worker Service` | rc service `type: worker` |
| `Load Balanced Web Service` | public rc service + port + `default_target` + domain (from `http.alias`) + aliases |
| `image.build: { context, dockerfile, target, args }` | docker-compose `build:` block (multi-stage `target` honored) |
| `image.location` | docker-compose `image:` (Copilot's `${TAG}` interpolation preserved) |
| `cpu`, `memory`, `count` | rc.yml `cpu`, `memory`, `replicas` |
| `storage.volumes.<n>: { path, efs: {uid, gid} }` | rc.yml `volumes` with EFS access-point uid/gid |
| `variables: { KEY: value }` | docker-compose `environment:` |
| `secrets: { KEY: { secretsmanager: arn } }` | rc.yml `secrets:` `source: aws_sm` |
| `environments.<env>` overrides | deep-merged when `--env <env>` passed |
| `${COPILOT_ENVIRONMENT_NAME}` | resolved when `--env` is passed; left literal otherwise |

**What gets flagged for review** (typed warnings grouped in `IMPORT_SUMMARY.md`):

| Copilot construct | warning |
|---|---|
| `Request-Driven Web Service` | `UnsupportedServiceTypeWarning` — App Runner is a different runtime; best-effort translated to public ECS for review |
| `Static Site` | `UnsupportedServiceTypeWarning` — CloudFront+S3 has no ECS analogue; emitted to `compose.exclude` so it's not silently dropped |
| `count: { range, cpu_percentage }` | `ScalingNotSupportedWarning` — autoscaling not yet emitted; replicas pinned to range floor |
| `count: 0` | `ScalingNotSupportedWarning` — ECS doesn't scale-to-zero; replicas=1 |
| `exec: false` | `ExecDisabledIgnoredWarning` — provider always enables ECS Exec |
| `network.vpc.placement: private` | `PrivateSubnetUnsupportedWarning` — public-subnet Fargate today (rc-e5u.25 tracks the NAT variant) |
| addons CFN templates | listed in summary — translate to terraform manually (P3 backlog) |

Tested against [a corpus of real Copilot apps](tests/fixtures/copilot/README.md) including:
- aws/copilot-cli e2e fixtures (canonical LBWS, app-with-domain, static-site)
- a public external example (ShanikaEdiriweera/aws-copilot-example)
- a 15-service production-grade app (sentinal: backend + workers + nginx + multi-env + secretsmanager refs)

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
├── copilot/
│   ├── discover.py                  # walk copilot/ → typed CopilotApp model
│   └── translate.py                 # 5 focused translators + composer + warning types
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

## Image builds — shared-image dedup

When several services share one build (same context + dockerfile + target +
build args — the standard Django layout where `django` and the `celery-*`
workers run the *same* image, differing only by `command`), rc builds and
pushes that image **once** to a single ECR repo and points the sibling task
definitions at it. Without this, an N-service app pushes the same image to N
repos — and because ECR stores layer blobs per-repo, that's N full uploads
(hours on a slow uplink). The repo owner is the alphabetically-first service in
the group; nothing to configure. Services with a unique build or a pre-built
`image:` are unaffected.

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
