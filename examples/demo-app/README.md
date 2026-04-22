# Demo: URL shortener

A small but real multi-service stack that exercises every feature of the
ECS provider in one deploy:

- **Fargate + EC2 mixed-mode** — `api` and `db` run on Fargate; `worker`
  runs on EC2 with SPOT capacity (cost demo)
- **EFS persistent volume** — Postgres data survives task replacement
- **Cloud Map service discovery** — services reach each other by compose
  name (`db`, `cache`) inside the VPC, no ALB for private traffic
- **Public ALB** — `api` is the only public-facing service
- **Secrets** — `APP_SECRET_KEY` is uploaded via `rc secrets push`, never
  lives in terraform state

What it does: shorten URLs, serve redirects, count clicks asynchronously.

```
          Internet
              │
              ▼
       ┌──────────────┐   ┌──────────────┐
       │  ALB :80     │──▶│  api (Fargate)│──┬──▶ db.demo-app.local
       └──────────────┘   │   FastAPI    │  │    (postgres, EFS volume)
                          └──────────────┘  └──▶ cache.demo-app.local
                                                (redis)
                                                     ▲
                                                     │ BLPOP clicks
                          ┌─────────────────┐        │
                          │  worker (EC2)   │────────┘
                          │   polls + bump  │───▶ db.demo-app.local
                          └─────────────────┘
```

## Run it locally (no AWS)

```bash
cp .env.example .env
docker compose up --build
curl -sSf -X POST -H 'Content-Type: application/json' \
     -d '{"url": "https://github.com"}' \
     http://localhost:8080/shorten
# {"code": "a1b2c3d4", "short_url": "http://localhost:8080/a1b2c3d4"}

curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:8080/a1b2c3d4  # 302
curl -sSf http://localhost:8080/stats/a1b2c3d4
# {"code":"a1b2c3d4","url":"https://github.com","clicks":1,"created_at":"..."}
```

Or run the scripted smoke test:

```bash
./scripts/smoke.sh local
```

## Deploy to AWS

Prereqs — one-time:

```bash
rc doctor --fix                 # installs/upgrades terraform via brew/apt/dnf
export AWS_PROFILE=<profile>    # must be able to create resources in us-east-1
```

Deploy:

```bash
# From inside examples/demo-app
rc -c rc.yml deploy             # emits terraform, applies, builds+pushes images
```

Hit it:

```bash
# rc status shows the ALB DNS under terraform outputs
ALB=$(cd terraform/ecs && terraform output -raw alb_dns_name)
curl -sSf "http://$ALB/health"
curl -sSf -H 'Content-Type: application/json' \
     -d '{"url": "https://remote-compose.dev"}' \
     "http://$ALB/shorten"
```

Watch the worker pick up clicks:

```bash
rc -c rc.yml logs worker --lines 50
```

Tear down:

```bash
rc -c rc.yml destroy
# Belt-and-suspenders: force-clean anything terraform missed
python3 $(git rev-parse --show-toplevel)/scripts/reap_test_region.py
```

## What to poke at

- **Scale up** — edit `services.api.replicas: 3` in `rc.yml`, `rc deploy`.
  The ALB target group picks up the new tasks automatically.
- **Break the worker** — EC2 launch type means the worker runs on an
  Auto Scaling Group. Set `ec2_capacity.desired: 0` and the worker stops
  consuming clicks; click count stops incrementing. Restore to 1 and
  it resumes.
- **Swap to Fargate SPOT for the worker** — change `launch_type: EC2` →
  `FARGATE` + add `launch_type_capacity_provider: FARGATE_SPOT` (not yet
  in rc.yml schema — filed under rc-e5u.*). Cheaper for fault-tolerant
  workloads.
- **Turn on a custom domain** — uncomment a `domain:` block in `rc.yml`,
  make sure the parent Route 53 zone exists, `rc deploy`. Provider will
  request an ACM cert, DNS-validate, wire a 443 listener, redirect 80.

## Files

| Path | What |
|---|---|
| `api/main.py` | FastAPI URL shortener |
| `api/Dockerfile` | Slim Python 3.12 image, uvicorn, healthcheck |
| `worker/worker.py` | Redis BLPOP consumer, bumps click counts |
| `worker/Dockerfile` | Even slimmer worker image |
| `docker-compose.yml` | Source of truth for services — works locally AND as rc deploy input |
| `rc.yml` | v2 deployment config — provider: ecs, mixed Fargate + EC2, EFS, secrets |
| `scripts/smoke.sh` | `local` (docker compose) or `cloud` (real AWS) end-to-end |

## Cost

A full deploy of this stack runs about **$0.10 - $0.20 per hour**:
ALB (~$0.023/hr) + 3 Fargate tasks (~$0.06/hr combined) + 1 t3.medium
EC2 SPOT (~$0.015/hr) + EFS (basically free at this size). The smoke
test provisions, exercises, destroys in ~10-15 minutes for less than $0.10.

Leave `rc destroy` running after every experiment, and keep
`scripts/reap_test_region.py` handy in case terraform state goes
sideways.
