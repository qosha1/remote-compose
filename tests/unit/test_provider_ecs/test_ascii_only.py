"""Regression test: emitted HCL must contain only ASCII in fields AWS validates.

AWS security group descriptions, IAM role descriptions, and several other
resource attributes are ASCII-only. This was caught in e2e by a CreateSG
rejection; this test makes sure no Unicode bullets / em-dashes / smart
quotes sneak into the templates again.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _rich_ctx(tmp_path: Path) -> DeployContext:
    return DeployContext(
        project="ascii-check",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "c",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web", cpu=256, memory=512, type="proxy", public=True, port=80
            ),
            "db": ServiceSpec(
                name="db",
                cpu=512,
                memory=1024,
                type="infrastructure",
                volumes=[{"name": "data", "mount": "/data"}],
            ),
            "worker": ServiceSpec(
                name="worker", cpu=1024, memory=2048, type="worker", launch_type="EC2"
            ),
        },
        secrets=[],
    )


def test_all_hcl_is_ascii(tmp_path):
    """Every emitted .tf file must be pure ASCII. AWS rejects non-ASCII
    in several fields (SG description, tag values in some cases, etc.)."""
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_rich_ctx(tmp_path), out)

    offenders: list[tuple[str, int, str, int]] = []
    for tf in sorted(out.rglob("*.tf")):
        for lineno, line in enumerate(tf.read_text().splitlines(), start=1):
            for col, ch in enumerate(line, start=1):
                if ord(ch) > 127:
                    offenders.append((tf.name, lineno, line, col))
                    break  # one finding per line is enough

    assert not offenders, "\n".join(
        f"  {name}:{lineno}:{col} — non-ASCII char U+{ord(line[col-1]):04X} in: {line!r}"
        for name, lineno, line, col in offenders
    )
