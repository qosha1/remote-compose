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
   (variable-based proxy_pass + VPC resolver), runs terraform apply,
   builds + pushes
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

## `rc preflight` — every missing prerequisite at once

A stack's FIRST stateful apply is where rc is most dangerous: nothing about
the terraform path has ever been exercised, and the failure lands on
production. Moving one stack off `--no-state` cost three failed production
deploys in a row, each surfacing exactly one missing prerequisite — no
terraform binary, an S3 403 on the state object, an `aws_profile` that
doesn't resolve on an OIDC runner. Fixing those by hand then turned up 36
more missing IAM actions, every one of which would have been another serial
failure.

```
rc preflight            # table
rc preflight --json     # machine-readable
```

It renders the terraform first, then checks:

- **terraform binary** — present, and new enough.
- **state backend** — readable by *this* principal, and its recorded
  `terraform_version` vs the local binary. terraform refuses to operate on
  state written by a newer version, so pinning another repo's version without
  checking it against *this* state fails every deploy. A state object that
  doesn't exist yet is a first apply, not a failure.
- **state lock** — acquire *and* release, proven against the DynamoDB lock
  table. A lock somebody else holds is reported, never broken.
- **deploy principal IAM** — every action the module will call, simulated
  with `iam:SimulatePrincipalPolicy`, reported **all at once** grouped by
  service, with a paste-ready policy statement for whatever's missing.

### Which principal gets checked

The identity that matters is the role CI assumes — which is exactly the
identity a laptop run is *not*. Running as an admin user and getting "all
prerequisites satisfied" is true and irrelevant, so rc says which principal
it checked and whether that's the one that deploys:

```yaml
provider_config:
  ecs:
    deploy_role_arn: arn:aws:iam::123456789012:role/myapp-prod-github-deploy
```

With that set, `rc preflight` checks the deploy role by default (and running
as anyone else is the thing you have to ask for). It also makes that role a
**versioned fact** — these `*-github-deploy` roles are otherwise hand-made
bootstrap artifacts that exist nowhere in git. `rc preflight --principal
<arn>` overrides it per-run. Without either, a fully-passing report is
reported as a **warning**, not a pass.

### Resource-scoped policies are simulated against real ARNs

`SimulatePrincipalPolicy` defaults the resource to `*`, and a correctly
least-privileged policy fails that way: a role scoped to
`role/myapp-*` and its own state bucket returns `implicitDeny` for
`iam:CreateRole`, `iam:CreateInstanceProfile`, `s3:PutObject` and
`dynamodb:PutItem` against `*`, and `allowed` against the ARNs it will really
touch. Reporting those would push operators to widen statements to
`Resource: "*"` — worse than the gap it replaces.

So rc groups actions by resource class and simulates each against the
concrete ARNs its own templates produce (`${project}-task`,
`${project}-ec2-instance`, the configured state object and lock table). Note
`ResourceArns` applies to *every* action in a call, so wildcard-only actions
(`ecs:RegisterTaskDefinition`, `ecr:GetAuthorizationToken`) are deliberately
kept in their own unscoped call — and any denial from an unscoped group is
labelled as a possible false negative with the command to verify it, rather
than presented as fact.

The action set is derived from the `.tf` rc just rendered — not from a
terraform plan, which would need the state access preflight is checking, and
not from a fixed list, so the report is about *this* deploy. A stack with no
`domain:` is never told it needs route53.

Two deliberate limits. Resource types rc has no action mapping for are
reported as **unchecked** rather than passed silently. And the whole thing is
advisory: `iam:SimulatePrincipalPolicy` is itself a permission the caller may
lack (reported as "could not check", never as a pass), and it does not
evaluate SCPs or permission boundaries — a clean report is evidence, not
proof.

Preflight also runs automatically at the head of `rc plan` / `rc deploy` for
stacks with a remote (s3) backend, and blocks on failures. `RC_SKIP_PREFLIGHT=1`
opts out.

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
    aws_profile: myprofile              # LOCAL DEVELOPMENT ONLY — see below
    deploy_role_arn: arn:aws:iam::123456789012:role/my-app-github-deploy
                                        # the principal CI assumes; `rc preflight`
                                        # checks THIS rather than whoever is logged in
    vpc_cidr: 10.0.0.0/16               # CIDR for the VPC rc creates (default mode)
    route53_zone: rctest.example.com   # override if zone != domain[-2:]

    # --- aws_profile is a workstation concept --------------------------------
    # A named profile only exists where a shared AWS config file does. On a CI
    # runner credentials arrive as environment variables (GitHub OIDC, an
    # assumed role, container credentials) and no profile exists at all, so
    # rendering `profile = "..."` into the terraform provider fails the apply
    # with terraform's own opaque:
    #
    #     Error: failed to get shared config profile, default
    #
    # rc resolves this at preflight (rc-rigk) rather than letting terraform
    # discover it mid-apply:
    #   * profile resolves            -> rendered as configured.
    #   * absent, ambient creds set   -> omitted + warned; the deploy uses the
    #                                    ambient credentials and succeeds.
    #   * absent, no ambient creds    -> hard error naming the profile and the
    #                                    config files searched.
    # Prefer omitting aws_profile entirely for any stack that deploys from CI.

    # --- Deploy into an EXISTING VPC (optional) -------------------------------
    # By default rc creates its own VPC. Set vpc_id to deploy INTO an existing
    # one instead — use this when the stack must share a VPC + security group
    # with peer systems (so same-VPC SG references + Cloud Map DNS work, which
    # cross-VPC peering can't replicate). All keys below are opt-in; omit them
    # and behavior is unchanged.
    #   vpc_id: vpc-0abc123                       # adopt this VPC (vpc_cidr unused)
    #   public_subnet_ids: [subnet-a, subnet-b]   # >=2 AZs, PUBLIC (IGW route) —
    #                                             # ALB + Fargate (assign_public_ip)
    #   private_subnet_ids: [subnet-c, subnet-d]  # optional; defaults to public
    #   default_subnet_placement: private         # public (default) | private —
    #                                             # where a service with NO
    #                                             # `subnet_group:` lands. Without
    #                                             # this, private_subnet_ids above
    #                                             # is threaded through but never
    #                                             # actually used for placement;
    #                                             # every service still gets a
    #                                             # public IP unless it opts into
    #                                             # the heavier `network:` block
    #                                             # (which, in adopt mode, always
    #                                             # carves a NEW subnet — it can't
    #                                             # place a service on one you
    #                                             # already own).
    #   security_group_ids: [sg-mesh]             # extra SGs attached to every
    #                                             # task (join an existing mesh)
    #   internet_gateway_id: igw-0abc             # only needed if a declared
    #                                             # `network.subnets` group sets
    #                                             # public: true — an adopted
    #                                             # VPC's IGW isn't rc's to name
    # In adopt mode, declared `network.subnets` groups must carry explicit
    # `cidrs: [...]`: rc won't carve a block out of a range it doesn't own.
    # rc pre-flights the VPC + subnets against AWS before deploying. In adopt
    # mode rc creates NO VPC/IGW/subnets/route-tables and does NOT touch the
    # VPC's DHCP options, so cross-service discovery must use FQDNs
    # (<svc>.<project>.local) rather than short names.

    # --- Adopting a foreign ALB (optional) -------------------------------------
    # Two different modes, easy to reach for the wrong one:
    #   existing_alb: rc REFERENCES a live ALB read-only — a data source it
    #     never creates, updates, or destroys. Every public service must set
    #     `domain:`; rc only adds host-based listener rules onto the existing
    #     listener, never touches its default action.
    #   adopt_owned.alb: rc OWNS the ALB's lifecycle — a real `aws_lb` resource,
    #     imported once via terraform import, so rc holds update/destroy
    #     authority afterward (the point: retiring whatever CloudFormation/other
    #     stack used to own it). No domain-per-service restriction, since rc
    #     owns the listener's default action too.
    #   existing_alb:
    #     arn: arn:aws:elasticloadbalancing:...:loadbalancer/app/NAME/ID
    #     https_listener_arn: arn:aws:elasticloadbalancing:...:listener/.../...
    #   adopt_owned:
    #     alb:
    #       arn: arn:aws:elasticloadbalancing:...:loadbalancer/app/NAME/ID
    #       http_listener_arn: arn:aws:elasticloadbalancing:...:listener/.../...
    #       https_listener_arn: arn:aws:elasticloadbalancing:...:listener/.../...  # only required if any service sets domain:
    #       security_group_ids: [sg-abc, sg-def]  # the SGs already on the live ALB
    # Both modes work with `launch_type: EC2` services too — the EC2 capacity
    # instances' security group admits ALB traffic from the same source the
    # `tasks` SG already uses (the adopted/existing ALB's own security
    # groups), no rc-created `aws_security_group.alb` required.
    # Mutually exclusive with each other. adopt_owned.alb sets
    # `lifecycle { ignore_changes = all }` on the adopted resources — rc holds
    # delete/update authority but deliberately never diffs their live attributes
    # against what it would render from scratch (the adopted ALB's real name/SGs
    # essentially never match rc's `${project}-alb` convention; forcing that
    # would replace/destroy a traffic-serving ALB). Before apply, rc boto3-
    # verifies the given ARNs are actually live and hard-errors if not — a bad
    # ARN here would otherwise have terraform create a brand-new ALB instead.

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

# --- Multi-container task groups (optional) ---------------------------------
# Put N compose services in ONE ECS task. Every awsvpc task burns one branch
# ENI, and on a real 30-service estate the ENI dimension needed TWICE the
# instances memory did (4x m6i.large where memory wanted 2) — so task
# granularity, not instance size, is what sets the bill. Grouping keeps the
# per-tenant security boundary (unlike network_mode: bridge, which collapses
# every task onto the host ENI under one shared SG) and moves inter-container
# traffic to localhost.
#
# Omit this block entirely and nothing changes: every service becomes its own
# group named after itself, and rc emits byte-identical terraform.
#
# task_groups:
#   nginx:                                     # group name == ECS service name
#     services: [nginx, django, frontend]      #            == Cloud Map A record
#     ingress: nginx                           # which container the ALB targets;
#                                              # required when >1 member is public
#   postgres:
#     services: [postgres, redis]
#     memory: 1536                             # optional; default = SUM of members
#
# Everything else — replicas, stateful, auto_roll, deployment, launch_type,
# subnets, security_groups, iam_role — is read off the MEMBER services, and rc
# REJECTS a group whose members disagree. That is deliberate: silently making
# the app group stateful would turn a rolling deploy into a stop-then-start
# outage from an rc.yml that reads innocent.
#
# THE ONE THING GROUPING CHANGES FOR YOUR APP — read this before you group.
# AWS ECS allows exactly ONE service registry per service ("Multiple service
# registries for each service isn't supported" — CreateService), so a group
# gets ONE Cloud Map A record, at the GROUP's name. Members whose name is not
# the group's LOSE their own hostname. The promise that compose hostnames like
# `db` and `cache` just keep resolving has this exception, and nothing else.
#
# Reach a merged member at the group name on its own port (containers in one
# awsvpc task share an IP, and rc validates that their ports don't collide),
# or at localhost from inside the group. `rc plan` warns with the exact list of
# hostnames a proposed group retires, and names any compose env var still
# dialling one.
#
# So NAME THE GROUP AFTER THE MEMBER whose hostname you most want to keep —
# preferably the ALB-fronted one. That member keeps its Cloud Map record, its
# ECS service name, its task-def family, its terraform address and its ALB
# target group, which also turns a brownfield regroup into an in-place task-def
# revision for it instead of a destroy/create.
#
# Two more consequences worth knowing up front:
#   * rolling ANY member rolls the whole group — the task is the unit of
#     deployment, so there is nothing smaller to roll.
#   * `rc run` starts the whole TASK (run_task starts a task, not a container),
#     so a one-off against a member of a stateless group brings its siblings up
#     too. rc refuses it outright for a stateful group, where it would mean a
#     second postgres on the same EFS access point. `rc exec` reuses the
#     already-running task and has neither problem.

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

  celeryworker:
    type: worker
    cpu: 1024
    memory: 3072
    replicas: 3
    # Rollout percentages for THIS service. Omit the block and you get rc's
    # zero-downtime default (100/200): every old task stays up until a new one
    # is healthy, so the service briefly runs DOUBLE its tasks. On EC2 that
    # doubling is not transient — the ASG has to be big enough to hold it, all
    # month. 50/100 replaces tasks in place instead: 2 of 3 keep working while
    # the third is replaced, no extra capacity is ever needed, and the fleet is
    # sized by steady state. Sound for queue-backed work behind no ALB (the
    # cost is queue latency); NOT for a service serving requests.
    # Not available on stateful services (EFS mount, `stateful: true`, or a
    # -beat/-scheduler name): those are pinned at 0/100 so two tasks never
    # share a data directory, and rc rejects the block rather than ignore it.
    # On FARGATE, `maximum_percent: 100` also turns AZ rebalancing off for the
    # service (ECS rejects the combination) and buys no fleet saving, since
    # there is no ASG — rc warns when you do it.
    deployment:
      minimum_healthy_percent: 50      # 0-100; floor on tasks kept RUNNING
      maximum_percent: 100             # >=100; ceiling during the roll

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

  redis:
    type: infrastructure
    cpu: 256
    memory: 512
    # A service that ISN'T in docker-compose.yml has to say what it runs —
    # compose is the only other place rc reads images from. Set alongside a
    # compose `build:`, this overrides it: rc deploys this image and builds
    # nothing for the service.
    image: redis:7-alpine
```

Every service in the deploy set needs an image from somewhere: compose
`image:`, compose `build:`, or rc.yml `image:`. A service with none of the
three is rejected at config load rather than deployed against an empty ECR
repository it would never be able to pull from.

Full schema reference: [ARCHITECTURE.md § rc.yml v2 at a glance](ARCHITECTURE.md#rcyml-v2-at-a-glance).

### Declared network — standalone security groups, private subnets, VPC endpoints

By default rc derives its network from your services: two security groups
(`alb` and `tasks`), and every service lands on `tasks`, which carries implicit
ALB ingress and blanket `egress → 0.0.0.0/0`. That is the right default for a
web app and its workers.

When you need an isolated blast radius — or when something *outside* rc
launches tasks (a backend calling `run_task`) and has no service for rc to hang
a group off — declare the network instead. These are standalone resources: a
declared group can be attached to an rc service, referenced by another declared
resource, or by nothing at all, in which case it simply exists and its id is
exported.

```yaml
network:
  security_groups:
    isolated-tasks:
      # No ingress key at all = nothing reaches this. rc never injects an ALB
      # rule; a service that wants one says `from: alb`.
      egress:                     # an allow-list, NOT the implicit ALL → 0.0.0.0/0
        - to: endpoint:ecr        # …a declared VPC endpoint (defaults to 443)
        - to: endpoint:logs
        - to: endpoint:s3
        - to: sg:api              # …another declared group
          ports: [5000]
        - to: cidr:0.0.0.0/0      # …or a literal CIDR
          ports: [53]
          protocol: udp

  subnets:
    tasks-private:
      public: false
      egress: endpoints           # endpoints | nat | none
      # `endpoints` emits NO 0.0.0.0/0 route at all: reachability comes only
      # from the VPC endpoints below. That is the NAT-free path — no NAT
      # gateway, no hourly charge, no internet routing.

  endpoints:
    ecr:  { services: [ecr.api, ecr.dkr], subnets: [tasks-private] }
    logs: { services: [logs],             subnets: [tasks-private] }
    sts:  { services: [sts],              subnets: [tasks-private] }
    s3:   { services: [s3],               subnets: [tasks-private] }   # gateway

repositories:
  db-sidecar:
    mirror: postgres:16-alpine    # records intent; rc creates the repo, you push

services:
  worker:
    security_groups: [isolated-tasks]   # REPLACES the shared tasks group
    subnets: tasks-private              # assign_public_ip derived from the group
```

Rule sources and destinations are `<kind>:<value>` — `sg:`, `service:`,
`endpoint:`, `cidr:` — plus the bare keywords `alb` and `self`.

What rc enforces so you can't ship a broken segment:

- **Default-deny is real.** Declared groups emit
  `aws_vpc_security_group_{ingress,egress}_rule` resources rather than inline
  blocks, so the AWS provider strips the allow-all egress rule AWS attaches at
  creation. No rule means no access.
- **Both halves of a two-sided rule.** `to: endpoint:ecr` grants the group
  egress *and* grants the endpoint's own security group the matching ingress.
  Writing one half and silently getting no connectivity is not expressible.
- **Replace, never append.** A service naming `security_groups:` gets exactly
  those — it is not joined to the shared `tasks` group, so it inherits neither
  its ALB ingress nor its blanket egress. A `public: true` service that does
  this must re-admit the ALB, or rc refuses.
- **NAT-free subnets are checked.** A task placed in an `egress: endpoints`
  subnet must be able to reach `ecr.api`, `ecr.dkr`, `s3`, and `logs` (plus
  `secretsmanager` if it has secrets) through endpoints attached to *that same
  subnet group*. Otherwise rc names the missing ones instead of letting the
  deploy fail minutes later with `CannotPullContainerError`.
- **Unreachable endpoints are refused.** An interface endpoint nothing egresses
  to is a paid ENI per AZ serving no traffic.

- **Address plan is checked, not assumed.** Subnet CIDRs are compared as
  concrete networks, so an explicit `cidrs:` that overlaps an auto-allocated
  block, rc's own built-in subnets, or another group is rejected at emit time
  rather than at apply as `InvalidSubnet.Conflict`. A CIDR outside `vpc_cidr` is
  rejected too.
- **Names are checked against rc's own.** A declared group called `tasks`, or a
  repository called `buildcache` or named after a service, would collide on the
  AWS name — `terraform validate` passes and *apply* fails. rc catches those,
  along with declared names that flatten to the same terraform address (`-` and
  `.` both become `_`).

Auto-allocated subnet CIDRs are derived from the group's **name**, not its
position, so adding a group never moves an existing one's block. That matters
because `cidr_block` is ForceNew on `aws_subnet`: renumbering a live group means
terraform destroys and recreates subnets that have running task ENIs attached.
Pin a block explicitly with `cidr_offset:` (or full `cidrs:`) if you want it
independent of the name entirely.

Omit both blocks and the emitted terraform is byte-identical to a stack that
predates them.

Every created id comes back out via `rc outputs`:

```console
$ rc outputs --env
RC_CLUSTER_NAME=my-app-prod
RC_VPC_ID=vpc-0a1b2c
RC_SECURITY_GROUPS_ISOLATED_TASKS=sg-0d4e5f
RC_SUBNETS_TASKS_PRIVATE=subnet-0aa,subnet-0bb
RC_SUBNET_EGRESS_MODES_TASKS_PRIVATE=endpoints
RC_VPC_ENDPOINTS_ECR_ECR_API=vpce-01
RC_REPOSITORIES_DB_SIDECAR=1234.dkr.ecr.us-west-2.amazonaws.com/my-app/db-sidecar
```

`rc outputs --json` for machine consumption, `rc outputs <name>` for one value.

### Declared task roles — per-service IAM instead of one shared role

rc emits one task role, `${project}-task`, and every task definition points at
it. `provider_config.ecs.iam` bolts grants onto *that* role, so a permission
written for one service is silently held by every other service in the stack.

Declare a role and name it on the services that need it:

```yaml
iam_roles:
  media-writer:
    description: S3 media write for the web tier
    managed_policies:
      - arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess
    statements:                        # same shape as provider_config.ecs.iam
      - sid: WriteMedia                # optional; IAM requires alphanumeric
        actions: [s3:PutObject, s3:GetObject]
        resources: ["arn:aws:s3:::my-app-media/*"]
        condition:                     # optional
          Bool: { "aws:SecureTransport": "true" }
    tags: { tier: web }                # surfaces as aws:PrincipalTag

  locked-down: {}                      # no grants at all, deliberately

services:
  web:
    iam_role: media-writer             # REPLACES the shared ${project}-task role
  worker:
    iam_role: locked-down
  nginx: {}                            # no iam_role -> shared role, as before
```

Why a top-level block rather than `services.<name>.iam:` — grants cluster by
tier, not by service, so inlining them means copy-pasting a list that then
drifts; two services sharing `iam_role: media-writer` provably share one AWS
role; and it matches the "declare it, reference it by name" shape the network
block already uses.

What holds regardless:

- **The shared role is never removed or renamed.** `aws_iam_role.task` is still
  emitted, still carries the `provider_config.ecs.iam` grants, and is still the
  `task_role_arn` of every service that does not name an `iam_role:`. This is an
  opt-in override, not a migration — a config that ignores it emits
  byte-identical terraform.
- **A declared role does *not* inherit `provider_config.ecs.iam`.** That is the
  point: those grants stay on the shared role.
- **ECS Exec keeps working.** Every declared role gets the same `ssmmessages:*`
  policy the shared role has, because `rc exec` / `rc db backup` / `rc db
  restore` authenticate the in-container SSM agent with the *task* role.
- **A role nobody references is fine** — its ARN is exported for an out-of-band
  consumer (`RC_IAM_ROLES_MEDIA_WRITER=arn:aws:iam::…:role/my-app-media-writer`).

The task *execution* role (`${project}-task-exec`, the one ECS itself uses to
pull images and read secrets) is untouched by any of this.

### default_launch_type — run every service on EC2 instead of Fargate

Every service defaults to `launch_type: FARGATE`. Switch the whole
environment to EC2-backed ECS with one key:

```yaml
provider_config:
  ecs:
    default_launch_type: EC2   # FARGATE (default) | EC2
```

A service's own `launch_type:` always wins over the default, so a mixed
fleet — some services on EC2, some on Fargate — is just per-service
overrides on top of the environment-wide default:

```yaml
provider_config:
  ecs:
    default_launch_type: EC2    # env-wide default

services:
  worker:
    launch_type: EC2            # redundant with the default here, but explicit
  web:
    launch_type: FARGATE        # opts OUT of the EC2 default
```

Any service that resolves to EC2 (via the default or its own override) is
scheduled through an `aws_ecs_capacity_provider` backed by an
`aws_autoscaling_group` of `aws_launch_template.ec2` instances. rc only
emits these into `capacity.tf` when at least one service actually needs
them — an all-Fargate stack renders no extra resources there.

Tune the ASG under `ec2_capacity`:

```yaml
provider_config:
  ecs:
    ec2_capacity:
      instance_type: m5.xlarge   # no fixed default — omit to auto-size, see below
      capacity_type: ON_DEMAND   # ON_DEMAND (default) | SPOT | MIXED
      spot_weight: 3             # MIXED only; default 3
      min: 1                     # default 1 when instance_type is set, else auto-sized
      desired: 1                 # default 1 when instance_type is set, else auto-sized
      max: 3                     # default 3 when instance_type is set, else auto-sized
      subnet_group: asg-private  # optional — a declared network.subnets group; see below
      eni_trunking: auto         # auto (default) | true | false — see below
      size_for_rolling_deploy: false  # default false — see below
      root_volume_size: 120      # GiB; omit to inherit the AMI's 30 GiB — see below
      root_volume_type: gp3      # gp3 (default) | gp2 | io1 | io2 | standard
      root_volume_encrypted: true  # default true
```

- **`instance_type`** — the EC2 shape backing the ASG. No fixed default:
  set it explicitly, or omit it to let rc auto-size (below).
- **`capacity_type`** — `ON_DEMAND` (default), `SPOT`, or `MIXED`. `SPOT`
  renders a `mixed_instances_policy` with `on_demand_base_capacity = 0`
  (100% spot, `capacity-optimized` allocation). `MIXED` keeps one
  on-demand instance as a floor (`on_demand_base_capacity = 1`) and fills
  the rest with spot.
- **`spot_weight`** — only consulted when `capacity_type: MIXED`. Feeds
  `on_demand_percentage_above_base_capacity = 100 // (spot_weight + 1)`;
  the default `3` works out to 25% on-demand / 75% spot above the
  one-instance floor.
- **`min` / `desired` / `max`** — ASG sizing. Any of the three you set
  explicitly is honored as-is. Whichever you omit is filled in by
  auto-sizing when `instance_type` is also omitted; otherwise the
  fallback is `1` / `1` / `3`.
- **`subnet_group`** — place the ASG's instances in a DECLARED
  [`network.subnets`](#what-rcyml-v2-looks-like) group instead of the
  environment-wide `default_subnet_placement`. Resolved through the exact
  same machinery a service's own `subnet_group:` uses: `assign_public_ip`
  always follows the named group's routing (`public: true` → public IP,
  `egress: nat` → none), never independently declared. This is also how
  you get NAT-gateway egress for EC2 capacity — point it at a group with
  `egress: nat`; declared groups already provision the real
  `aws_nat_gateway` + route table. Omit it and nothing changes (see
  below).
- One-off tasks (`rc run`, `mode: task` lifecycle hooks) launch on the same
  capacity as their service — via its capacity provider strategy on EC2, its
  launch type on Fargate. Note EC2 one-offs need a free slot on an instance
  that already exists: auto-sizing models declared services only, so a full
  fleet leaves a `migrate`-before-roll task `PENDING` while the ASG boots. rc
  warns at plan time when a `mode: task` hook runs on EC2 capacity.
- **`eni_trunking`** — `auto` (default), `true`, or `false`. With `awsvpc`
  networking every task consumes a whole ENI, and **ENI counts are flat
  across much of an instance family**: an `m5.2xlarge` is twice the box of an
  `m5.xlarge` and hosts the same 3 tasks. A fleet sized against that ceiling
  exists to satisfy a networking artifact rather than a workload — 11
  right-sized tasks needing 4.5 vCPU end up on 4 instances / 28 vCPU. ECS's
  `awsvpcTrunking` account setting lifts the cap (m5.xlarge: 3 → 20 tasks),
  and on `auto` rc reads it via `ecs:ListAccountSettings` during preflight
  and sizes accordingly.

  **`awsvpcTrunking` is PER-REGION.** `put-account-setting-default` applies
  only to the region it is called in, so enabling it in the region you tested
  from does nothing for a stack deployed elsewhere. rc names the region in
  every message about it for exactly this reason. Check with:

  ```
  aws ecs list-account-settings --name awsvpcTrunking --effective-settings --region <region>
  ```

  Set `true` to assert it without the API call — rc validates the instance
  family is eligible and **errors if not**, since sizing against a ceiling
  that doesn't exist leaves tasks `PENDING` forever. Eligibility is AWS's
  published list: the entire `t3`/`t3a`/`t4g` burstable family is *not*
  trunking-eligible at any size (so this knob is a no-op for rc's default
  auto-sizing ladder), and `m5.metal`/`c5.metal` are excluded even though
  their families are — don't infer a metal tier's limit from its siblings.
  Trunking only affects EC2 container instances; Fargate is unaffected.
- **`size_for_rolling_deploy`** — whether auto-sizing covers a rolling
  deploy or only steady state. Default `false`: rc sizes the ASG for the
  tasks you declared, exactly as it always has. But ECS permits up to 200%
  task duplication while a deploy is in flight
  (`deployment_maximum_percent`, rendered as 200 for normal services, 100 for
  stateful ones, and whatever `services.<svc>.deployment` declares when it
  does), so a fleet that is right at rest can be undersized at
  the only moment that matters — managed scaling then adds instances
  mid-deploy, and EC2 boot plus ECS agent registration takes minutes during
  which tasks sit `PENDING`. rc always **warns** when peak demand exceeds
  the sized fleet, naming the numbers. Set this `true` to have auto-sizing
  size for that peak instead; it removes the `PENDING` window and typically
  costs 1.5–2x the instances continuously, which is why it is opt-in rather
  than the default. Ignored when `instance_type` is set (auto-sizing doesn't
  run at all then) — the warning still fires.

  **It composes with `services.<svc>.deployment`.** The 200% is a per-service
  default, not a platform constant: a service that declares
  `deployment: {minimum_healthy_percent: 50, maximum_percent: 100}` rolls in
  place, so its peak demand *equals* its steady-state demand and it adds
  nothing for this knob to size for. Turn `maximum_percent: 100` on for the
  queue-backed workers (which is where the burst usually concentrates — they
  are the biggest tasks and have the most replicas) and
  `size_for_rolling_deploy: true` becomes close to free, because the only
  services still duplicating are the small request-serving ones that genuinely
  need the overlap. Measured on debuggai-api-prod (2026-08-23): 12 tasks
  reserving 25.8 GiB run on 5x m5.xlarge, ~34% memory utilised, with the
  rolling burst — concentrated in two 3-replica celery pools — as the reason
  the fleet has to be that size at rest.
- **`root_volume_size` / `root_volume_type` / `root_volume_encrypted`** —
  the container instance's root EBS volume. Omit `root_volume_size` and the
  launch template declares no `block_device_mappings`, so every instance
  inherits the ECS-optimized AMI's own root volume: **30 GiB gp2, shared by
  every task binpacked onto that instance**. This is the EC2-side answer to
  Fargate's `ephemeral_storage`, and it is deliberately not the same thing —
  `ephemeral_storage` is per-task and private, a root volume belongs to the
  instance and its tasks share it, so a task that fills the disk takes its
  neighbours down with it. A service moving from `ephemeral_storage: 40` on
  Fargate to EC2 needs this set, sized for the whole instance (roughly
  per-task GiB × tasks per instance). rc warns at plan time when it is unset
  and more than one task can land on an instance. `root_volume_type`
  defaults to `gp3` (cheaper than the AMI's gp2, and its 3000 baseline IOPS
  isn't tied to volume size); minimum size is 30 GiB, since an EBS root
  volume cannot be smaller than the AMI snapshot. Note a launch-template
  change only reaches an instance when that instance is **replaced** —
  existing container instances keep their current root volume until the ASG
  rolls them.
- `ec2_capacity` is also where the IMDS hardening knobs (`imdsv2`,
  `metadata_hop_limit`, `block_task_imds`) live — covered next.

**Omitting `instance_type` triggers auto-sizing** (`autosize.py`): rc picks
the smallest t3-family shape that fits the single largest EC2 task's
CPU/memory request, then sizes the ASG to cover total EC2 task demand
across three independent dimensions — CPU, memory, and awsvpc task ENIs —
taking whichever needs the most instances. See [EC2 task density is capped
by ENI limits, not just
CPU/memory](#ec2-task-density-is-capped-by-eni-limits-not-just-cpumemory)
below for the ENI dimension's mechanics. `max` doubles as `auto_size()`'s
hard cap (default ceiling `10`): declared EC2 demand that needs more
instances than that raises a config error telling you to raise
`ec2_capacity.max` rather than silently under-provisioning.

With no `ec2_capacity.subnet_group`, EC2 instance placement (public vs.
private subnets) follows the same environment-wide
[`default_subnet_placement`](#what-rcyml-v2-looks-like) (`public` unless
set to `private`) that a Fargate service with no `subnet_group:` gets —
byte-identical to before `ec2_capacity.subnet_group` existed. Set it to
put the ASG on a declared group instead — the same one a service's own
`subnet_group:` can name, or a dedicated group just for capacity. One ASG
still hosts every EC2-launch-type service in the stack regardless of what
each service's own `subnet_group:` says, so this only controls where the
*instances* sit, not which services land on them.

A declared group with `egress: endpoints` (VPC-endpoints-only, no default
route) is refused at plan time for `ec2_capacity.subnet_group` today: the
ASG's instances always sit on the fixed `${project}-ec2-instances`
security group, which is never a member of `network.security_groups` and
so can never be granted ingress by a VPC endpoint's own security group the
way a service's own `security_groups:` override can. A container
*instance* also has to reach more than a task does just to register with
the cluster in the first place — the ECS agent polls
`com.amazonaws.<region>.{ecs, ecs-agent, ecs-telemetry}`, none of which a
Fargate task (or an EC2 task's own `awsvpc` ENI) ever needs, because
Fargate's control-plane traffic never transits your VPC. Use `egress: nat`
or a public group for `ec2_capacity.subnet_group` instead.

**Reverting an EC2 pilot isn't clean.** Moving a service back from
`launch_type: EC2` to `FARGATE` — dropping `capacity_provider_strategy` and
setting `launch_type` again — hits the identical AWS provider requirement
in reverse (a real repro from a browser-mgr pilot): `Error:
force_new_deployment should be true when capacity_provider_strategy is
being updated`. rc does not emit `force_new_deployment = true` on the
`FARGATE` branch to work around this — that branch is the default output
for every non-EC2 service in every rc-managed stack, and setting it there
would mean any ordinary Fargate service update (a replica-count bump with
no other change, say) also force-cycles every running task, which is a
real behavior change for users who have never touched EC2. Also observed
in the same pilot: the transition forces a full `aws_ecs_service`
replacement rather than an in-place update (`launch_type` is effectively
immutable across this specific field combination), so reversing an EC2
pilot is not a quick toggle either way. If you need to revert, coordinate
with your team on the exact `terraform apply`/`plan` invocation for that
one service rather than assuming a standard `rc deploy` handles it
cleanly.

### IMDS hardening on EC2 container instances

Only relevant when a service sets `launch_type: EC2`. Fargate tasks take their
credentials from the task metadata endpoint (169.254.170.2), not EC2 IMDS, and
`aws_ecs_task_definition` has no metadata options to set — the exposure is the
*instance* role, and it lives on `aws_launch_template.ec2`.

rc now emits, by default:

```hcl
metadata_options {
  http_endpoint               = "enabled"
  http_tokens                 = "required"   # IMDSv2 only
  http_put_response_hop_limit = 2
}
```

`http_tokens = "required"` is the mitigation that matters: a forged `GET` from
inside a container cannot mint the session token IMDSv2 demands, so an SSRF bug
no longer yields the instance role. Tune it under `ec2_capacity`:

```yaml
provider_config:
  ecs:
    ec2_capacity:
      imdsv2: required          # required (default) | optional
      metadata_hop_limit: 2     # default 2; 1 is the strict setting
      block_task_imds: false    # default false
```

The hop limit is 2 rather than 1 on purpose. It is the IP TTL of the token
response, and each container network hop decrements it: 1 admits only the
instance's own network namespace and cuts off every bridge-mode container,
which is a silent, instance-wide change to make to a stack that already runs.
And 1 is *not* the awsvpc cut-off it looks like — an awsvpc task reaches IMDS
over its own ENI. To deny rc's tasks the instance role, set
`block_task_imds: true`, which writes `ECS_AWSVPC_BLOCK_IMDS=true` into the ECS
agent config. Tasks keep their own task role either way.

`http_endpoint` is deliberately not configurable: disabling IMDS entirely stops
the ECS agent registering the instance, so the stack would never run a task.

### Multi-container task groups

Every ECS task uses `awsvpc` network mode, so **every task burns one ENI** — and
by default rc renders one task per compose service. On a measured 30-service
estate that made ENI, not memory, the dimension that set the fleet size: the ENI
math wanted 4x `m6i.large` while memory alone wanted 2, and the account had
already hit a real `AssociationLimitExceeded` placement failure. 61.3 GB of
memory was registered against 5.9 GB actually in use.

`task_groups:` puts N compose services in ONE task, behind one ENI:

```yaml
task_groups:
  nginx:                                  # group name == ECS service name
    services: [nginx, django, frontend]   #            == Cloud Map A record
    ingress: nginx                        # ALB target; required if >1 is public
  postgres:
    services: [postgres, redis]
    memory: 1536                          # optional; default = SUM of members
```

Same estate, regrouped 5 tenants x 2 tasks: 10 ENIs, which fits **one**
`m6i.xlarge`. Note that resizing alone buys nothing — 30 ungrouped tasks still
need 2x `m6i.xlarge`, the same $/mo as 4x `m6i.large`. Task granularity is the
lever, not instance size.

**Omit the block and nothing changes.** Every service becomes an implicit group
of one named after itself, and rc emits byte-identical terraform — guarded by a
golden fixture that renders through the same template loop as a group of N.

**Why not `network_mode: bridge`?** It removes the ENI dimension outright, and
it was rejected on evidence rather than taste: bridge puts every task on the
host ENI under one shared security group, collapsing per-tenant isolation into
a single boundary. Grouping keeps each tenant's own SG and moves
inter-container traffic to localhost.

**Group properties derive from members, and disagreement is an error.**
`replicas`, `stateful`, `auto_roll`, `deployment`, `launch_type`, `subnets`,
`security_groups`, `iam_role` and `ephemeral_storage` are all read off the
member services; rc rejects a group whose members disagree instead of picking a
winner. That is deliberate — silently making the app group `stateful` would turn
a rolling deploy into a stop-then-start outage from an rc.yml that reads
innocent, and member order would decide it. `memory` and `cpu` SUM (on Fargate
`cpu` is a required task-level reservation and AWS only accepts certain
cpu/memory pairs). Note that `iam_role` uniformity is not a limitation rc chose:
`task_role_arn` is a task-level field, so a group genuinely has one task role.

#### The one thing grouping changes for your application

AWS ECS allows exactly **one service registry per service** — *"Multiple service
registries for each service isn't supported"* (`CreateService`). So a group gets
ONE Cloud Map A record, at the **group's** name, and members whose name is not
the group's **lose their own hostname**. rc's promise that compose hostnames
like `db` and `cache` keep resolving has this one exception.

Reach a merged member at the group name on its own port — containers in one
awsvpc task share an IP, and rc validates their ports don't collide — or at
`localhost` from inside the group. `rc plan` warns with the exact list of
hostnames a proposed group retires and names any compose env var still dialling
one, so this is visible before you apply rather than after the stack comes up
green and fails to connect.

**Name the group after the member whose hostname you most want to keep**,
preferably the ALB-fronted one. That member keeps its Cloud Map record, ECS
service name, task-def family, terraform address, ALB target group and listener
rule — which also turns a brownfield regroup into an in-place task-def revision
for it rather than a destroy/create.

#### Operational consequences

- **Rolling any member rolls the whole group.** The task is the unit of
  deployment; there is nothing smaller. `rc deploy --services django` restarts
  its groupmates too, which is why members must agree on `auto_roll` — otherwise
  a groupmate in the default roll set would drag an opted-out member along.
- **`rc run` starts the whole task.** `run_task` starts a task, not a container,
  and `containerOverrides` only changes the named one's command. rc refuses it
  for a member of a *stateful* group (it would put a second postgres on the same
  EFS access point) and warns for a stateless one, since a migrate hook would
  bring the siblings up as a throwaway task. `rc exec` reuses the running task
  and has neither problem.
- **`rc status` reports one entry per group**, because desired/running counts
  belong to the task.
- **`rc logs <member>` is unaffected.** Log stream prefixes stay per-container.
- **`essential: false` is not crash isolation.** ECS never restarts an
  individual container, so a non-essential container that exits stays dead while
  the task runs on without it — quieter, not safer, than the whole-task restart
  `essential: true` gives you. Default is `true`, and a task must have at least
  one essential container. `rc plan` warns when a container is non-essential
  with no `restart_policy`.

#### Restarting one container instead of the whole task

`restart_policy` is the knob that actually gives a grouped task compose-like
recovery — it restarts *that* container in place rather than replacing the task:

```yaml
services:
  reingest:
    essential: false
    restart_policy:
      enabled: true
      ignored_exit_codes: [0]     # a clean finish is not a failure
      attempt_period: 120         # seconds; AWS allows 60–1800, default 300
```

Declaring the block is the opt-in. It is never turned on for you: rc emits
terraform offline, so it cannot check the container-agent version on your
behalf, and this needs **agent 1.86.0+** on EC2 (rc's own ECS-optimized AL2 AMI
ships far newer — 1.106.1 as of 2026-08-26). It works on essential and
non-essential containers alike.

**It fixes transient exits, not crash loops.** A container must run for
`attempt_period` before a restart is attempted, so one that dies faster than
that is *not* restarted and falls through to whatever `essential` says. The
combination closest to a compose box is `essential: true` plus a restart policy:
a one-off exit restarts the container alone, and a genuine crash loop still
replaces the task.

### EC2 task density is capped by ENI limits, not just CPU/memory

Only relevant when a service sets `launch_type: EC2`. Every ECS task uses
`awsvpc` network mode regardless of launch type, and on EC2 (unlike Fargate)
that means each task gets its own elastic network interface (ENI) attached to
the container instance that hosts it. Every EC2 instance type has an AWS-wide
ceiling on attached ENIs, and one of those is always the instance's own
primary network interface — never available to a task. So the number of
awsvpc tasks a container instance can actually host is `max_enis - 1`, not
however many fit by CPU/memory math alone. A pile of small, high-replica-count
services can hit this ceiling long before CPU or memory does.

**Bin-packing.** Every `launch_type: EC2` service gets
`ordered_placement_strategy { type = "binpack", field = "memory" }` on its
`aws_ecs_service`. Without a placement strategy, ECS spreads tasks with no
particular packing goal, so the ASG scales out to cover them and EC2 launch
type loses its cost advantage over Fargate — bin-packing by memory is what
lets several services' tasks actually land on the same instances. Trade-off:
ECS's automatic AZ rebalancing only applies to a service whose first (or
only) placement strategy is an AZ spread, or that declares none at all —
binpack-first makes an EC2-launch service ineligible for it. Deliberate:
bin-packing for density and spreading for AZ resilience pull in opposite
directions, and EC2 capacity here optimizes for the former.

rc's default t3 ladder, verified against the live AWS API
(`aws ec2 describe-instance-types`, `NetworkInfo.MaximumNetworkInterfaces`):

| Instance type | Max ENIs | Usable for tasks (max − 1) |
| --- | --- | --- |
| t3.small / t3.medium / t3.large | 3 | 2 |
| t3.xlarge / t3.2xlarge | 4 | 3 |

`autosize.py`'s `auto_size()` treats this as a third sizing dimension
alongside CPU and memory: it computes the instance count needed to cover the
total EC2 task count (summed across replicas, with the same `safety_headroom`
multiplier CPU/memory use) at that ceiling, and takes the max of all three
dimensions. A tiny task with a high replica count can therefore need more
instances than its CPU/memory footprint alone would suggest, even though
`instance_type` selection (the smallest shape that fits the *largest single*
task) stays CPU/memory-only — the ENI ceiling is a floor on instance *count*,
not a factor in shape choice, so a many-tiny-task stack still lands on
`t3.small` rather than a shape with better ENI density per dollar. This
headroom multiplier is only an approximation for ENIs, though: it does not
model the up-to-200% task duplication ECS permits mid-rolling-deploy for
non-stateful services, so a 2-replica service can briefly need double its
steady-state ENI slots while a deploy is in flight. If tasks sit `PENDING`
for ENIs specifically during rolling deploys, raise
`ec2_capacity.max`/`safety_headroom`, move to a bigger shape, or take the
duplication away at the source with `services.<svc>.deployment`
(`maximum_percent: 100` rolls in place and consumes no extra ENI slot).

**Why not ENI trunking (`awsvpcTrunking`) instead?** AWS does offer an
account-level ECS setting (`aws_ecs_account_setting_default` with
`name = "awsvpcTrunking"`) that raises the ENI ceiling for *newly launched*
instances of eligible types. It was evaluated and rejected for now: (1) it's
an AWS account/region-wide setting — turning it on changes ECS behavior for
every workload in that account/region, including ones rc doesn't manage, which
is a real, unwanted side effect for a tool that deploys into a user's existing
account; and (2) it would be a no-op for rc's default ladder regardless —
checked AWS's own published list of ENI-trunking-eligible instance types
(general purpose, compute optimized, memory optimized, storage optimized,
accelerated computing, HPC — all six family tables), and **no `t3.*` entry
appears in any of them, at any size**. Using it at
all would require a custom ladder pinned to trunking-eligible families (m5,
c5, r5, and their newer generations), on top of the account-wide opt-in — a
meaningfully bigger, riskier feature than sizing around the ceiling. Revisit
if a workload's replica-count-driven instance sprawl becomes a real cost
problem that a bigger `instance_type` doesn't fix.

**Setting `ec2_capacity.instance_type` explicitly is validated too.**
`auto_size()` never runs when `instance_type` is given directly — rc used to
take `min`/`desired`/`max` straight from config with no check that the
declared EC2 task demand (CPU, memory, or ENI count) actually fits, so
infeasible config emitted clean terraform and only failed later as tasks
stuck `PENDING` in real ECS. rc now validates against the same three
dimensions for every instance type it carries verified numbers for:

| Family | Sizes covered |
| --- | --- |
| `t3` / `t3a` / `t4g` (burstable) | nano → 2xlarge |
| `m5` / `m6i` (general purpose) | large → 24xlarge, metal |
| `c5` / `c6i` (compute optimized) | large → 24xlarge, metal |

Verified live against `describe-instance-types` (2026-08-18) — not derived
from a per-family/per-size formula. Two concrete reasons why: `t3a.small` has
**half** the usable ENI slots of its identically-shaped `t3.small`/`t4g.small`
siblings (max ENIs 2 vs. 3), and `c5`/`c6i` carry **half** the memory of a
same-labeled `m5`/`m6i`/`t3` size at the same vCPU count (`c5.xlarge` = 8 GiB,
`m5.xlarge`/`t3.xlarge` = 16 GiB). A table generalized from either family
would get these wrong. Setting `instance_type` to anything outside this list
is simply not modeled — rc skips the check rather than guess, the same way a
caller-supplied auto-sizing ladder with no ENI data skips the ENI dimension.

### Worked example: Django + Celery, web on Fargate, workers on EC2

The common shape this is for: a Django app where `web` is bursty and
request-driven (Fargate — you pay per task, scales cleanly with traffic) but
`worker`/`beat` are always-on background daemons that never scale to zero.
Running always-on processes on Fargate means paying the Fargate premium
24/7 for capacity that never idles down; one small EC2 instance hosting both
is usually cheaper the moment "always on" is true. Extends the `postgres` /
`django` / `nginx` example above — `postgres`, `django`, and `nginx` are
unchanged, omitted here for brevity:

```yaml
services:
  redis:
    type: infrastructure
    cpu: 256
    memory: 512

  worker:
    type: worker
    cpu: 256
    memory: 512
    launch_type: EC2                 # overrides Fargate for this service only
    command: ["celery", "-A", "myapp", "worker", "-l", "info"]

  beat:
    type: worker
    cpu: 128
    memory: 256
    launch_type: EC2
    command: ["celery", "-A", "myapp", "beat", "-l", "info"]

provider_config:
  ecs:
    ec2_capacity:
      instance_type: t3.small        # explicit, not auto-sized -- see below
      min: 1
      max: 1                         # never scale to a second instance
      desired: 1
```

What each knob is doing and why:

- **`launch_type: EC2` only on `worker`/`beat`** — `django` and `nginx` stay
  on Fargate (the env-wide default). Per-service `launch_type:` always beats
  the default, so a mixed fleet is just this one override, not a
  wholesale `default_launch_type: EC2` switch.
- **`ec2_capacity.max: 1`** pins the ASG to a single instance so `worker` and
  `beat` are *guaranteed* to land together, not just likely to. Without this,
  ECS's scheduler bin-packs opportunistically — usually the same outcome at
  this scale, but not a guarantee if the ASG ever has room for two.
- **`instance_type: t3.small` set explicitly, not auto-sized** — this is the
  ENI ceiling from the section above, applied: `worker` + `beat` is exactly 2
  awsvpc tasks, and `t3.small` has exactly 2 usable ENI slots. That fits with
  zero headroom for a third process. Add `flower` or a second queue's worker
  later and this same config needs `t3.xlarge` (3 usable slots) — not because
  CPU/memory ran out, but because ENIs did. Auto-sizing (omit `instance_type`)
  already accounts for this; setting it explicitly here makes the ceiling
  visible rather than implicit.
- **`redis`** is the Celery broker, Fargate (no `launch_type:`) since it's
  not part of this EC2 story — apply the same worker-on-EC2 reasoning to it
  too if it's also always-on and small enough to share the box.

Verified: this exact config (with `django`/`nginx`/`postgres` from the
example above) passes `ECSProvider().emit_terraform()` + `terraform
validate` — copy-paste starting point, not illustrative pseudo-YAML.

---

## Feature index

What's built and live-verified on the `portable-deploy` branch:

### Provider abstraction

- **`Provider` ABC** — every cloud target (ECS today, K8s next) implements `emit_terraform`, `plan`, `deploy`, `redeploy`, `status`, `logs`, `exec`, `rollback`, `destroy` against a shared `DeployContext`.
- **`FakeProvider`** for tests — every contract test runs against both `ECSProvider` and `FakeProvider` so adding a new provider is a copy-paste exercise.
- **rc-test-* tag** — every project named `rc-test-*` gets `Environment=rc-test` tags + `force_destroy=true` on destructive resources, so test stacks always tear down clean.

### ECS provider — what terraform we generate

- VPC + 2 public + 2 private subnets, IGW, security groups, default routing
- **`default_subnet_placement`** — environment-wide default (public today, opt into private) for any service with no explicit `subnet_group:`, without needing the heavier declarative `network:` block
- **`adopt_owned.alb`** — own a foreign ALB's terraform lifecycle (real resource + one-time `terraform import`), distinct from `existing_alb`'s read-only reference; the escape hatch for retiring a prior IaC tool (Copilot, hand-rolled CFN) that still owns a live, shared ALB
- **Declared network** (`network:`) — standalone, nameable security groups with default-deny ingress/egress, subnet groups with an explicit egress mode (`endpoints` / `nat` / `none`), and VPC endpoints; per-service `security_groups:` / `subnets:` that *replace* rc's defaults ([details](#declared-network--standalone-security-groups-private-subnets-vpc-endpoints))
- **Standalone ECR repos** (`repositories:`) — not tied to any service's build; for mirroring an upstream image into a NAT-free segment
- **Declared task roles** (`iam_roles:`) — opt-in per-service IAM instead of one shared task role every service inherits; the shared `${project}-task` role stays exactly as it was for anything that doesn't opt in ([details](#declared-task-roles--per-service-iam-instead-of-one-shared-role))
- **`default_launch_type: EC2`** — environment-wide switch to schedule every service through an EC2-backed `aws_autoscaling_group` capacity provider instead of Fargate; per-service `launch_type:` always overrides it. `ec2_capacity` tunes instance type, on-demand/spot mix, and ASG sizing, auto-sized from declared task demand when `instance_type` is omitted ([details](#default_launch_type--run-every-service-on-ec2-instead-of-fargate))
- **IMDS hardening on EC2 capacity** — IMDSv2 required on `aws_launch_template.ec2` by default, container-compatible hop limit, opt-in `ECS_AWSVPC_BLOCK_IMDS` ([details](#imds-hardening-on-ec2-container-instances))
- **ENI-aware EC2 auto-sizing** — `autosize.py` treats awsvpc's one-ENI-per-task ceiling (`max_enis - 1` usable per instance, verified against the AWS API) as a third sizing dimension alongside CPU/memory, so a high-replica-count stack of small tasks can't under-provision instance count ([details](#ec2-task-density-is-capped-by-eni-limits-not-just-cpumemory))
- **EC2 bin-packing** — every `launch_type: EC2` service gets `ordered_placement_strategy { type = "binpack", field = "memory" }`, so multiple services actually share instances instead of each nudging the ASG to add another one ([details](#ec2-task-density-is-capped-by-eni-limits-not-just-cpumemory))
- **Explicit `ec2_capacity.instance_type` density validation** — CPU/memory/ENI feasibility checked against a verified table (t3/t3a/t4g/m5/m6i/c5/c6i) even when auto-sizing is bypassed, so an infeasible shape+demand combo fails at `emit_terraform` instead of leaving tasks `PENDING` in real ECS ([details](#ec2-task-density-is-capped-by-eni-limits-not-just-cpumemory))
- **`existing_alb`/`adopt_owned.alb` + `launch_type: EC2`** — EC2 capacity instances admit ALB traffic from the adopted/existing ALB's own security groups, same source the `tasks` SG already uses; no rc-created ALB security group required
- ECS cluster (Container Insights off by default — expensive CloudWatch metric ingestion; opt in with `provider_config.ecs.container_insights: true`)
- Per-service: ECR repo. Per **task group**: task def, ECS service, Cloud Map
  service-discovery entry — and with no `task_groups:` block every service is
  its own group, so that is one of each per service (see
  [Multi-container task groups](#multi-container-task-groups))
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
| `rc destroy --yes` | terraform destroy. On EC2-launch stacks, first drains every deployed service to `desiredCount=0` and scales the capacity ASG to zero via the SDK — including services the local config no longer names (an ephemeral stack's compose file is gone by then), which are found by enumerating the cluster. Fargate-only stacks make zero AWS calls here. |
| `rc status` | ECS service health table |
| `rc outputs [--json\|--env] [<name>]` | resource ids from the deployed stack — cluster, ALB, ECR repos, plus every declared security group / subnet / VPC endpoint / repository. `--env` emits `KEY=value` for piping into a `.env` |
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
│           ├── cluster.tf.j2        # ECS cluster (Container Insights opt-in)
│           ├── domain.tf.j2         # ACM cert (with SANs) + R53 records
│           ├── efs.tf.j2            # EFS + access points (uid/gid/mode)
│           ├── iam.tf.j2            # task-execution + shared/declared task roles + ssmmessages policy
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
