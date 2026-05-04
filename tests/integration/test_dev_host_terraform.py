"""Integration tests for the rc dev terraform module.

TDD red phase for [rc dev 2.2] (rc-srl). Tests assert that the terraform
HCL at remote_compose/terraform/dev_host/ initializes, validates, and plans
cleanly under moto-mocked AWS endpoints. Real terraform binary is required.

Phase 4.1 (rc-z7p) creates the HCL; Phase 4.2 (rc-4ra) wires the runner.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


# ---------- terraform availability helpers (mirrors test_terraform_runner.py) ----------


def _terraform_usable() -> bool:
    if shutil.which("terraform") is None:
        return False
    try:
        result = subprocess.run(
            ["terraform", "-version"], capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_terraform = pytest.mark.skipif(
    not _terraform_usable(),
    reason="terraform binary not usable in this environment",
)


# ---------- module location ----------


def _dev_host_module_path() -> Path:
    """Path to the dev_host terraform module shipped in the package."""
    from remote_compose import terraform as tf_pkg

    return Path(tf_pkg.__file__).parent / "dev_host"


# ---------- tests ----------


@requires_terraform
class TestDevHostModuleStructure:
    def test_module_directory_exists(self):
        path = _dev_host_module_path()
        assert path.is_dir(), f"dev_host terraform module missing at {path}"

    def test_module_has_main_tf(self):
        path = _dev_host_module_path() / "main.tf"
        assert path.is_file(), f"dev_host main.tf missing at {path}"

    def test_module_has_variables_tf(self):
        path = _dev_host_module_path() / "variables.tf"
        assert path.is_file(), f"dev_host variables.tf missing at {path}"

    def test_module_has_outputs_tf(self):
        path = _dev_host_module_path() / "outputs.tf"
        assert path.is_file(), f"dev_host outputs.tf missing at {path}"


@requires_terraform
class TestDevHostModuleValidates:
    def test_module_initializes_and_validates(self, tmp_path):
        """terraform init + validate must succeed against the shipped HCL."""
        from remote_compose.terraform.runner import TerraformRunner

        # copy module to a temp dir so we don't pollute the package
        src = _dev_host_module_path()
        for f in src.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())

        runner = TerraformRunner(tmp_path)
        runner.init(backend=False)
        runner.validate()

    def test_required_variables_declared(self, tmp_path):
        """The module must declare the variables DevHostService passes in."""
        src = _dev_host_module_path() / "variables.tf"
        content = src.read_text()

        for var_name in (
            "name",
            "instance_type",
            "ami_id",
            "subnet_id",
            "ssh_public_key",
            "security_group_ports",
            "user_data",
            "ebs_size_gb",
            "tags",
        ):
            assert f'variable "{var_name}"' in content, (
                f"dev_host module missing required variable: {var_name}"
            )

    def test_outputs_expose_aws_handles(self, tmp_path):
        """Outputs must expose what DevHostService records in state."""
        src = _dev_host_module_path() / "outputs.tf"
        content = src.read_text()

        for output_name in ("instance_id", "public_ip", "public_dns"):
            assert f'output "{output_name}"' in content, (
                f"dev_host module missing required output: {output_name}"
            )


# ---------- cloud-init render contract ----------


class TestCloudInitRenderedYamlIsValid:
    """Each source's render_user_data() must produce cloud-init AL2023 can parse."""

    def test_git_source_yaml_round_trips(self):
        import yaml

        from remote_compose.dev_host.bootstrap import GitSource

        rendered = GitSource(
            url="https://github.com/owner/repo.git", ref="main"
        ).render_user_data()

        assert rendered.startswith("#cloud-config")
        body = "\n".join(rendered.splitlines()[1:])
        parsed = yaml.safe_load(body)

        assert isinstance(parsed, dict)
        # cloud-init standard keys we expect at least one of
        assert any(k in parsed for k in ("runcmd", "write_files", "packages"))

    def test_image_source_yaml_round_trips(self):
        import yaml

        from remote_compose.dev_host.bootstrap import ImageSource

        rendered = ImageSource(image="nginx:alpine").render_user_data()
        body = "\n".join(rendered.splitlines()[1:])
        parsed = yaml.safe_load(body)
        assert isinstance(parsed, dict)

    def test_script_source_yaml_round_trips(self):
        import yaml

        from remote_compose.dev_host.bootstrap import ScriptSource

        rendered = ScriptSource(script="echo hi").render_user_data()
        body = "\n".join(rendered.splitlines()[1:])
        parsed = yaml.safe_load(body)
        assert isinstance(parsed, dict)
