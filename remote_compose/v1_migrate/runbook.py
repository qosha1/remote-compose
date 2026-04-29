"""Runbook — per-phase audit log + undo-command emission.

When `rc v1 migrate` runs, every phase appends a RunbookEntry. On
success, the runbook is written to <output_dir>/runbook.json. On
failure, the matching undo command is printed to stderr so the
operator can hand-execute the rollback.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RunbookEntry:
    phase: str
    started_at: str
    finished_at: str | None
    ok: bool
    undo_command: str
    details: str

    @classmethod
    def begin(cls, phase: str, undo_command: str = "") -> "RunbookEntry":
        return cls(
            phase=phase,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            ok=False,
            undo_command=undo_command,
            details="",
        )

    def finish(self, ok: bool, details: str) -> None:
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.ok = ok
        self.details = details


def write_runbook_json(entries: list[RunbookEntry], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in entries], indent=2))


def find_undo_for_phase(
    entries: list[RunbookEntry], phase_name: str,
) -> str | None:
    for e in entries:
        if e.phase == phase_name:
            return e.undo_command or None
    return None


def format_undo_runbook(entries: list[RunbookEntry]) -> str:
    """Human-readable text. Used when a phase fails — print the undo
    chain in reverse order so the operator can step backwards.
    """
    lines = ["# Undo runbook (run in reverse order):"]
    for e in reversed(entries):
        if e.ok and e.undo_command:
            lines.append(f"# {e.phase} (succeeded; undo if desired):")
            lines.append(f"  {e.undo_command}")
    return "\n".join(lines)
