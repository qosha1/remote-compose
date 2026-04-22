#!/usr/bin/env bash
# End-to-end smoke test for the demo app.
#
# Modes:
#   scripts/smoke.sh local    — hit the local docker-compose stack (no AWS)
#   scripts/smoke.sh cloud    — deploy to AWS, hit, destroy. Requires AWS creds
#                                + RC_E2E=1. Will burn ~$0.10 of compute.
#
# Asserts the full path: /health 200, POST /shorten 200, GET /{code} 302,
# GET /stats/{code} shows clicks > 0 (proves the worker consumed the event).

set -euo pipefail

MODE="${1:-local}"
cd "$(dirname "$0")/.."

assert_200() {
    local url="$1"
    local tries="${2:-30}"
    for i in $(seq 1 "$tries"); do
        if curl -sSf -o /dev/null "$url"; then
            echo "  ✓ $url"
            return 0
        fi
        sleep 2
    done
    echo "  ✗ $url did not return 200 after $tries tries" >&2
    return 1
}

smoke_against() {
    local base="$1"
    echo "== Smoke test against $base =="

    echo "[1/5] /health"
    assert_200 "$base/health" 60

    echo "[2/5] POST /shorten"
    local resp
    resp=$(curl -sSf -H 'Content-Type: application/json' \
           -d '{"url": "https://example.com/demo-smoke"}' \
           "$base/shorten")
    echo "  response: $resp"
    local code
    code=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['code'])" "$resp")
    echo "  code: $code"

    echo "[3/5] GET /$code (expect 302)"
    local status
    status=$(curl -sS -o /dev/null -w '%{http_code}' "$base/$code")
    if [[ "$status" != "302" ]]; then
        echo "  ✗ expected 302 from /$code, got $status" >&2
        return 1
    fi
    echo "  ✓ 302"

    echo "[4/5] GET /$code twice more to generate click events"
    curl -sS -o /dev/null "$base/$code"
    curl -sS -o /dev/null "$base/$code"

    echo "[5/5] worker should have consumed events; poll /stats/$code"
    for i in $(seq 1 30); do
        local clicks
        clicks=$(curl -sSf "$base/stats/$code" | python3 -c "import json,sys;print(json.loads(sys.stdin.read())['clicks'])")
        if [[ "$clicks" -ge 1 ]]; then
            echo "  ✓ clicks=$clicks after $((i*2))s"
            return 0
        fi
        sleep 2
    done
    echo "  ✗ worker never bumped click counter" >&2
    return 1
}


if [[ "$MODE" == "local" ]]; then
    echo "== Starting docker compose =="
    docker compose up -d --build
    trap 'docker compose down -v' EXIT
    port="${HOST_PORT:-8088}"
    smoke_against "http://localhost:${port}"
elif [[ "$MODE" == "cloud" ]]; then
    : "${RC_E2E:?RC_E2E=1 must be set to run the cloud smoke}"

    # Isolate under a unique rc-test-* project so the reap script covers it.
    test_id=$(python3 -c "import uuid;print('rc-test-demo-'+uuid.uuid4().hex[:8])")
    smoke_cfg="$(pwd)/rc.smoke.yml"
    python3 -c "
import yaml
cfg = yaml.safe_load(open('rc.yml'))
cfg['project'] = '${test_id}'
cfg['provider_config']['ecs']['cluster'] = '${test_id}-cluster'
cfg['secrets'] = []  # rc secrets push would be a separate step; skip for smoke
# Worker forced to FARGATE until EC2 private-subnet routing (rc-e5u.25) lands
for svc in cfg.get('services', {}).values():
    if svc.get('launch_type') == 'EC2':
        svc['launch_type'] = 'FARGATE'
yaml.safe_dump(cfg, open('${smoke_cfg}', 'w'), sort_keys=False)
"
    echo "== Using isolated config: project=${test_id}, config=${smoke_cfg}"

    repo_root="$(git rev-parse --show-toplevel)"
    trap 'echo "== cleanup =="; python3 -m remote_compose.cli -c "'"${smoke_cfg}"'" destroy -y || true; python3 "'"${repo_root}"'/scripts/reap_test_region.py" || true; rm -f "'"${smoke_cfg}"'"' EXIT

    echo "== rc deploy =="
    python3 -m remote_compose.cli -c "${smoke_cfg}" deploy 2>&1 | tail -60

    echo "== Resolving ALB DNS from terraform outputs =="
    tf_dir="./terraform"
    alb=$(cd "$tf_dir" && terraform output -json | python3 -c "import json,sys;print(json.loads(sys.stdin.read())['alb_dns_name']['value'])")
    echo "  alb: $alb"

    smoke_against "http://$alb"
else
    echo "usage: $0 {local|cloud}" >&2
    exit 2
fi

echo
echo "=== smoke test passed ==="
