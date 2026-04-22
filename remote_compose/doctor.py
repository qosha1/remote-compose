"""Dependency preflight + optional auto-fix for remote-compose.

`rc doctor` walks the prereq list, reports what's OK/missing/stale, and (with
--fix) attempts repair via the platform's native package manager. Keeps the
first-run story turnkey: users shouldn't have to hand-install terraform,
docker, boto3 extras, etc.

Checks:
    terraform >= 1.5       hard requirement for every provider
    docker                 required if any service has build: stanzas
    python   >= 3.9        required by the package itself
    boto3                  optional (required if provider is ecs)
    kubernetes             optional (required if provider is k8s)
    aws creds              optional (checked against sts:GetCallerIdentity
                            when profile/env vars are present)

Supported fix backends: macOS (Homebrew), Debian/Ubuntu (apt), Fedora (dnf).
Other platforms: report-only with manual-install instructions.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


REQUIRED_TERRAFORM = (1, 5)
REQUIRED_PYTHON = (3, 9)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix_hint: Optional[str] = None
    fixer: Optional[Callable[[], tuple[bool, str]]] = field(default=None, repr=False)

    @property
    def status(self) -> str:
        return "OK" if self.ok else "MISSING"


def _detect_pkg_manager() -> Optional[str]:
    system = platform.system()
    if system == "Darwin" and shutil.which("brew"):
        return "brew"
    if system == "Linux":
        if shutil.which("apt-get"):
            return "apt"
        if shutil.which("dnf"):
            return "dnf"
    return None


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 255, "", str(exc)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python() -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) >= REQUIRED_PYTHON
    return CheckResult(
        name="python",
        ok=ok,
        detail=f"{v.major}.{v.minor}.{v.micro}",
        fix_hint=None if ok else (
            f"upgrade to Python >= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} "
            "via pyenv / official installer"
        ),
    )


def check_terraform() -> CheckResult:
    tf = shutil.which("terraform")
    if not tf:
        return CheckResult(
            name="terraform",
            ok=False,
            detail="not installed",
            fix_hint="install via `rc doctor --fix` or Homebrew/apt/dnf",
            fixer=_fix_terraform,
        )
    rc, out, err = _run([tf, "-version"], timeout=15)
    if rc != 0:
        return CheckResult(
            name="terraform",
            ok=False,
            detail=f"binary at {tf} failed to execute (exit {rc}); likely stale / killed by Gatekeeper",
            fix_hint="`rc doctor --fix` — upgrades via package manager",
            fixer=_fix_terraform,
        )
    m = re.search(r"Terraform v(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return CheckResult(
            name="terraform",
            ok=False,
            detail=f"could not parse version from output: {out.strip()[:80]}",
            fix_hint="`rc doctor --fix` to reinstall",
            fixer=_fix_terraform,
        )
    major, minor = int(m.group(1)), int(m.group(2))
    version = f"{major}.{minor}.{m.group(3)}"
    if (major, minor) < REQUIRED_TERRAFORM:
        return CheckResult(
            name="terraform",
            ok=False,
            detail=f"{version} at {tf} — need >= {REQUIRED_TERRAFORM[0]}.{REQUIRED_TERRAFORM[1]}",
            fix_hint="`rc doctor --fix` to upgrade",
            fixer=_fix_terraform,
        )
    return CheckResult(name="terraform", ok=True, detail=f"{version} at {tf}")


def check_docker() -> CheckResult:
    docker = shutil.which("docker")
    if not docker:
        return CheckResult(
            name="docker",
            ok=False,
            detail="not installed (only required for `rc deploy` with build: services)",
            fix_hint="install Docker Desktop (macOS/Windows) or `rc doctor --fix`",
            fixer=_fix_docker,
        )
    rc, out, _ = _run([docker, "version", "--format", "{{.Client.Version}}"], timeout=10)
    if rc != 0:
        return CheckResult(
            name="docker",
            ok=False,
            detail="binary present but `docker version` failed (daemon not running?)",
            fix_hint="start Docker Desktop / systemctl start docker",
        )
    return CheckResult(name="docker", ok=True, detail=f"{out.strip()} at {docker}")


def check_boto3() -> CheckResult:
    return _check_python_module("boto3", extra="ecs")


def check_kubernetes_client() -> CheckResult:
    return _check_python_module("kubernetes", extra="k8s")


def check_aws_creds() -> CheckResult:
    try:
        import boto3
    except ImportError:
        return CheckResult(
            name="aws creds",
            ok=False,
            detail="boto3 not installed — cannot check",
            fix_hint="`pip install -e '.[ecs]'`",
        )
    profile = os.environ.get("AWS_PROFILE")
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        who = f"account={identity['Account']} arn={identity['Arn'].split('/')[-1]}"
        return CheckResult(name="aws creds", ok=True, detail=who)
    except Exception as exc:
        return CheckResult(
            name="aws creds",
            ok=False,
            detail=f"sts:GetCallerIdentity failed: {type(exc).__name__}",
            fix_hint="set AWS_PROFILE or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY",
        )


def _check_python_module(module: str, extra: str) -> CheckResult:
    try:
        mod = importlib.import_module(module)
        version = getattr(mod, "__version__", "unknown")
        return CheckResult(name=module, ok=True, detail=f"version {version}")
    except ImportError:
        return CheckResult(
            name=module,
            ok=False,
            detail="not installed",
            fix_hint=f"pip install -e '.[{extra}]'",
        )


# ---------------------------------------------------------------------------
# Fixers (package-manager calls)
# ---------------------------------------------------------------------------


def _fix_terraform() -> tuple[bool, str]:
    pm = _detect_pkg_manager()
    if pm == "brew":
        if shutil.which("terraform"):
            rc, out, err = _run(["brew", "upgrade", "terraform"], timeout=600)
            if rc == 0:
                return True, "brew upgrade terraform"
            # brew upgrade fails if it's up-to-date but linked wrong; try relink
            if "already installed" in err or "already installed" in out:
                _run(["brew", "unlink", "terraform"])
                rc2, _, _ = _run(["brew", "link", "terraform"])
                if rc2 == 0:
                    return True, "brew relink terraform"
            # Fallback: tap the official HashiCorp tap + install
            _run(["brew", "tap", "hashicorp/tap"])
            rc3, _, err3 = _run(["brew", "install", "hashicorp/tap/terraform"], timeout=600)
            return rc3 == 0, err3 or "brew install hashicorp/tap/terraform"
        rc, _, err = _run(["brew", "install", "terraform"], timeout=600)
        return rc == 0, err or "brew install terraform"
    if pm == "apt":
        cmds = [
            ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "gnupg", "software-properties-common", "curl"],
        ]
        for cmd in cmds:
            rc, _, err = _run(cmd, timeout=300)
            if rc != 0:
                return False, err or f"failed: {' '.join(cmd)}"
        # HashiCorp apt repo
        rc, out, err = _run(
            ["bash", "-c",
             "curl -fsSL https://apt.releases.hashicorp.com/gpg | "
             "sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg && "
             "echo \"deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] "
             "https://apt.releases.hashicorp.com $(lsb_release -cs) main\" | "
             "sudo tee /etc/apt/sources.list.d/hashicorp.list && "
             "sudo apt-get update && sudo apt-get install -y terraform"],
            timeout=600,
        )
        return rc == 0, err or "apt install terraform"
    if pm == "dnf":
        rc, _, err = _run(
            ["sudo", "dnf", "config-manager", "--add-repo",
             "https://rpm.releases.hashicorp.com/fedora/hashicorp.repo"],
            timeout=120,
        )
        if rc != 0:
            return False, err or "dnf config-manager failed"
        rc, _, err = _run(["sudo", "dnf", "-y", "install", "terraform"], timeout=600)
        return rc == 0, err or "dnf install terraform"
    return False, (
        f"no supported package manager on {platform.system()}. "
        "Install terraform from https://developer.hashicorp.com/terraform/install"
    )


def _fix_docker() -> tuple[bool, str]:
    pm = _detect_pkg_manager()
    if pm == "brew":
        rc, _, err = _run(["brew", "install", "--cask", "docker"], timeout=600)
        return rc == 0, err or "brew install --cask docker (start Docker Desktop after)"
    return False, "install Docker Desktop manually from https://docker.com"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


DEFAULT_CHECKS: list[Callable[[], CheckResult]] = [
    check_python,
    check_terraform,
    check_docker,
    check_boto3,
    check_aws_creds,
]


@dataclass
class DoctorReport:
    results: list[CheckResult]

    @property
    def ok(self) -> bool:
        # docker, boto3, aws-creds are soft requirements — provider choice
        # decides whether they're strictly needed. terraform + python are hard.
        hard_required = {"terraform", "python"}
        return all(r.ok for r in self.results if r.name in hard_required)

    def render_table(self) -> str:
        col = max(len(r.name) for r in self.results)
        lines = [f"  {'check'.ljust(col)}  status    detail"]
        lines.append(f"  {'-' * col}  --------  {'-' * 48}")
        for r in self.results:
            flag = "✓ OK   " if r.ok else "✗ FAIL "
            lines.append(f"  {r.name.ljust(col)}  {flag}  {r.detail}")
        hints = [r for r in self.results if not r.ok and r.fix_hint]
        if hints:
            lines.append("")
            lines.append("  Fix hints:")
            for r in hints:
                lines.append(f"    {r.name}: {r.fix_hint}")
        return "\n".join(lines)


def run(checks: Optional[list[Callable[[], CheckResult]]] = None) -> DoctorReport:
    checks = checks or DEFAULT_CHECKS
    return DoctorReport(results=[c() for c in checks])


def apply_fixes(report: DoctorReport) -> list[tuple[str, bool, str]]:
    outcomes: list[tuple[str, bool, str]] = []
    for r in report.results:
        if r.ok or r.fixer is None:
            continue
        ok, detail = r.fixer()
        outcomes.append((r.name, ok, detail))
    return outcomes
