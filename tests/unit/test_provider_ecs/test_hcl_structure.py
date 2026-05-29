"""HCL-level structural assertions on the ECS provider's emitted module.

Uses ``python-hcl2`` to parse every ``.tf`` file, aggregate the defined
resources, and confirm that cross-file references point at resources that
exist. Complements the string-match unit tests by catching typos and drift
a regex wouldn't see (e.g. ``aws_ecs_cluster.mian.id``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import hcl2
import pytest

from remote_compose.provider import DeployContext, SecretRef, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

REF_RE = re.compile(r"(aws_[a-z0-9_]+|data\.aws_[a-z0-9_]+)\.([A-Za-z_][A-Za-z0-9_]*)")


def _ctx(tmp_path: Path) -> DeployContext:
    env = tmp_path / ".django"
    env.write_text("SECRET_KEY=placeholder\nDATABASE_URL=placeholder\n")
    return DeployContext(
        project="struct",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={"domain": "api.example.com", "tls": {"mode": "acm"}},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "struct-cluster",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
                health_check_path="/",
            ),
            "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
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
        secrets=[
            SecretRef(name="django", source="file", path=str(env)),
        ],
    )


def _parse_module(tf_dir: Path) -> dict[str, Any]:
    """Return {'resources': {type: {name}}, 'data': {type: {name}}, 'raw_text': str}."""
    resources: dict[str, set[str]] = {}
    data_sources: dict[str, set[str]] = {}
    raw_text_parts: list[str] = []

    for tf in sorted(tf_dir.glob("*.tf")):
        text = tf.read_text()
        raw_text_parts.append(text)
        if not text.strip():
            continue
        with tf.open() as f:
            parsed = hcl2.load(f)
        for block in parsed.get("resource", []):
            for rtype, rbody in block.items():
                rtype = rtype.strip('"')
                for name in rbody.keys():
                    name = name.strip('"')
                    resources.setdefault(rtype, set()).add(name)
        for block in parsed.get("data", []):
            for dtype, dbody in block.items():
                dtype = dtype.strip('"')
                for name in dbody.keys():
                    name = name.strip('"')
                    data_sources.setdefault(dtype, set()).add(name)

    return {
        "resources": resources,
        "data_sources": data_sources,
        "raw_text": "\n".join(raw_text_parts),
    }


class TestHclParsesCleanly:
    def test_every_tf_file_is_valid_hcl(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        for tf in out.glob("*.tf"):
            if not tf.read_text().strip():
                continue  # templates that conditionally render nothing
            with tf.open() as f:
                try:
                    hcl2.load(f)
                except Exception as exc:
                    pytest.fail(f"{tf.name}: HCL parse failed — {exc}")


class TestResourceInventory:
    def test_expected_resource_types_present(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        mod = _parse_module(out)
        expected = {
            "aws_vpc",
            "aws_subnet",
            "aws_internet_gateway",
            "aws_security_group",
            "aws_lb",
            "aws_lb_listener",
            "aws_lb_target_group",
            "aws_ecs_cluster",
            "aws_ecs_cluster_capacity_providers",
            "aws_iam_role",
            "aws_cloudwatch_log_group",
            "aws_ecr_repository",
            "aws_ecs_task_definition",
            "aws_ecs_service",
            "aws_efs_file_system",
            "aws_efs_mount_target",
            "aws_efs_access_point",
            "aws_secretsmanager_secret",
            "aws_secretsmanager_secret_version",
            "aws_iam_role_policy",  # for secrets
            "aws_launch_template",
            "aws_autoscaling_group",
            "aws_ecs_capacity_provider",
            "aws_acm_certificate",
            "aws_route53_record",
        }
        missing = expected - set(mod["resources"].keys())
        assert not missing, f"missing resource types: {missing}"

    def test_one_service_set_per_compose_service(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        mod = _parse_module(out)
        svc_names = mod["resources"]["aws_ecs_service"]
        task_def_names = mod["resources"]["aws_ecs_task_definition"]
        ecr_names = mod["resources"]["aws_ecr_repository"]
        assert svc_names == {"web", "api", "db", "worker"}
        assert task_def_names == svc_names
        assert ecr_names == svc_names

    def test_efs_structure_matches_volumes(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        mod = _parse_module(out)
        # one file_system per named volume
        assert mod["resources"]["aws_efs_file_system"] == {"data"}
        # one access_point per (service, volume) pair; only db mounts 'data'
        assert mod["resources"]["aws_efs_access_point"] == {"db__data"}


class TestReferentialIntegrity:
    def test_every_resource_reference_has_a_target(self, tmp_path):
        """Every aws_X.Y token outside a `resource \"aws_X\" \"Y\"` header must
        refer to a defined resource or data source."""
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        mod = _parse_module(out)
        defined = set()
        for rtype, names in mod["resources"].items():
            for name in names:
                defined.add(f"{rtype}.{name}")
        for dtype, names in mod["data_sources"].items():
            for name in names:
                defined.add(f"data.{dtype}.{name}")

        # Strip header lines `resource "aws_x" "y"` so we don't count
        # definitions as references.
        text = re.sub(
            r'resource\s+"([^"]+)"\s+"([^"]+)"',
            lambda m: f"<<def {m.group(1)}.{m.group(2)}>>",
            mod["raw_text"],
        )
        text = re.sub(
            r'data\s+"([^"]+)"\s+"([^"]+)"',
            lambda m: f"<<def data.{m.group(1)}.{m.group(2)}>>",
            text,
        )
        text = re.sub(r"<<def [^>]+>>", "", text)

        unresolved: list[str] = []
        for m in REF_RE.finditer(text):
            token = m.group(0)
            if token in defined:
                continue
            # allow `data.aws_<x>.<y>` form already matched above
            unresolved.append(token)

        # Deduplicate, keep only references for AWS-managed types
        unresolved = sorted(
            set(
                u
                for u in unresolved
                if u.startswith("aws_") or u.startswith("data.aws_")
            )
        )
        assert not unresolved, "references to undefined resources:\n  " + "\n  ".join(
            unresolved
        )
