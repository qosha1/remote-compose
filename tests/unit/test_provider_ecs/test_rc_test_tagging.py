"""Projects prefixed rc-test-* must get an Environment=rc-test default_tag.

This is the invariant the reap script relies on (scripts/reap_test_region.py).
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path: Path, project: str) -> DeployContext:
    return DeployContext(
        project=project,
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-east-1", "cluster": f"{project}-cluster",
            "vpc_cidr": "10.99.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={"web": ServiceSpec(name="web", cpu=256, memory=512, type="proxy",
                                     public=True, port=80)},
        secrets=[],
    )


def test_rc_test_prefix_adds_environment_tag(tmp_path):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, "rc-test-abc123"), out)
    providers = (out / "providers.tf").read_text()
    assert 'Environment = "rc-test"' in providers, (
        "rc-test-* projects must carry Environment=rc-test for reap script "
        "to find them. Got providers.tf:\n" + providers
    )


def test_normal_project_has_no_environment_tag(tmp_path):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, "myapp-prod"), out)
    providers = (out / "providers.tf").read_text()
    assert "Environment" not in providers
