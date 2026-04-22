"""Truth test for ECSProvider.emit_terraform.

Runs ``terraform init -backend=false && terraform validate`` against the
emitted module. If this passes, the HCL is syntactically and
semantically valid according to the AWS provider.

Skipped automatically when terraform is not usable in this environment
(see sentinel in test_terraform_runner).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import TerraformRunner


pytestmark = pytest.mark.integration


def _terraform_usable() -> bool:
    if not shutil.which("terraform"):
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
def ecs_ctx(tmp_path):
    return DeployContext(
        project="itest",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-2",
            "cluster": "itest-cluster",
            "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(name="web", cpu=256, memory=512, type="proxy",
                               public=True, port=80, health_check_path="/health"),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
        },
        secrets=[],
    )


@requires_terraform
class TestEmittedHclValidates:
    def test_terraform_init_and_validate(self, ecs_ctx, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ecs_ctx, out)
        runner = TerraformRunner(out)
        runner.init(backend=False)
        runner.validate()
