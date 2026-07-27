# I open-sourced remote-compose: one command, your whole stack on AWS — with Claude already running inside

*MIT-licensed. Alpha. Ready for opinionated feedback.*

---

I've been quietly building a tool to scratch two of my own itches:

1. **Spinning up a fresh dev environment for every Claude agent I run** — without the "wait while I install Postgres / clone the repo / set the env vars / authenticate" dance.
2. **Deploying my docker-compose stacks to real AWS** — without writing a 500-line terraform module per service or surrendering to Heroku-style lock-in.

It turns out both problems have the same shape. Compose already describes how my services fit together. The job of a deploy tool isn't to make me re-describe that topology in ECS task defs or k8s YAML — it's to add the handful of things compose can't express (CPU, secrets, public hostname) and put the result somewhere useful.

So I built **`remote-compose`** (or `rc`, on the command line). Today it's MIT-licensed at [github.com/qosha1/remote-compose](https://github.com/qosha1/remote-compose).

It does two things, and the first one is the one I actually want to talk about.

---

## `rc dev up` — a fresh EC2 box with Claude inside, in five minutes

```bash
GH_TOKEN="$(gh auth token)" rc dev up alice \
  --repo https://github.com/owner/myapp \
  --compose docker-compose.yml \
  --skip-permissions
```

In about five minutes, you get back:

- A fresh EC2 instance (defaults to `t4g.2xlarge`, ARM, ~$0.27/hr — 8 vCPU,
  chosen because provisioning is dominated by CPU-bound image builds;
  pass `--instance-type t4g.large` for a cheaper, slower box)
- Docker installed, your repo cloned, `docker compose up -d --build` already run
- **Claude Code preinstalled and already authenticated** (your local OAuth token shipped to the box)
- A tmux session named `claude` waiting at the repo root
- `bd`, `gh`, your `~/.claude` settings + hooks + agents — all pre-installed and pre-configured
- A public IP with the ports your compose declares already open in the security group

Then:

```bash
rc dev attach alice
```

You drop into a Claude prompt. Pre-authed. Repo loaded. Stack running. Ready to work.

That's it. No SSH dance. No "let me install dependencies." No "where's my .env file." When you're done:

```bash
rc dev destroy alice
```

Everything gone. EBS, EIP, security group, key pair, instance — all torn down via the terraform module rc generated and applied for you.

### Why this is the part I'm excited about

I run multiple Claude agents in parallel — one for each branch, each long-running task, each "let me explore this idea while I do something else." The friction was always **environment**. Each agent needs its own database, its own port, its own copy of the repo, its own auth state. Running them all on my laptop means port collisions, RAM pressure, and "wait, which docker-compose did I leave running?"

Running them as separate cloud boxes solves all of that. But cloud boxes have their own friction — manual provisioning, manual auth, manual repo cloning, manual env file shipping. `rc dev` exists because I wanted that friction to be a single command, every time.

The bigger feature: **multi-repo deploys.** Real platforms aren't one repo:

```bash
rc dev up alice \
  --repo https://github.com/me/backend \
  --repo https://github.com/me/frontend \
  --repo https://github.com/me/workers \
  --compose ./docker-compose.full.yml \
  --compose ./docker-compose.workers.yml \
  --port 8000 --port 3000 --port 5555 \
  --env backend/.envs/.local/.django \
  ...
```

Each compose file becomes its own `docker compose -p <project>` project on the box (so service-name conflicts across repos don't collide). All three repos clone as siblings. Claude lands at the workspace root and can navigate any of them.

I run this against a 16-container production-shape stack (Django + Postgres + Redis + Celery + browser pool + Next.js frontend) and it just works.

---

## `rc deploy` — the original use case

The other half of `rc` is what I built first: turning a `docker-compose.yml` into a real ECS terraform module.

```bash
rc up --from-compose docker-compose.yml
```

This reads your compose, asks for the things compose can't express (CPU/memory, secrets, public hostnames, EFS uids), generates a clean ECS terraform module, applies it. Then everyday verbs:

- `rc deploy` — build, push, terraform apply, force-roll the new image
- `rc lifecycle migrate` — run a named hook in a live container
- `rc db push /tmp/local.dump` — seed the deployed database from a local pg_dump
- `rc exec django -- /bin/bash` — interactive shell into a running task
- `rc destroy --yes` — tear it all down

The key design choice: **the terraform module rc generates is yours.** You can `cd terraform/ && terraform apply` and never run `rc` again. It's a generator, not a runtime. If `rc` disappears tomorrow, your infrastructure keeps working.

I've hand-tested this against a real production-shape Django stack: VPC + 4 subnets, ECS cluster with Container Insights, ALB with HTTP→HTTPS + ACM cert + Route53, EFS per stateful volume, Secrets Manager (one secret per `.env` file, JSON-keyed so each KEY arrives as its own env var), ECS Exec wired, full data restore from a 569 MB local dump.

---

## Why both modes share one tool

Compose is the spec. For dev, I ship that spec to an EC2 box and start an agent inside. For production, I render it to ECS terraform. **Either way, you write the compose file once.**

That's the bet. Most deploy tooling makes you choose:

- **Cloud-specific knobs** (ECS task defs, k8s manifests, Helm charts) — same app config in three places.
- **Black-box PaaS** (Heroku, Fly, Render) — opinionated, locked-in, hard to escape.
- **Hand-rolled terraform** — flexible but a 500-line module per service.

`remote-compose` says: your compose file already describes the topology. The tool's job is to add the few things it can't express, and put the result somewhere useful. Sometimes "somewhere useful" is production ECS. Sometimes it's a disposable EC2 box that disappears in an hour.

---

## Status: alpha, real-world tested, opinionated feedback wanted

This is alpha software. I've used it heavily for my own work. The core flows are scripted as acceptance tests that hit real AWS (`bash scripts/test-startsimpli-end-to-end.sh` runs a clean account → full Django+celery+nginx stack → clean teardown without touching aws-cli).

**What works today** (live-verified):
- ECS provider end-to-end (the one provider that ships)
- `rc dev up` against single and multi-repo deploys with claude pre-auth
- AWS Copilot migration (`rc copilot import` reads any `copilot/` tree → rc.yml v2 + compose)
- Lifecycle hooks (`auto_on_deploy`, `run_once` with probes, interactive TTY)
- `rc db push` for seeding deployed Postgres from local dumps

**What's roadmap:**
- Kubernetes provider (the contract is designed for it; ECS implementation is the proof)
- Private subnets + NAT
- Auto-detection of compose env_file references for `rc dev up --env` discoverability
- Per-repo branch override on multi-git deploys

**What I'd love feedback on:**
- The `rc dev` model (one EC2 per agent vs. shared cluster vs. local docker)
- Multi-repo deploys (does `--compose` repeatable + per-project semantics map to your stack?)
- The terraform-you-own promise — is the generated module actually what you'd want to maintain?
- What's missing from the rc.yml v2 schema for your real production stacks

---

## Try it

```bash
git clone https://github.com/qosha1/remote-compose
cd remote-compose
pip install -e ".[ecs]"

# Production-style deploy
rc up --from-compose your-compose.yml

# Or: dev-host with claude inside
rc dev up alice --repo your/repo --compose your-compose.yml --skip-permissions
rc dev attach alice
```

Full setup, schema, feature index, and codebase map are in the [README](https://github.com/qosha1/remote-compose#readme). Architecture deep-dive in [ARCHITECTURE.md](https://github.com/qosha1/remote-compose/blob/main/ARCHITECTURE.md).

MIT-licensed. Issues, PRs, and "this would never work for my use case because X" feedback all welcome.

---

*If you spin up a dev box, pop in with `rc dev attach`, and ask the in-box Claude to extend `remote-compose` itself, please tell me how that goes — the recursive case is the one I keep thinking about.*
