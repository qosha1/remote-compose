"""Golden-file regression tests for the ECS provider.

Emit terraform from a canonical DeployContext and byte-compare the result
against a committed fixture under ``tests/fixtures/golden/ecs_minimal/``.

Any template change that isn't intentional shows up here immediately.
When an intentional template change lands, regenerate the fixture:

    python -m tests.unit.test_provider_ecs.test_golden --regenerate

The fixture covers a rich scenario: mixed Fargate + EC2 services, EFS
volume, file + aws_sm secrets, custom domain with ACM. If a template
change affects any of these paths, the fixture diff is the canonical
review artifact.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, SecretRef, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


GOLDEN_DIR = Path(__file__).parent.parent.parent / "fixtures" / "golden" / "ecs_minimal"


def _canonical_ctx(working_dir: Path) -> DeployContext:
    """Locked-down inputs so the golden fixture stays stable."""
    # The file secret is read at emit time for KEY names; provide a stable
    # env file under working_dir so the fixture is reproducible.
    env_dir = working_dir / ".envs" / ".production"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".django").write_text(
        "SECRET_KEY=placeholder\nDATABASE_URL=placeholder\n"
    )
    return DeployContext(
        project="golden",
        compose_path=working_dir / "docker-compose.yml",
        rc_yml_v2={
            "version": 2, "project": "golden",
            "domain": "api.example.com",
            "tls": {"mode": "acm"},
        },
        provider_config={"ecs": {
            "region": "us-west-2", "cluster": "golden-cluster",
            "vpc_cidr": "10.0.0.0/16", "aws_profile": "golden",
            "ec2_capacity": {"capacity_type": "ON_DEMAND"},
        }},
        tf_backend_config={
            "type": "s3", "bucket": "golden-tf-state",
            "key": "golden/ecs.tfstate", "region": "us-west-2",
        },
        working_dir=working_dir,
        services={
            "web": ServiceSpec(
                name="web", cpu=256, memory=512, type="proxy",
                public=True, port=80, health_check_path="/health",
            ),
            "api": ServiceSpec(
                name="api", cpu=512, memory=1024, replicas=2,
                type="application", health_check_path="/api/health/",
            ),
            "db": ServiceSpec(
                name="db", cpu=512, memory=1024, type="infrastructure",
                volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
            ),
            "worker": ServiceSpec(
                name="worker", cpu=1024, memory=2048, type="worker",
                launch_type="EC2",
            ),
        },
        secrets=[
            SecretRef(name="django", source="file",
                      path=".envs/.production/.django"),
            SecretRef(name="db_password", source="aws_sm",
                      arn="arn:aws:secretsmanager:us-west-2:111122223333:"
                          "secret:golden/db-AbCdEf"),
        ],
    )


class TestGoldenFixtureMatches:
    def test_fixture_exists(self):
        assert GOLDEN_DIR.exists(), (
            f"golden fixture missing at {GOLDEN_DIR}. Regenerate with "
            f"`python -m tests.unit.test_provider_ecs.test_golden --regenerate`."
        )

    def test_every_fixture_file_is_produced(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_canonical_ctx(tmp_path), out)
        fixture_names = {p.name for p in GOLDEN_DIR.iterdir()}
        emitted_names = {p.name for p in out.iterdir()}
        assert emitted_names == fixture_names, (
            f"file set drift — only-in-emitted: {emitted_names - fixture_names}; "
            f"only-in-fixture: {fixture_names - emitted_names}"
        )

    @pytest.mark.parametrize("filename", sorted(
        p.name for p in GOLDEN_DIR.iterdir() if p.is_file()
    ) if GOLDEN_DIR.exists() else [])
    def test_file_is_byte_identical(self, tmp_path, filename):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_canonical_ctx(tmp_path), out)
        expected = (GOLDEN_DIR / filename).read_bytes()
        actual = (out / filename).read_bytes()
        assert actual == expected, (
            f"{filename} has drifted from the golden fixture. If this change "
            f"is intentional, regenerate the fixture with "
            f"`python -m tests.unit.test_provider_ecs.test_golden --regenerate`."
        )


def _regenerate() -> None:
    """Overwrite the golden fixture with a fresh emit. Human-invoked only."""
    import tempfile
    if GOLDEN_DIR.exists():
        shutil.rmtree(GOLDEN_DIR)
    GOLDEN_DIR.mkdir(parents=True)
    # Use a tempdir as working_dir so the ctx's helper-created files
    # (.envs/.production/.django for the file-secret) don't leak into
    # tests/fixtures/.
    with tempfile.TemporaryDirectory() as work:
        ECSProvider().emit_terraform(_canonical_ctx(Path(work)), GOLDEN_DIR)
    print(f"Regenerated golden fixture at {GOLDEN_DIR}")
    for p in sorted(GOLDEN_DIR.iterdir()):
        print(f"  {p.name}  {p.stat().st_size}B")


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print("Pass --regenerate to overwrite the golden fixture.", file=sys.stderr)
        sys.exit(2)
