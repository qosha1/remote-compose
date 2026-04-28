# Production v1 → v2 Migration Runbook

**Target stack:** `ss-debuggai-prod` (us-west-2), 133 GB postgres on EFS, 7 ECS services, 32 SM secrets, customer-facing `api.startsimpli.com`.

**Window budget:** 1 hour. Rollback budget: < 10 minutes. DNS unchanged throughout (ALB import preserves the chain).

**Tooling:** `rc v1 migrate plan` + `rc v1 migrate apply` (this package).

---

## T−24h: Pre-flight (no downtime)

```bash
# Snapshot the live tfstate FIRST. Required for ImportStatePhase.
cd ~/Repos/start-simpli/start-simpli-api
cp terraform/<live>.tfstate /tmp/ss-debuggai-prod.tfstate.bak

# Run plan (read-only — no AWS mutation).
PYTHONPATH=~/Repos/devtools/remote-compose \
~/Repos/devtools/remote-compose/.venv/bin/rc v1 migrate plan \
  ./rc.yml \
  --aws-profile debuggai \
  --out ./v2-migration

# Review:
#   v2-migration/MIGRATION_SUMMARY.md      <-- read this end-to-end
#   v2-migration/imports.tf                <-- 23 imports, no destroys
#   v2-migration/rc.yml.v2                 <-- the new config
#   v2-migration/runbook.json              <-- 5 phases, undo per phase

# Verify safety properties at the shell:
grep -ic "destroy\|delete" v2-migration/imports.tf      # MUST be 0
grep -c "fsap-004097e867c7bb755" v2-migration/imports.tf  # MUST be 1 (live postgres)
```

If any of those don't match, **STOP**. Re-run with `--inventory-snapshot` against a fresh `aws ...` capture and diff.

---

## T−2h: Sandbox dry-run (no downtime)

```bash
# cp -r the live state. ImportStatePhase REFUSES to run without this.
cp /tmp/ss-debuggai-prod.tfstate.bak /tmp/ss-debuggai-prod.tfstate.sandbox

# Run apply against the sandbox copy. ValidatePhase + EmitV2 + ImportStatePhase
# will all run; ServicesCutover + Decommission are explicitly skipped via --phase.
rc v1 migrate apply ./rc.yml \
  --out ./v2-migration \
  --sandbox-tfstate /tmp/ss-debuggai-prod.tfstate.sandbox \
  --aws-profile debuggai \
  --phase validate
rc v1 migrate apply ./rc.yml \
  --out ./v2-migration \
  --sandbox-tfstate /tmp/ss-debuggai-prod.tfstate.sandbox \
  --aws-profile debuggai \
  --phase emit_v2_terraform
rc v1 migrate apply ./rc.yml \
  --out ./v2-migration \
  --sandbox-tfstate /tmp/ss-debuggai-prod.tfstate.sandbox \
  --aws-profile debuggai \
  --phase import_state
```

Each phase prints `[<name>] OK (<elapsed>s)` on success or `FAIL` + the
undo runbook on failure.

The destroy-line guard: `import_state` parses the `terraform plan`
output. If ANY line matches `- destroy` or `will be destroyed`, the
phase aborts with `ok=False` BEFORE running `terraform apply`.

If `import_state` returns `OK`, you have proven that running the same
sequence against the LIVE tfstate will not destroy anything. Diff the
sandbox state against the original to confirm only imports were
applied.

---

## T+0: Maintenance window opens — downtime starts

### Phase 1: Validate (read-only, ~5 min)

```bash
rc v1 migrate apply ./rc.yml --out ./v2-migration \
  --sandbox-tfstate /tmp/ss-debuggai-prod.tfstate.sandbox \
  --aws-profile debuggai --phase validate
```

ValidatePhase re-discovers live state and diffs against the plan
inventory. If anything has drifted since T−24h (new ECS service, new
EFS access point, etc.), it reports `ok=False`. Re-run plan from
scratch in that case.

### Phase 2: Emit v2 terraform (~1 min)

Already done at T−2h; re-run idempotently if needed. No AWS calls.

### Phase 3: Import state — atomic swap (~5 min)

```bash
# Atomic swap: backup + replace.
cp ./terraform/<live>.tfstate ./terraform/<live>.tfstate.pre-migration
cp /tmp/ss-debuggai-prod.tfstate.sandbox ./terraform/<live>.tfstate

# Confirm the swap.
diff /tmp/ss-debuggai-prod.tfstate.sandbox ./terraform/<live>.tfstate  # MUST be empty
```

This is the moment of mutation. Everything before this point is
idempotent.

### Phase 4: Services cutover (~35 min)

```bash
rc v1 migrate apply ./rc.yml --out ./v2-migration \
  --sandbox-tfstate ./terraform/<live>.tfstate \
  --aws-profile debuggai --phase services_cutover
```

Registers v2-shaped task definitions for all 7 ECS services and rolls
each one. Sequential, ~5 min each. Watch CloudWatch:

```bash
aws ecs describe-services --cluster ss-debuggai-prod \
  --services ss-debuggai-django ss-debuggai-postgres ss-debuggai-redis \
             ss-debuggai-nginx ss-debuggai-celery-worker \
             ss-debuggai-celery-beat ss-debuggai-celery-worker-linkedin \
  --profile debuggai --region us-west-2 \
  --query 'services[*].{Name:serviceName,Running:runningCount,Desired:desiredCount,Status:status}' \
  --output table
```

All `Running` columns must hit `desired` count before proceeding.

### Phase 5: GO/NO-GO — DB integrity canary (~2 min)

**This is the bar. Do not skip.**

```bash
# Connect to the post-migration postgres via ECS exec or via a bastion.
aws ecs execute-command --cluster ss-debuggai-prod \
  --task <postgres-task-arn> --container postgres --interactive \
  --command "psql -U postgres -d <prod_db> -c \
    'SELECT id, marker FROM migration_canary WHERE id = 1;'" \
  --profile debuggai --region us-west-2
```

You wrote the canary row at T−24h:
```sql
CREATE TABLE IF NOT EXISTS migration_canary (id INTEGER PRIMARY KEY, marker TEXT);
INSERT INTO migration_canary (id, marker) VALUES (1, 'pre-migration-<DATE>');
```

The SELECT must return that exact row. If it doesn't:
- **STOP**.
- Roll back per the runbook below.
- Do NOT continue to Phase 6.

### Phase 6: Decommission v1 (~1 min)

```bash
rc v1 migrate apply ./rc.yml --out ./v2-migration \
  --sandbox-tfstate ./terraform/<live>.tfstate \
  --aws-profile debuggai --phase decommission_v1
```

Archives v1 rc.yml under `./v2-migration/archive/rc.yml.<timestamp>`.
Tripwired: NEVER calls `delete_secret`, `delete_file_system`,
`delete_load_balancer`, or `delete_certificate`.

---

## Rollback (< 10 min)

If Phase 5 (canary) fails OR any phase reports `ok=False`:

```bash
# 1. Revert ECS services to v1 task definitions (parallel — fast).
for svc in ss-debuggai-django ss-debuggai-postgres ss-debuggai-redis \
           ss-debuggai-nginx ss-debuggai-celery-worker \
           ss-debuggai-celery-beat ss-debuggai-celery-worker-linkedin; do
  aws ecs update-service --cluster ss-debuggai-prod --service $svc \
    --task-definition <pre-migration-$svc-task-def-arn> \
    --profile debuggai --region us-west-2 &
done
wait

# 2. Revert tfstate (atomic).
cp ./terraform/<live>.tfstate.pre-migration ./terraform/<live>.tfstate

# 3. Verify.
aws ecs describe-services ... # confirm running counts at desired
curl -s https://api.startsimpli.com/api/health/ # MUST return 200
```

DNS unchanged throughout. ALB import preserves the chain. SM secrets
untouched (zero mutation by design).

---

## Post-migration

```bash
# Commit v2-migration/ artifacts to start-simpli-api repo for audit trail.
cp v2-migration/rc.yml.v2 ./rc.yml.v2
git add rc.yml.v2 v2-migration/MIGRATION_SUMMARY.md v2-migration/runbook.json
git commit -m "production cutover to rc v2 ($(date -u +%Y-%m-%d))"

# After 7 days of clean operation, archive the v1 tfstate backup.
mv /tmp/ss-debuggai-prod.tfstate.bak ./terraform/archive/
```
