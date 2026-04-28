# Production v1 → v2 Migration Runbook (boto3-only cutover)

**Target stack:** `ss-debuggai-prod` (us-west-2), 133 GB postgres on EFS, 7 ECS services, 32 SM secrets, customer-facing `api.startsimpli.com`.

**Window budget:** ~30 min cutover + 5 min canary verification. Rollback < 5 min via task-def revert. DNS unchanged (registrar-side record points at the existing ALB DNS, ALB is untouched).

**Approach:** v1 prod is deployed imperatively (boto3 calls, no terraform state). The migration is therefore an **ECS task-definition shape change**, not a terraform import. Every stateful resource (EFS, ALB, ACM cert, VPC, SM secrets, ECS cluster) stays exactly as-is. We only register new task-def revisions with v2 conventions (`secrets[]` referencing SM ARNs instead of v1's envfile injection) and rolling-update each service to use them.

## Why this is the safest path

| Resource | What we change | What changes in AWS |
|---|---|---|
| 133 GB postgres EFS volume | nothing | nothing — same `fileSystemId`, same access points |
| 32 SM secrets | nothing | nothing — same ARNs, same values |
| ALB + listeners + cert | nothing | nothing — same DNS, same SSL chain |
| VPC + subnets + SGs | nothing | nothing |
| ECS cluster | nothing | nothing — same name, same containerInsights setting |
| ECR repos + images | nothing | nothing — task defs reference same image URIs |
| ECS task definitions | new revisions registered | container `secrets[]` now ARN-by-key; env keys that collided with secrets dropped |
| ECS services | rolling restart | task definition pointer flips to new revision |

The data plane (EFS, SM, ALB, cert, DNS) is **byte-for-byte identical** before and after. The only thing that changes is each container's `secrets[]` block: v1 injected secret values into env at task-launch time via custom envfile machinery; v2 references SM ARNs natively so ECS handles the injection.

---

## T−2h: Pre-flight (no downtime, no AWS mutation)

```bash
# 1. Generate the migration plan against live AWS state.
cd ~/Repos/start-simpli/start-simpli-api
PYTHONPATH=~/Repos/devtools/remote-compose \
~/Repos/devtools/remote-compose/.venv/bin/rc v1 migrate plan ./rc.yml \
  --aws-profile debuggai \
  --out ./v2-migration

# 2. Review the artifacts.
cat ./v2-migration/MIGRATION_SUMMARY.md            # blast radius, imports, secrets, undo
cat ./v2-migration/rc.yml.v2                       # the new rc.yml (commit this later)

# 3. Verify safety properties.
grep -ic 'destroy\|delete' ./v2-migration/imports.tf  # MUST be 0
.venv/bin/python -c "
import json
plan = json.load(open('./v2-migration/runbook.json'))
print('phases:', [e['phase'] for e in plan])
"
```

If any check fails, **STOP** and inspect.

---

## T−24h: Write the canary row to live postgres

Before kicking off the migration, drop a known row into a new table on
the live postgres. After the cutover, the same SELECT must return the
same row — that's the GO/NO-GO bar.

```bash
# Connect via ECS exec to the live postgres task.
TASK_ARN=$(aws ecs list-tasks --cluster ss-debuggai-prod \
  --service-name ss-debuggai-postgres --profile debuggai --region us-west-2 \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster ss-debuggai-prod \
  --task "$TASK_ARN" --container postgres --interactive \
  --command "psql -U postgres -d <prod_db>" \
  --profile debuggai --region us-west-2

# In the psql session:
CREATE TABLE IF NOT EXISTS migration_canary (
  id INTEGER PRIMARY KEY,
  marker TEXT NOT NULL,
  written_at TIMESTAMP DEFAULT NOW()
);
INSERT INTO migration_canary (id, marker)
VALUES (1, 'pre-migration-2026-04-28')
ON CONFLICT (id) DO UPDATE SET marker = EXCLUDED.marker, written_at = NOW();
SELECT * FROM migration_canary;
\q
```

---

## T+0: Maintenance window opens

### Phase 1: Validate (read-only, ~30s)

```bash
rc v1 migrate apply ./rc.yml \
  --out ./v2-migration \
  --aws-profile debuggai \
  --phase validate \
  --auto-approve
```

ValidatePhase re-discovers live state and diffs against the plan
inventory. Drift here means a resource changed since T−2h plan; re-run
plan + investigate before continuing.

### Phase 2: Services cutover (~25 min — 7 services × ~3 min rolling)

This is the actual mutation. For each ECS service it:
1. Reads the current task def (`describe_task_definition`).
2. v2-shapes it: replaces `secrets[]` with ARN-by-key entries from
   `plan.secret_arn_map`; drops env entries that collide with secret keys.
3. Registers the new revision (`register_task_definition`).
4. Updates the service to point at the new revision (`update_service`),
   triggering a rolling deploy.

```bash
rc v1 migrate apply ./rc.yml \
  --out ./v2-migration \
  --aws-profile debuggai \
  --phase services_cutover
# Will prompt; review the plan before confirming.
```

Watch CloudWatch:

```bash
watch -n 5 'aws ecs describe-services --cluster ss-debuggai-prod \
  --services ss-debuggai-django ss-debuggai-postgres ss-debuggai-redis \
             ss-debuggai-nginx ss-debuggai-celery-worker \
             ss-debuggai-celery-beat ss-debuggai-celery-worker-linkedin \
  --profile debuggai --region us-west-2 \
  --query "services[*].{Name:serviceName,Running:runningCount,Desired:desiredCount}" \
  --output table'
```

Wait for all `Running` columns to hit `Desired`. Per-service health
endpoints:

```bash
curl -fsS https://api.startsimpli.com/api/health/   # MUST return 200
```

### Phase 3: GO/NO-GO — DB integrity canary (~2 min)

**Do not skip.** The cutover is reversible up to this point.

```bash
TASK_ARN=$(aws ecs list-tasks --cluster ss-debuggai-prod \
  --service-name ss-debuggai-postgres --profile debuggai --region us-west-2 \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster ss-debuggai-prod \
  --task "$TASK_ARN" --container postgres --interactive \
  --command "psql -U postgres -d <prod_db> -c \"SELECT id, marker FROM migration_canary WHERE id = 1;\"" \
  --profile debuggai --region us-west-2
```

The SELECT must return:
```
 id |          marker
----+-------------------------
  1 | pre-migration-2026-04-28
```

If the row is missing OR the marker doesn't match: **STOP**. Roll back
per below.

### Phase 4: Decommission v1 rc.yml (~5s)

```bash
rc v1 migrate apply ./rc.yml \
  --out ./v2-migration \
  --aws-profile debuggai \
  --phase decommission_v1 \
  --auto-approve
```

Archives the v1 `rc.yml` to `./v2-migration/archive/rc.yml.<timestamp>`.
Tripwired: never calls `delete_secret`, `delete_file_system`,
`delete_load_balancer`, or `delete_certificate`.

### Phase 5: Commit the v2 rc.yml

```bash
mv ./v2-migration/rc.yml.v2 ./rc.yml.v2
git add rc.yml.v2 ./v2-migration/MIGRATION_SUMMARY.md ./v2-migration/runbook.json
git commit -m "production cutover to rc v2 ($(date -u +%Y-%m-%d))"
git push
```

(Don't `mv rc.yml.v2 rc.yml` yet — keep both side-by-side until v2's
`rc deploy` path is wired up to handle imported stacks.)

---

## Rollback (< 5 min)

If Phase 3 (canary) fails OR any service in Phase 2 fails to reach
`Running == Desired`:

```bash
# Read the runbook.json to find the previous task-def ARN per service.
.venv/bin/python -c "
import json
rb = json.load(open('./v2-migration/runbook.json'))
cutover = next(e for e in rb if e['phase'] == 'services_cutover')
print(cutover['details'])  # contains 'name->family:rev' pairs
"

# For each service, revert to the v1 task definition (the one BEFORE the
# new revision we just registered). The previous revision number is
# (current_revision - 1).
for svc in ss-debuggai-django ss-debuggai-postgres ss-debuggai-redis \
           ss-debuggai-nginx ss-debuggai-celery-worker \
           ss-debuggai-celery-beat ss-debuggai-celery-worker-linkedin; do
  CURRENT=$(aws ecs describe-services --cluster ss-debuggai-prod \
    --services $svc --profile debuggai --region us-west-2 \
    --query 'services[0].taskDefinition' --output text)
  FAMILY=$(echo "$CURRENT" | rev | cut -d/ -f1 | rev | cut -d: -f1)
  CURR_REV=$(echo "$CURRENT" | rev | cut -d: -f1 | rev)
  PREV_REV=$((CURR_REV - 1))
  aws ecs update-service --cluster ss-debuggai-prod --service $svc \
    --task-definition "${FAMILY}:${PREV_REV}" \
    --profile debuggai --region us-west-2 &
done
wait

# Verify.
curl -fsS https://api.startsimpli.com/api/health/   # MUST return 200
```

DNS unchanged. ALB unchanged. EFS untouched. SM secrets untouched. The
rollback is just an ECS service update flipping the task-def pointer
back.

---

## Why we're NOT running terraform import today

The original lifecycle plan included a `terraform import` phase to
absorb the existing AWS resources into v2's terraform state. That phase
is now opt-in (`--phase import_state`) and **deferred**:

1. **v1 prod has no terraform state.** It was deployed imperatively.
   There's nothing to import *into* a state file that doesn't exist.
2. **v2 ECSProvider's terraform templates are designed for greenfield
   deployments.** They emit `aws_subnet.public[count.index]`,
   `aws_security_group.alb` (named role-based), `aws_acm_certificate.main`
   (with cert-validation chain). Our prod has 5 SGs, 2 subnets without
   public/private classification, an already-issued ACM cert with
   externally-managed DNS validation. Importing those into v2's emitted
   addresses would either fail or cause terraform to propose recreating
   them.
3. **Adding "BYO existing resources" support to v2 is a separate
   feature** (~1-2 days work). Tracked as a follow-up: extend rc.yml
   schema with `provider_config.ecs.existing.{vpc_id, subnets,
   security_groups, alb_arn, certificate_arn, efs_file_system_ids}` and
   make the v2 emitter emit `data` blocks instead of `resource` blocks
   when those are set.

The boto3-only cutover gets prod onto v2's task-def conventions
**today**. Future `rc deploy` against the migrated stack will need the
BYO-existing feature; for now, the stack stays manageable via the v1
imperative tooling (which still works for `rc destroy`, manual
redeploys, etc.) until the BYO feature ships.
