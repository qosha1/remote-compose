"""rc adopt — bring a live AWS stack under terraform management.

Two modes (selected by the CLI's --from-local-tfstate flag):

  Default (from-scratch): the live-stack adoption case. Emits the v2
  terraform module, runs ``terraform init`` against the configured
  backend, walks live AWS to build the import set, then runs
  ``terraform import`` per resource. After a clean run, terraform state
  matches live AWS and ``rc plan`` should report ~no drift.

  --from-local-tfstate <path>: copies an existing local tfstate into
  the configured s3 backend in one shot. Handled in cli_commands/adopt.py.

Idempotent: running adopt twice on the same stack reports
``imported=0, skipped=N`` because every resource is already in state.
Partial failure recovery: if resource N+1 fails, the previous N stay
imported and re-running adopt only retries the failed set.

The import set itself is built by ``state_backend.adopt_imports`` (rc-6o3),
which enumerates the emitted terraform resource addresses and resolves
each to its live AWS id via boto3.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

from .adopt_imports import ImportPlan, build_import_plan


class AdoptResult(NamedTuple):
    """Outcome of an `rc adopt` invocation.

    imported:   resources newly added to terraform state this run
    skipped:    resources already in state (idempotent re-runs)
    failed:     list of (terraform_address, aws_resource_id, error_message)
    not_live:   list of (terraform_address, reason) for addresses with no
                live resource to import (terraform will create them on the
                first apply) or no resolver — informational, not a failure
    duration_s: wall-clock seconds
    """

    imported: int
    skipped: int
    failed: list[tuple[str, str, str]]
    not_live: list[tuple[str, str]] = []
    duration_s: float = 0.0


def adopt_v1_to_v2(
    rc_yml_path: Path,
    working_dir: Optional[Path] = None,
    *,
    session: Optional[Any] = None,
) -> AdoptResult:
    """Emit + init the terraform module, then import live AWS into state.

    Args:
        rc_yml_path: Path to the rc.yml v2 file describing the target stack.
        working_dir: Optional override for the terraform module directory.
            When None (the normal path) it's derived from the rc.yml so
            adopt operates on the SAME directory + state as ``rc plan`` /
            ``rc deploy`` (the rc.yml's parent / ``terraform``).
        session: Optional boto3 Session for the AWS walk. When None one is
            built from the rc.yml's aws_profile + region.

    Returns:
        AdoptResult capturing (imported, skipped, failed, not_live,
        duration_s).

    Idempotent: re-running on a fully-imported stack returns
    ``imported=0, skipped=N``.

    Partial failure preserves consistency: if a resource fails to import,
    the ones already imported stay in state and the ``failed`` list lets a
    re-run retry just those.
    """
    start = time.monotonic()
    tf_dir = _prepare_module(rc_yml_path, working_dir)
    plan = _discover_imports(rc_yml_path, tf_dir, session=session)

    imported = 0
    skipped = 0
    failed: list[tuple[str, str, str]] = []

    for address, resource_id in plan.imports:
        status, message = _run_terraform_import(tf_dir, address, resource_id)
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
        not_live=plan.skipped,
        duration_s=time.monotonic() - start,
    )


def _prepare_module(
    rc_yml_path: Path,
    working_dir: Optional[Path] = None,
) -> Path:
    """Emit the v2 terraform module and run ``terraform init``.

    Returns the directory the module + state live in (where subsequent
    ``terraform import`` calls run). Derived from the rc.yml so it matches
    the dir ``rc plan``/``rc deploy`` use, unless ``working_dir`` overrides
    it (mostly for tests).

    terraform import requires (a) the resource blocks to exist in config
    and (b) an initialized backend, so both steps run before any import.
    """
    from remote_compose.cli_v2 import (
        build_deploy_context,
        load_rc_yml,
        resolve_provider,
    )
    from remote_compose.terraform.runner import TerraformRunner

    _version, raw, v2 = load_rc_yml(rc_yml_path)
    if v2 is None:
        raise ValueError(f"{rc_yml_path} is not a v2 rc.yml")

    ctx = build_deploy_context(v2, raw, Path(rc_yml_path))
    provider = resolve_provider(v2)

    tf_dir = Path(working_dir) if working_dir else Path(ctx.working_dir) / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)

    # emit_terraform writes the full module (deterministic) into tf_dir.
    provider.emit_terraform(ctx, tf_dir)
    TerraformRunner(tf_dir).init()
    return tf_dir


def _discover_imports(
    rc_yml_path: Path,
    working_dir: Path,
    *,
    session: Optional[Any] = None,
) -> ImportPlan:
    """Build the (address, id) import set for the emitted module.

    Thin wrapper over ``adopt_imports.build_import_plan`` kept as a seam so
    the orchestrator + its tests can patch discovery without driving real
    AWS. ``working_dir`` must already contain the emitted ``*.tf`` files.
    """
    return build_import_plan(rc_yml_path, Path(working_dir), session=session)


def _run_terraform_import(
    working_dir: Path,
    address: str,
    resource_id: str,
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
