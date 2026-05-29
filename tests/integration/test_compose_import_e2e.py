"""End-to-end integration: `rc compose import` output passes terraform validate.

Closes the verification gap on rc-e5u.41.3. The unit test for
scaffold_rc_yml only proves the output PARSES through the v2 schema. This
test proves the scaffolded rc.yml + the original compose file together
produce a deploy context the ECSProvider can emit valid HCL from.

Path exercised: scaffold_rc_yml → load_rc_yml → build_deploy_context →
ECSProvider.emit_terraform → real `terraform init -backend=false` →
`terraform validate`.

Skipped when terraform is not usable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
from remote_compose.compose_import import scaffold_rc_yml
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import TerraformRunner

pytestmark = pytest.mark.integration


def _terraform_usable() -> bool:
    if not shutil.which("terraform"):
        return False
    try:
        result = subprocess.run(
            ["terraform", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_terraform = pytest.mark.skipif(
    not _terraform_usable(),
    reason="terraform binary not usable in this environment",
)


_FIXTURES_DIR = Path(__file__).parent.parent / "compose_samples"


@requires_terraform
class TestScaffoldedRcYmlValidatesAsTerraform:
    """For each shipped compose fixture, the scaffolded rc.yml must round-trip
    through the full plan path and emit terraform that `terraform validate`
    accepts."""

    @pytest.mark.parametrize(
        "fixture_name",
        ["minimal_3_service.yml", "with_volumes.yml", "with_secrets.yml"],
    )
    def test_scaffold_to_terraform_validate(self, fixture_name, tmp_path):
        # Stage the fixture next to where rc.yml will live.
        src_compose = _FIXTURES_DIR / fixture_name
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(src_compose.read_text())

        # 1. Scaffold rc.yml from the compose.
        rc_yml_text = scaffold_rc_yml(compose_path, project="itest")
        rc_yml_path = tmp_path / "rc.yml"
        rc_yml_path.write_text(rc_yml_text)

        # 2. Load it back through the production loader.
        version, raw, v2 = load_rc_yml(rc_yml_path)
        assert version == 2, "scaffold must emit version: 2"
        assert v2 is not None
        assert v2.project == "itest"

        # 3. Build the deploy context — the same path `rc plan` walks.
        ctx = build_deploy_context(v2, raw, rc_yml_path)
        assert (
            ctx.services
        ), f"build_deploy_context produced no services for {fixture_name}"

        # 4. Emit terraform.
        out_dir = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out_dir)

        # 5. Real terraform validate.
        runner = TerraformRunner(out_dir)
        runner.init(backend=False)
        runner.validate()

    def test_scaffold_then_cli_compose_import_then_validate(self, tmp_path):
        """Bonus: exercise the full CLI path (rc compose import) instead of
        calling scaffold_rc_yml directly. Catches CLI-layer regressions
        the unit tests miss."""
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text((_FIXTURES_DIR / "minimal_3_service.yml").read_text())

        # Run `python3 -m remote_compose.cli compose import` as a subprocess.
        rc_yml_path = tmp_path / "rc.yml"
        result = subprocess.run(
            [
                # rc-4e5: sys.executable so the test uses the same
                # interpreter pytest is running under (which has the
                # project deps installed). 'python3' fails on systems
                # where the system python3 lacks click/yaml.
                sys.executable,
                "-m",
                "remote_compose.cli",
                "compose",
                "import",
                "--from",
                str(compose_path),
                "--out",
                str(rc_yml_path),
                "--project",
                "itest",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"rc compose import failed:\nstdout:{result.stdout}\n"
            f"stderr:{result.stderr}"
        )
        assert rc_yml_path.exists()

        # Now plan-equivalent path on the produced file.
        version, raw, v2 = load_rc_yml(rc_yml_path)
        assert version == 2
        ctx = build_deploy_context(v2, raw, rc_yml_path)
        assert ctx.services

        out_dir = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out_dir)
        runner = TerraformRunner(out_dir)
        runner.init(backend=False)
        runner.validate()
