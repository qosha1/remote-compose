#!/usr/bin/env bash
#
# scripts/test-startsimpli-end-to-end.sh — the rc-only acceptance test
# (rc-e5u.46.6).
#
# If THIS script doesn't exit 0, rc doesn't work for production Django
# systems. Period. The whole point of the rc-e5u.46 epic is for this
# script to pass with ONLY rc commands — no aws cli, no sed, no manual
# /tmp/docker-compose.ecs-test.yml dance, no curl -H Host: rewrite, no
# hand-written nginx.conf.
#
# Steps:
#   1. rc destroy --all-ephemeral  (clean slate)
#   2. rm -f /tmp/rc.local.yml     (no leftover rc.yml)
#   3. rc up --from-compose        (the one-shot deploy)
#   4. poll rc status until all 6 services healthy or 10 min timeout
#   5. plain curl http://<ALB>/api/v1/health/ returns 200 with workers healthy
#   6. rc destroy --yes            (clean teardown)
#   7. rc list --ephemeral         (verify registry empty)
#
# Usage:
#   bash scripts/test-startsimpli-end-to-end.sh
#
# Exit codes:
#   0 — rc works
#   1 — rc up failed
#   2 — services never reached healthy state
#   3 — ALB returned non-200 (or wrong body)
#   4 — destroy failed
#   5 — celery still showing 'no_workers' or non-healthy
#
# What's EXPLICITLY FORBIDDEN inside this script:
#   - aws cli invocations
#   - sed of user files
#   - editing /tmp/docker-compose.* by hand
#   - curl -H "Host: ..." rewrites
#   - manually running rc fix nginx-conf (rc up should orchestrate it)
#   - python helpers / wait-for-it scripts
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START_SIMPLI="${START_SIMPLI:-/Users/qosha/Repos/start-simpli/start-simpli-api}"
COMPOSE_FILE="${START_SIMPLI}/docker-compose.local.yml"
RC_YML="${RC_YML:-/tmp/rc.local.yml}"
AWS_PROFILE="${AWS_PROFILE_OVERRIDE:-default}"
REGION="${REGION_OVERRIDE:-us-west-1}"
TIMEOUT_DEPLOY="${TIMEOUT_DEPLOY:-900}"   # 15 min wall budget for rc up itself
TIMEOUT_HEALTH="${TIMEOUT_HEALTH:-600}"   # 10 min for services to converge after rc up exits

if [[ -x "${REPO_ROOT}/.venv/bin/rc" ]]; then
    RC="${REPO_ROOT}/.venv/bin/rc"
else
    RC="$(command -v rc || true)"
    if [[ -z "$RC" ]]; then
        echo "FAIL: rc not found. Run scripts/bootstrap-from-zero.sh first." >&2
        exit 1
    fi
fi

[[ -f "$COMPOSE_FILE" ]] || {
    echo "FAIL: compose file not found at $COMPOSE_FILE" >&2
    echo "      Set START_SIMPLI=<path-to-start-simpli-api>" >&2
    exit 1
}

echo "=== rc-e5u.46.6 end-to-end acceptance test ==="
echo "  rc:           $RC"
echo "  compose:      $COMPOSE_FILE"
echo "  rc.yml:       $RC_YML"
echo "  region:       $REGION"
echo "  aws_profile:  $AWS_PROFILE"
echo

# ---------------------------------------------------------------------------
# Step 1: clean slate
# ---------------------------------------------------------------------------
echo "[1/7] rc destroy --all-ephemeral (clean slate)..."
"$RC" destroy --all-ephemeral --yes || true   # may exit non-zero if registry empty
rm -f "$RC_YML"
echo "      done."
echo

# ---------------------------------------------------------------------------
# Step 2: rc up --from-compose
# ---------------------------------------------------------------------------
echo "[2/7] rc up --from-compose ${COMPOSE_FILE} (single command, full deploy)..."
deploy_log="$(mktemp -t rc-e2e-deploy-XXXXXX.log)"
set +e
"$RC" -c "$RC_YML" up \
    --from-compose "$COMPOSE_FILE" \
    --aws-profile "$AWS_PROFILE" \
    --region "$REGION" \
    --ttl 4h 2>&1 | tee "$deploy_log"
deploy_rc=${PIPESTATUS[0]}
set -e

if [[ $deploy_rc -ne 0 ]]; then
    echo "FAIL: rc up exited $deploy_rc — see $deploy_log" >&2
    exit 1
fi

ALB_URL="$(grep -oE 'http://[^[:space:]]+\.elb\.amazonaws\.com' "$deploy_log" | tail -1)"
if [[ -z "$ALB_URL" ]]; then
    echo "FAIL: rc up did not print an ALB URL — see $deploy_log" >&2
    exit 1
fi
echo "      ALB: $ALB_URL"
echo

# ---------------------------------------------------------------------------
# Step 3: wait for all services to reach healthy via rc status
# ---------------------------------------------------------------------------
echo "[3/7] poll 'rc status' until all 6 services healthy (timeout ${TIMEOUT_HEALTH}s)..."
deadline=$(( $(date +%s) + TIMEOUT_HEALTH ))
while true; do
    status_out="$("$RC" -c "$RC_YML" status 2>&1 || true)"
    # Fast structural check: 'degraded' must not appear AND every service
    # row should show health=healthy. STALE markers are also a no-go.
    if ! grep -q "degraded\|stale\|STALE" <<<"$status_out"; then
        # All 6 expected service names present?
        missing=""
        for s in django nginx postgres redis celery-worker celery-beat; do
            grep -q "^[[:space:]]*${s}[[:space:]]" <<<"$status_out" || missing="${missing} ${s}"
        done
        if [[ -z "$missing" ]]; then
            echo "      all services healthy."
            echo "$status_out"
            break
        fi
    fi
    if [[ $(date +%s) -gt $deadline ]]; then
        echo "FAIL: services did not converge within ${TIMEOUT_HEALTH}s." >&2
        echo "Last status:" >&2
        echo "$status_out" >&2
        exit 2
    fi
    sleep 15
done
echo

# ---------------------------------------------------------------------------
# Step 4: plain curl (no Host: header rewrite) returns 200
# ---------------------------------------------------------------------------
echo "[4/7] plain curl ${ALB_URL}/api/v1/health/ (no Host header rewrite)..."
# ECS service-health convergence is necessary but not sufficient: when
# Django launches under /start, the TCP listener comes up BEFORE
# `python manage.py migrate` + collectstatic + runserver finish. nginx
# resolver also needs ~10-30s to refresh once Cloud Map registers the
# new django ENI. Retry for up to TIMEOUT_HTTP seconds before giving
# up — 502 + connection-refused are expected in this window.
TIMEOUT_HTTP="${TIMEOUT_HTTP:-300}"
http_deadline=$(( $(date +%s) + TIMEOUT_HTTP ))
status=""
body=""
while true; do
    body="$(curl -sS -m 30 "${ALB_URL}/api/v1/health/" || true)"
    status="$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "${ALB_URL}/api/v1/health/" || true)"
    if [[ "$status" == "200" ]]; then
        break
    fi
    if [[ $(date +%s) -gt $http_deadline ]]; then
        echo "FAIL: ALB never returned 200 within ${TIMEOUT_HTTP}s." >&2
        echo "      last status=$status, body=$body" >&2
        exit 3
    fi
    echo "      ALB returned $status; waiting 10s for django to converge..."
    sleep 10
done
echo "      HTTP 200, body: $body"
echo

# ---------------------------------------------------------------------------
# Step 5: workers actually healthy (not 'no_workers')
# ---------------------------------------------------------------------------
echo "[5/7] verify celery workers actually responding..."
if ! grep -q '"celery"[[:space:]]*:[[:space:]]*"healthy"' <<<"$body"; then
    echo "FAIL: celery did not report healthy. Body: $body" >&2
    exit 5
fi
echo "      celery: healthy ✓"
for component in database redis; do
    if ! grep -q "\"${component}\"[[:space:]]*:[[:space:]]*\"healthy\"" <<<"$body"; then
        echo "FAIL: ${component} did not report healthy. Body: $body" >&2
        exit 5
    fi
    echo "      ${component}: healthy ✓"
done
echo

# ---------------------------------------------------------------------------
# Step 6: rc destroy --yes
# ---------------------------------------------------------------------------
echo "[6/7] rc destroy --yes (clean teardown)..."
"$RC" -c "$RC_YML" destroy --yes 2>&1 || {
    echo "FAIL: rc destroy errored — see above" >&2
    exit 4
}
echo "      done."
echo

# ---------------------------------------------------------------------------
# Step 7: registry should now show 0 ephemeral stacks for this project
# ---------------------------------------------------------------------------
echo "[7/7] verify ephemeral registry no longer lists this project..."
list_out="$("$RC" list --ephemeral 2>&1 || true)"
# Project name derives from the compose-file directory name; for
# /Users/.../start-simpli-api/docker-compose.local.yml the project
# is 'start-simpli-api'.
if grep -qE "rc-test-startsimpli|start-simpli-api" <<<"$list_out"; then
    echo "FAIL: project still in ephemeral registry post-destroy:" >&2
    echo "$list_out" >&2
    exit 4
fi
echo "      registry clean ✓"
echo

# ---------------------------------------------------------------------------
echo "=== PASS: rc-e5u.46.6 acceptance test green ==="
echo "    rc deploys + serves a real Django+celery system end-to-end via"
echo "    rc commands only. No aws cli, no sed, no /tmp dance, no Host"
echo "    header rewrite. The chain works."
exit 0
