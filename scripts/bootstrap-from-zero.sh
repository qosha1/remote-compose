#!/usr/bin/env bash
#
# bootstrap-from-zero.sh — take a fresh-ish machine to a usable rc install.
#
# What it does (idempotent — safe to re-run):
#   1. Detects the OS and ensures a package manager is available
#      (Homebrew on macOS; apt-get or dnf on Linux).
#   2. Installs Terraform via the platform package manager IF it's missing.
#      `rc doctor --fix` already knows how to do this on brew/apt/dnf, so this
#      step is mostly belt-and-suspenders for environments where the user
#      hasn't installed the rc package yet (chicken-and-egg) or where they
#      want a self-contained pre-flight before the pip install.
#   3. Creates a Python venv in ./.venv (skipped if one already exists).
#   4. Installs the package in editable mode with the [ecs] extra.
#   5. Runs `rc doctor --fix` to repair anything still missing.
#   6. Runs `rc doctor` and exits non-zero unless every hard check is green.
#
# Usage:
#   bash scripts/bootstrap-from-zero.sh
#
# Requirements going in:
#   - python3 + pip on PATH (every modern OS image ships these)
#   - sudo if running on Linux and terraform isn't already installed
#     (the apt/dnf branches need root to add the HashiCorp repo)
#
# Verified on:
#   - macOS arm64 (Darwin 25.x, Homebrew 4.x) — local re-run smoke test
#   - Linux apt/dnf branches: code-review only; full container clean-bootstrap
#     test is a follow-up bead (see rc-e5u.44.4 close note).
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Pretty output (mirrors deploy_to_aws.py / deploy_to_ecs.py banner style).
# ---------------------------------------------------------------------------
banner() {
    echo ""
    echo "============================================================"
    echo "  $1"
    echo "============================================================"
}

step() {
    echo ""
    echo ">>> $1"
}

ok() {
    echo "    [ok] $1"
}

warn() {
    echo "    [warn] $1" >&2
}

die() {
    echo "    [fail] $1" >&2
    exit 1
}

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

banner "remote-compose bootstrap-from-zero (idempotent)"
echo "  project root: $PROJECT_ROOT"
echo "  uname:        $(uname -s) $(uname -m)"

# ---------------------------------------------------------------------------
# Step 1: detect OS + package manager.
# ---------------------------------------------------------------------------
step "Step 1/5 — detect platform"

OS="$(uname -s)"
PKG_MGR=""
case "$OS" in
    Darwin)
        if ! command -v brew >/dev/null 2>&1; then
            die "Homebrew not installed. Install from https://brew.sh first, then re-run."
        fi
        PKG_MGR="brew"
        ok "macOS detected; Homebrew at $(command -v brew)"
        ;;
    Linux)
        if command -v apt-get >/dev/null 2>&1; then
            PKG_MGR="apt"
            ok "Linux/apt detected"
        elif command -v dnf >/dev/null 2>&1; then
            PKG_MGR="dnf"
            ok "Linux/dnf detected"
        else
            die "No supported package manager (apt-get, dnf) found on this Linux."
        fi
        ;;
    *)
        die "Unsupported OS: $OS. Supported: Darwin (macOS), Linux."
        ;;
esac

# ---------------------------------------------------------------------------
# Step 2: install Terraform if missing.
#
# Note: `rc doctor --fix` does the same thing once rc is installed. We do it
# here too so a bare machine without rc has a one-shot path to terraform; on
# a machine where terraform is already on PATH this whole step is a no-op.
# ---------------------------------------------------------------------------
step "Step 2/5 — ensure terraform is installed"

install_terraform_brew() {
    brew tap hashicorp/tap >/dev/null 2>&1 || true
    brew install hashicorp/tap/terraform
}

install_terraform_apt() {
    sudo apt-get update -y
    sudo apt-get install -y gnupg software-properties-common curl lsb-release
    # HashiCorp apt repo (matches doctor.py)
    curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
        | sudo tee /etc/apt/sources.list.d/hashicorp.list >/dev/null
    sudo apt-get update -y
    sudo apt-get install -y terraform
}

install_terraform_dnf() {
    sudo dnf install -y dnf-plugins-core
    sudo dnf config-manager --add-repo https://rpm.releases.hashicorp.com/fedora/hashicorp.repo
    sudo dnf -y install terraform
}

if command -v terraform >/dev/null 2>&1; then
    ok "terraform already on PATH at $(command -v terraform) — skipping install"
else
    case "$PKG_MGR" in
        brew) install_terraform_brew ;;
        apt)  install_terraform_apt ;;
        dnf)  install_terraform_dnf ;;
    esac
    command -v terraform >/dev/null 2>&1 \
        || die "terraform install via $PKG_MGR did not put terraform on PATH"
    ok "terraform installed: $(terraform version | head -1)"
fi

# ---------------------------------------------------------------------------
# Step 3: ensure a Python venv exists.
# ---------------------------------------------------------------------------
step "Step 3/5 — ensure ./.venv exists"

if [ -d "$PROJECT_ROOT/.venv" ] && [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    ok ".venv already present at $PROJECT_ROOT/.venv"
else
    if ! command -v python3 >/dev/null 2>&1; then
        die "python3 not on PATH. Install Python 3.9+ and re-run."
    fi
    python3 -m venv "$PROJECT_ROOT/.venv"
    ok "created venv at $PROJECT_ROOT/.venv"
fi

# Use the venv python/pip explicitly — DO NOT activate, keeps script idempotent
# regardless of caller shell state.
VENV_PY="$PROJECT_ROOT/.venv/bin/python"
VENV_PIP="$PROJECT_ROOT/.venv/bin/pip"

# ---------------------------------------------------------------------------
# Step 4: pip install -e ".[ecs]"
#
# Always re-runs (pip is a no-op when nothing changed). The acceptance criteria
# from rc-e5u.44.4 explicitly bakes this command in.
# ---------------------------------------------------------------------------
step "Step 4/5 — pip install -e \".[ecs]\""

"$VENV_PIP" install --upgrade pip >/dev/null
"$VENV_PIP" install -e ".[ecs]"
ok "rc CLI available at $PROJECT_ROOT/.venv/bin/rc"

# ---------------------------------------------------------------------------
# Step 5: rc doctor --fix && rc doctor
# ---------------------------------------------------------------------------
step "Step 5/5 — rc doctor --fix && rc doctor"

VENV_RC="$PROJECT_ROOT/.venv/bin/rc"
[ -x "$VENV_RC" ] || die "rc CLI did not install to $VENV_RC"

# `rc doctor --fix` exits 0 if everything ends up green, non-zero otherwise.
# When everything is already green it just prints the table and exits 0.
"$VENV_RC" doctor --fix

# Final verification — bare `rc doctor`. This is what the acceptance criteria
# wants to see green at the end.
echo ""
echo "  Final verification:"
"$VENV_RC" doctor

banner "bootstrap complete — rc is ready to use"
echo "  Activate with:  source $PROJECT_ROOT/.venv/bin/activate"
echo "  Or invoke:      $VENV_RC <command>"
echo ""
