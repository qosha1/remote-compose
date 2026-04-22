"""Subprocess wrapper around the ``terraform`` CLI.

Wrapped commands: init, validate, plan, apply, destroy, output.
All invocations stream stdout/stderr to an optional progress callback so
providers can preserve the step-by-step UX from the legacy pipeline (NFR-5).

Tests use ``RecordingTerraformRunner`` (below) to assert on the command
sequence without executing real terraform.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class TerraformError(RuntimeError):
    """Raised when a terraform subprocess exits non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        # Include full stderr — truncating lost too many AWS API error details.
        # If callers want it short, they can str(exc).split('\n')[0].
        combined = (stderr.strip() or stdout.strip())[:4000]
        super().__init__(
            f"terraform {' '.join(cmd[1:])} exited {returncode}:\n{combined}"
        )


@dataclass
class PlanSummary:
    create: int
    update: int
    destroy: int
    raw: str


_PLAN_SUMMARY_RE = re.compile(
    r"Plan:\s+(\d+)\s+to\s+add,\s+(\d+)\s+to\s+change,\s+(\d+)\s+to\s+destroy"
)


class TerraformRunner:
    """Execute terraform against a working directory.

    Parameters
    ----------
    working_dir:
        Directory containing the terraform module (.tf files).
    terraform_bin:
        Path to the terraform binary. Defaults to ``$PATH`` lookup.
    progress:
        Optional callback invoked with each stdout/stderr line as it arrives.
    env:
        Environment variables forwarded to subprocess (e.g. ``AWS_PROFILE``).
    """

    def __init__(
        self,
        working_dir: Path,
        terraform_bin: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.working_dir = Path(working_dir)
        self.terraform_bin = terraform_bin or shutil.which("terraform") or "terraform"
        self.progress = progress
        self.env = env

    # -----------------------------------------------------------------
    # Core commands
    # -----------------------------------------------------------------

    def init(self, backend: bool = True, upgrade: bool = False) -> None:
        args = ["init", "-input=false"]
        if not backend:
            args.append("-backend=false")
        if upgrade:
            args.append("-upgrade")
        self._run(args)

    def validate(self) -> None:
        self._run(["validate"])

    def plan(self, out_file: Optional[Path] = None) -> PlanSummary:
        args = ["plan", "-input=false", "-no-color"]
        if out_file:
            args += ["-out", str(out_file)]
        stdout = self._run(args)
        return _parse_plan_summary(stdout)

    def apply(self, plan_file: Optional[Path] = None, auto_approve: bool = True) -> None:
        args = ["apply", "-input=false", "-no-color"]
        if auto_approve and plan_file is None:
            args.append("-auto-approve")
        if plan_file:
            args.append(str(plan_file))
        self._run(args)

    def destroy(self, auto_approve: bool = True) -> None:
        args = ["destroy", "-input=false", "-no-color"]
        if auto_approve:
            args.append("-auto-approve")
        self._run(args)

    def output(self, name: Optional[str] = None) -> dict:
        """Return terraform outputs as a dict (parsed from ``terraform output -json``)."""
        args = ["output", "-json"]
        if name:
            args.append(name)
        stdout = self._run(args)
        return json.loads(stdout) if stdout.strip() else {}

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _run(self, args: list[str]) -> str:
        cmd = [self.terraform_bin] + args
        if self.progress:
            self.progress(f"$ terraform {' '.join(args)}")
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.working_dir),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        assert proc.stdout is not None and proc.stderr is not None
        for line in proc.stdout:
            stdout_lines.append(line)
            if self.progress:
                self.progress(line.rstrip())
        for line in proc.stderr:
            stderr_lines.append(line)
            if self.progress:
                self.progress(line.rstrip())
        proc.wait()

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        if proc.returncode != 0:
            raise TerraformError(cmd, proc.returncode, stdout, stderr)
        return stdout


def _parse_plan_summary(stdout: str) -> PlanSummary:
    """Parse `Plan: N to add, N to change, N to destroy` out of terraform plan output."""
    m = _PLAN_SUMMARY_RE.search(stdout)
    if m:
        return PlanSummary(
            create=int(m.group(1)),
            update=int(m.group(2)),
            destroy=int(m.group(3)),
            raw=stdout,
        )
    if "No changes" in stdout or "no changes" in stdout:
        return PlanSummary(create=0, update=0, destroy=0, raw=stdout)
    return PlanSummary(create=0, update=0, destroy=0, raw=stdout)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _Recorded:
    args: list[str]
    returncode: int = 0
    stdout: str = ""


class RecordingTerraformRunner(TerraformRunner):
    """A TerraformRunner that records invocations instead of executing them.

    Use in unit tests that need to assert on the sequence of terraform calls
    a provider makes without actually running terraform.
    """

    def __init__(self, working_dir: Path):
        super().__init__(working_dir=working_dir, terraform_bin="/dev/null")
        self.calls: list[_Recorded] = []
        self.scripted_outputs: dict[str, str] = {}

    def _run(self, args: list[str]) -> str:  # type: ignore[override]
        self.calls.append(_Recorded(args=list(args)))
        key = args[0] if args else ""
        return self.scripted_outputs.get(key, "")

    def script(self, command: str, stdout: str) -> None:
        """Pre-program stdout for a given terraform subcommand (e.g. 'plan')."""
        self.scripted_outputs[command] = stdout
