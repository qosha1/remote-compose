"""rc adopt — bring a live AWS stack under terraform management.

Two modes (selected by the CLI's --from-local-tfstate flag):

  Default (from-scratch): the v1→v2 cutover case. No terraform state
  exists at all. Walks live AWS via v1_migrate.discover, generates
  terraform import addresses via v1_migrate.translate, drives terraform
  init + sequential terraform import per resource. State lands wherever
  rc.yml's terraform.backend block points (s3 if remote, local if not).

  --from-local-tfstate <path>: copies an existing local tfstate into
  the configured s3 backend in one shot. Handled in cli_commands/adopt.py.

Idempotent: running adopt twice on the same stack reports
``imported=0, skipped=N`` because every resource is already in state.
Partial failure recovery: if resource N+1 fails, the previous N stay
imported and re-running adopt only retries the failed set.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, NamedTuple, Optional


class AdoptResult(NamedTuple):
    """Outcome of an `rc adopt` invocation.

    imported:   resources newly added to terraform state this run
    skipped:    resources already in state (idempotent re-runs)
    failed:     list of (terraform_address, aws_resource_id, error_message)
    duration_s: wall-clock seconds
    """
    imported: int
    skipped: int
    failed: list[tuple[str, str, str]]
    duration_s: float


def adopt_v1_to_v2(
    rc_yml_path: Path,
    working_dir: Path,
    *,
    session: Optional[Any] = None,
) -> AdoptResult:
    """Walk live AWS, generate import addresses, populate terraform state.

    Args:
        rc_yml_path: Path to the rc.yml v2 file describing the target stack.
        working_dir: Directory where the v2 terraform module will be emitted
            (typically rc.yml's parent / ``terraform`` / provider name).
        session: Optional boto3 Session for the AWS walk + terraform's
            backend operations.

    Returns:
        AdoptResult capturing (imported, skipped, failed, duration_s).

    Idempotent: re-running on a fully-imported stack returns
    ``AdoptResult(imported=0, skipped=N, failed=[])``.

    Partial failure preserves consistency: if resource at position N
    fails, the N-1 resources already imported stay in state. The
    `failed` list lets the caller (or a re-run) retry just those.
    """
    start = time.monotonic()
    imports = _discover_imports(rc_yml_path, session=session)

    imported = 0
    skipped = 0
    failed: list[tuple[str, str, str]] = []

    for address, resource_id in imports:
        status, message = _run_terraform_import(
            working_dir, address, resource_id,
        )
        if status == "imported":
            imported += 1
        elif status == "already_in_state":
            skipped += 1
        else:  # failed
            failed.append((address, resource_id, message))

    return AdoptResult(
        imported=imported,
        skipped=skipped,
        failed=failed,
        duration_s=time.monotonic() - start,
    )


def _discover_imports(
    rc_yml_path: Path, *, session: Optional[Any] = None,
) -> list[tuple[str, str]]:
    """Walk live AWS via v1_migrate's discovery + translation, return
    (terraform_address, aws_resource_id) tuples ready to feed into
    `terraform import`.

    Reuses v1_migrate so the import-set stays in sync with what the v1
    migrate tool would write. The function is split out so tests can
    patch it without driving real AWS.
    """
    from remote_compose.cli_v2 import load_rc_yml
    from remote_compose.v1_migrate import discover, translate

    _version, raw, v2 = load_rc_yml(rc_yml_path)
    if v2 is None:
        return []

    # Re-shape v2 → v1-style stack the discoverer expects. The v1
    # discoverer reads project + cluster + region from a flat dict;
    # supply them from the v2 provider_config.
    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    stack = discover.V1Stack(
        project_name=v2.project,
        cluster=ecs_cfg.get("cluster", f"{v2.project}-cluster"),
        region=ecs_cfg.get("region", "us-west-2"),
        aws_profile=ecs_cfg.get("aws_profile"),
        compose_file=v2.compose_file,
    )
    inv = discover.discover(stack, session=session)
    return list(translate.imports_for(inv))


def _run_terraform_import(
    working_dir: Path, address: str, resource_id: str,
) -> tuple[str, str]:
    """Run `terraform import <address> <id>` in working_dir; classify result.

    Returns ``(status, message)`` where status is one of:
      - ``"imported"``: clean import.
      - ``"already_in_state"``: resource was already imported (idempotent).
      - ``"failed"``: any other terraform error; ``message`` carries stderr.

    Split out so adopt_v1_to_v2 stays a pure orchestrator + tests can
    patch the runner.
    """
    from remote_compose.terraform.runner import TerraformError, TerraformRunner

    runner = TerraformRunner(working_dir)
    try:
        runner.import_resource(address, resource_id)
        return ("imported", "")
    except TerraformError as exc:
        msg = ((exc.stderr or "") + (exc.stdout or "")).lower()
        if "already managed" in msg or "already exists in state" in msg:
            return ("already_in_state", str(exc))
        return ("failed", str(exc))
