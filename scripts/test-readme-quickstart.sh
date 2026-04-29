#!/usr/bin/env bash
#
# test-readme-quickstart.sh — audit the README's quick-start against reality.
#
# Verifies that every command documented in README.md "Quick start" section
# actually exists in `rc --help` output, and that `rc init` produces a v2
# rc.yml as the README claims. No AWS calls — purely structural checks.
#
# Closes the loop on rc-e5u.44.5 (README claim audit).
#
# Usage:
#   bash scripts/test-readme-quickstart.sh
#
# Exit codes:
#   0 — every documented command resolves and behaves as the README says
#   1 — at least one claim is broken; details printed to stderr
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Use the in-repo .venv if present, else fall back to whatever rc is on PATH.
# Resolve to an absolute path before any cd so subshells in /tmp can still find it.
if [[ -x "$REPO_ROOT/.venv/bin/rc" ]]; then
    RC="$REPO_ROOT/.venv/bin/rc"
else
    RC="$(command -v rc || true)"
    if [[ -z "$RC" ]]; then
        echo "FAIL: rc binary not found. Run scripts/bootstrap-from-zero.sh first." >&2
        exit 1
    fi
fi

failures=0
checks=0

ok()    { echo "  ok    $*"; }
fail()  { echo "  FAIL  $*" >&2; failures=$((failures+1)); }
check() { checks=$((checks+1)); }

# ---------------------------------------------------------------------------
# Commands the README claims exist
# ---------------------------------------------------------------------------

help_out="$($RC --help 2>&1)"

for cmd in init up plan deploy secrets status exec destroy lifecycle db doctor; do
    check
    if grep -qE "^[[:space:]]+${cmd}\b" <<<"$help_out"; then
        ok "rc ${cmd} listed in --help"
    else
        fail "rc ${cmd} is in README quick-start but not in 'rc --help' output"
    fi
done

# `rc lifecycle <hook>` form — confirm lifecycle accepts a positional arg
check
if "$RC" lifecycle --help 2>&1 | grep -qE "Usage:.*lifecycle"; then
    ok "rc lifecycle subcommand exists"
else
    fail "rc lifecycle help failed"
fi

# `rc db push <file>` form — confirm db has a 'push' subcommand
check
if "$RC" db --help 2>&1 | grep -qE "^[[:space:]]+push\b"; then
    ok "rc db push subcommand exists"
else
    fail "rc db push subcommand missing"
fi

# `rc secrets push` form
check
if "$RC" secrets --help 2>&1 | grep -qE "^[[:space:]]+push\b"; then
    ok "rc secrets push subcommand exists"
else
    fail "rc secrets push subcommand missing"
fi

# ---------------------------------------------------------------------------
# rc init defaults to v2 schema (per README quick-start, .44.3)
# ---------------------------------------------------------------------------

check
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
(
    cd "$tmp_dir"
    "$RC" init >/dev/null 2>&1
    if grep -qE "^version:[[:space:]]*2" rc.yml; then
        echo "  ok    rc init writes v2 schema by default (version: 2)"
    else
        echo "  FAIL  rc init did not produce 'version: 2' (top of file: $(head -10 rc.yml | tr '\n' ' '))" >&2
        exit 1
    fi
)

# ---------------------------------------------------------------------------
# rc init --from-compose accepts a path arg (per README quick-start, .44.10)
# ---------------------------------------------------------------------------

check
if "$RC" init --help 2>&1 | grep -q -- "--from-compose"; then
    ok "rc init --from-compose flag documented"
else
    fail "rc init --from-compose flag not in --help"
fi

# ---------------------------------------------------------------------------
# rc up exists and accepts --from-compose (per README quick-start, .44.11)
# ---------------------------------------------------------------------------

check
if "$RC" up --help 2>&1 | grep -q -- "--from-compose"; then
    ok "rc up --from-compose flag documented"
else
    fail "rc up --from-compose flag not in --help"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
if [[ $failures -eq 0 ]]; then
    echo "OK: $checks/$checks README quick-start claims verified."
    exit 0
fi

echo "FAIL: $failures/$checks claims broken." >&2
exit 1
