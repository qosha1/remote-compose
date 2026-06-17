"""Golden-file regression test for the bootstrap deploy-role stack (rc-kiz.3).

Emit the committed bootstrap stack from a canonical config and byte-compare
against a committed fixture under ``tests/fixtures/golden/bootstrap_oidc/``.
Any template/derivation change that isn't intentional shows up here, and the
byte-for-byte match also proves the emit is deterministic/idempotent.

Regenerate after an intentional change (review the diff!):

    python -m tests.unit.test_bootstrap_golden --regenerate
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from remote_compose.bootstrap import emit_bootstrap_stack
from remote_compose.config.v2_schema import GithubOidcDeployRole

GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden" / "bootstrap_oidc"


def _canonical_role() -> GithubOidcDeployRole:
    return GithubOidcDeployRole(
        github_repo="acme/app",
        github_branch="main",
        permissions={
            "codebuild_project": "${project}-build",
            "ecr_namespace": "${project}/*",
            "ecs_clusters": ["${cluster}", "foundry-tenant-*"],
            "pass_roles": ["${project}-task", "${project}-task-exec"],
        },
    )


def _emit(out_dir: Path) -> None:
    emit_bootstrap_stack(
        _canonical_role(),
        project="golden",
        cluster="golden-cluster",
        workload_backend={
            "type": "s3",
            "bucket": "golden-tf-state",
            "key": "golden/ecs.tfstate",
            "region": "us-west-2",
            "dynamodb_table": "golden-locks",
        },
        out_dir=out_dir,
    )


class TestBootstrapGoldenMatches:
    def test_fixture_exists(self):
        assert GOLDEN_DIR.exists(), (
            f"golden fixture missing at {GOLDEN_DIR}. Regenerate with "
            f"`python -m tests.unit.test_bootstrap_golden --regenerate`."
        )

    def test_file_set_matches(self, tmp_path):
        out = tmp_path / "tf"
        _emit(out)
        fixture_names = {p.name for p in GOLDEN_DIR.iterdir()}
        emitted_names = {p.name for p in out.iterdir()}
        assert emitted_names == fixture_names, (
            f"file set drift — only-in-emitted: {emitted_names - fixture_names}; "
            f"only-in-fixture: {fixture_names - emitted_names}"
        )

    @pytest.mark.parametrize(
        "filename",
        (
            sorted(p.name for p in GOLDEN_DIR.iterdir() if p.is_file())
            if GOLDEN_DIR.exists()
            else []
        ),
    )
    def test_file_is_byte_identical(self, tmp_path, filename):
        out = tmp_path / "tf"
        _emit(out)
        expected = (GOLDEN_DIR / filename).read_bytes()
        actual = (out / filename).read_bytes()
        assert actual == expected, (
            f"{filename} drifted from the golden fixture. If intentional, "
            f"regenerate with `python -m tests.unit.test_bootstrap_golden "
            f"--regenerate` and review the diff."
        )


def _regenerate() -> None:
    if GOLDEN_DIR.exists():
        shutil.rmtree(GOLDEN_DIR)
    GOLDEN_DIR.mkdir(parents=True)
    _emit(GOLDEN_DIR)
    print(f"Regenerated golden fixture at {GOLDEN_DIR}")
    for p in sorted(GOLDEN_DIR.iterdir()):
        print(f"  {p.name}  {p.stat().st_size}B")


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print("Pass --regenerate to overwrite the golden fixture.", file=sys.stderr)
        sys.exit(2)
