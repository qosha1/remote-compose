"""Integration tests that invoke real terraform against a minimal HCL fixture.

These tests are the truth gate for the TerraformRunner wrapper: if they pass,
any provider built on top of the runner inherits a working subprocess path.

Skipped automatically when the ``terraform`` binary is not installed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from remote_compose.terraform.runner import TerraformRunner, TerraformError


pytestmark = pytest.mark.integration


def _terraform_available() -> bool:
    return shutil.which("terraform") is not None


def _terraform_usable() -> bool:
    """Sentinel: verify `terraform -version` completes.

    Skips cleanly on boxes with a missing or broken terraform binary
    (e.g. stale brew symlink killed by Gatekeeper). `rc doctor` diagnoses
    and `rc doctor --fix` repairs.
    """
    if not _terraform_available():
        return False
    try:
        result = subprocess.run(
            ["terraform", "-version"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_terraform = pytest.mark.skipif(
    not _terraform_usable(),
    reason="terraform binary not usable in this environment (binary missing or sandboxed)",
)


@pytest.fixture
def hcl_valid(tmp_path):
    """A minimal provider-less terraform module that validates cleanly."""
    (tmp_path / "main.tf").write_text(
        'terraform {\n'
        '  required_version = ">= 1.0"\n'
        '}\n'
        '\n'
        'variable "name" {\n'
        '  type    = string\n'
        '  default = "test"\n'
        '}\n'
        '\n'
        'output "name" {\n'
        '  value = var.name\n'
        '}\n'
    )
    return tmp_path


@pytest.fixture
def hcl_invalid(tmp_path):
    """HCL that parses but fails validation."""
    (tmp_path / "main.tf").write_text(
        'variable "bad" {\n'
        '  type = some_nonexistent_type\n'
        '}\n'
    )
    return tmp_path


@requires_terraform
class TestTerraformRunnerAgainstRealBinary:
    def test_init_and_validate_succeeds_on_valid_module(self, hcl_valid):
        runner = TerraformRunner(hcl_valid)
        runner.init(backend=False)
        runner.validate()

    def test_validate_raises_on_bad_module(self, hcl_invalid):
        """Terraform 1.14+ catches unknown types at init; earlier versions
        only flag at validate. Accept either as long as bad HCL raises."""
        runner = TerraformRunner(hcl_invalid)
        with pytest.raises(TerraformError):
            runner.init(backend=False)
            runner.validate()

    def test_plan_returns_summary(self, hcl_valid):
        runner = TerraformRunner(hcl_valid)
        runner.init(backend=False)
        summary = runner.plan()
        assert summary.create >= 0
        assert summary.update >= 0
        assert summary.destroy >= 0

    def test_progress_callback_receives_output(self, hcl_valid):
        events: list[str] = []
        runner = TerraformRunner(hcl_valid, progress=events.append)
        runner.init(backend=False)
        assert any("terraform init" in e or "Initializing" in e for e in events), (
            f"progress callback received no init-related lines: {events[:5]}"
        )


@pytest.fixture
def hcl_with_terraform_data(tmp_path):
    """Module with one terraform_data resource — importable without cloud creds.

    Used to exercise import_resource() against the real terraform binary
    (the orphan-log-group flow in ECSProvider relies on it). terraform_data
    is a built-in resource (Terraform 1.4+) that just stores arbitrary
    input — perfect for this since no provider plugins or AWS auth needed.
    """
    (tmp_path / "main.tf").write_text(
        'terraform {\n'
        '  required_version = ">= 1.4"\n'
        '}\n'
        '\n'
        'resource "terraform_data" "thing" {\n'
        '  input = "hello"\n'
        '}\n'
    )
    return tmp_path


@requires_terraform
class TestImportResourceAgainstRealBinary:
    """The truth gate for ECSProvider._reconcile_orphan_log_groups.

    The provider's swallow pattern matches "already managed" in lowercased
    stderr. These tests prove that string actually appears in real
    terraform's output for the duplicate-import case.
    """

    def test_import_already_managed_raises_with_swallow_pattern_in_stderr(
        self, hcl_with_terraform_data,
    ):
        runner = TerraformRunner(hcl_with_terraform_data)
        runner.init(backend=False)
        # Apply puts terraform_data.thing in state.
        runner._run(["apply", "-input=false", "-auto-approve", "-no-color"])
        # Importing the same address now MUST fail with "already managed".
        with pytest.raises(TerraformError) as exc_info:
            runner.import_resource("terraform_data.thing", "fake-id")
        combined = (
            (exc_info.value.stderr or "") + (exc_info.value.stdout or "")
        ).lower()
        assert "already managed" in combined, (
            f"Real terraform's duplicate-import stderr no longer contains "
            f"'already managed' — ECSProvider._reconcile_orphan_log_groups "
            f"swallow pattern needs updating. Got:\n{combined[:1000]}"
        )

    def test_import_resource_calls_terraform_import_subcommand(
        self, hcl_with_terraform_data,
    ):
        """Sanity: import_resource() actually runs `terraform import` (not e.g.
        `terraform state push`). Captures argv via the progress callback."""
        events: list[str] = []
        runner = TerraformRunner(hcl_with_terraform_data, progress=events.append)
        runner.init(backend=False)
        try:
            runner.import_resource("terraform_data.thing", "fake-id")
        except TerraformError:
            pass  # We don't care if import succeeds — only that it ran.
        assert any("terraform import" in e for e in events), (
            f"progress callback never saw 'terraform import' line: "
            f"{events[:10]}"
        )
