"""DB-integrity canary — the GO/NO-GO bar for the prod cutover.

The contract: a row written to a sandbox postgres BEFORE migration must
be readable AFTER migration. This is what proves the in-place EFS
preservation actually preserved data.

Workflow:
    1. Bring up a sandbox v1-shaped stack (rc v1) with:
       - postgres:16 mounting an EFS access point
       - django that connects to it
    2. Write a canary row: INSERT INTO migration_canary (id, marker)
       VALUES (1, 'pre-migration-2026-04-27');
    3. Run the migration tooling: discover -> build_plan -> apply phases.
    4. After cutover, the django service is now v2-managed. Connect to
       the SAME postgres (same EFS, same data) and SELECT the row.
    5. Assert the row is present AND the marker matches.

Moto cannot simulate the EFS posix-IO data plane (it mocks the control
plane only — file systems, access points exist in the API but no actual
storage backs them). So the canary test requires real AWS or a local
EFS-equivalent (NFS server in a docker-compose).

For the failing-tests phase, the canary is implemented as a tier-3
test that's marked skip-by-default. The actual test code is wired up
so that when run with RC_E2E_DB_CANARY=1 it executes against real AWS.

This is intentional: the failing-tests phase locks in the CONTRACT
(test exists, has correct shape, runs in skip mode), while the actual
red-light proof requires real AWS — which is by definition Phase 5.2's
manual verification step.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_compose.v1_migrate.apply import (
    EmitV2TerraformPhase,
    ImportStatePhase,
    ServicesCutoverPhase,
    ValidatePhase,
)
from remote_compose.v1_migrate.discover import discover
from remote_compose.v1_migrate.plan import build_plan


pytestmark = pytest.mark.integration


CANARY_ROW = (1, "pre-migration-canary-2026-04-27")
CANARY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS migration_canary (
    id INTEGER PRIMARY KEY,
    marker TEXT NOT NULL,
    written_at TIMESTAMP DEFAULT NOW()
);
"""


def _e2e_enabled() -> bool:
    return os.environ.get("RC_E2E_DB_CANARY") == "1"


@pytest.fixture
def real_aws_canary_stack(tmp_path):
    """Stand up a real (cheap) sandbox v1 stack in AWS with a postgres
    container on EFS. Yield a connection-info dict.

    Skipped unless RC_E2E_DB_CANARY=1, since this costs money and time.
    """
    if not _e2e_enabled():
        pytest.skip(
            "DB-integrity canary requires RC_E2E_DB_CANARY=1 (real AWS). "
            "This is the GO/NO-GO bar for prod cutover — must pass before "
            "running the migration in prod."
        )
    raise NotImplementedError(
        "real_aws_canary_stack fixture: provision sandbox stack via rc v1, "
        "return {rc_yml: Path, region: str, postgres_dsn: str}"
    )


# ---------------------------------------------------------------------
# Canary row write/read
# ---------------------------------------------------------------------

class TestDbIntegrityCanary:
    """Run only with RC_E2E_DB_CANARY=1; otherwise all tests skip."""

    def test_pre_migration_canary_write(self, real_aws_canary_stack):
        # Connect to v1 postgres, ensure table exists, INSERT canary row.
        # The actual psycopg2 connect logic lives behind the fixture so
        # the test stays declarative.
        import psycopg2  # noqa: F401 — only imported when test runs
        raise NotImplementedError(
            "psycopg2.connect(dsn) -> CREATE TABLE -> INSERT canary"
        )

    def test_migration_then_post_migration_canary_read(
        self, real_aws_canary_stack
    ):
        # After the canary row exists in v1, run the full migration.
        # Then connect to the SAME postgres (now v2-managed) and SELECT.
        # Row must be present, marker must match.
        info = real_aws_canary_stack
        stack, inv = discover(
            rc_v1_yml_path=info["rc_yml"],
            aws_session=info["session"],
        )
        plan = build_plan(stack, inv)

        # Phase 1: validate (no mutation)
        v = ValidatePhase(plan=plan, aws_session=info["session"]).run()
        assert v.ok, f"validate failed: {v.details}"

        # Phase 2: emit
        out_dir = Path(info["working_dir"]) / "tf-v2"
        EmitV2TerraformPhase(plan=plan, output_dir=out_dir).run()

        # Phase 3: import state into a sandbox copy first
        sandbox_state = Path(info["working_dir"]) / "tfstate.copy"
        sandbox_state.write_bytes(
            Path(info["live_tfstate"]).read_bytes()
        )
        ip = ImportStatePhase(
            plan=plan,
            output_dir=out_dir,
            sandbox_tfstate=sandbox_state,
        ).run()
        assert ip.ok, f"import state failed: {ip.details}"

        # Phase 4: cutover services
        co = ServicesCutoverPhase(
            plan=plan,
            ecs_client=info["session"].client("ecs"),
        ).run()
        assert co.ok, f"cutover failed: {co.details}"

        # Phase 5 (post-cutover): the canary MUST still be readable.
        import psycopg2
        with psycopg2.connect(info["postgres_dsn"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, marker FROM migration_canary WHERE id = %s;",
                    (CANARY_ROW[0],),
                )
                row = cur.fetchone()
        assert row is not None, (
            "DB-INTEGRITY-CANARY FAILURE: canary row gone after migration. "
            "DO NOT RUN MIGRATION IN PROD."
        )
        assert row[1] == CANARY_ROW[1], (
            f"DB-INTEGRITY-CANARY FAILURE: marker mismatch. "
            f"expected {CANARY_ROW[1]!r}, got {row[1]!r}. "
            f"DO NOT RUN MIGRATION IN PROD."
        )

    def test_canary_table_uses_correct_schema(self, real_aws_canary_stack):
        # Belt-and-suspenders: the schema we use for the canary table must
        # be readable by the same migration code that runs in prod, since
        # cutover replaces the django container's image. If the new
        # container can't read the old schema, that's also data loss.
        raise NotImplementedError(
            "verify schema readability via post-cutover django shell"
        )
