"""Ephemeral stack registry + TTL helpers for `rc deploy --ttl ...` / `rc reap`.

Two responsibilities live here so callers (cli.py, provider) only see one
import path:

  * ``parse_duration("4h30m") -> timedelta`` — accept compose-style
    duration strings (5m, 30m, 2h, 1d, 4h30m) and convert to a timedelta.

  * Registry read/write under ``~/.config/remote-compose/ephemeral.json``.
    A flat JSON list of records keyed (uniquely) by ``(project, region)``.
    ``register_stack`` is idempotent — re-running ``rc deploy --ttl 4h``
    on the same stack updates ``expires_at`` instead of duplicating.

The registry purposefully sits OUTSIDE Django models so ``rc reap`` works
without an ORM bootstrap (and works against multiple projects from one
shell). The single source of truth for "what ephemeral stacks exist on
this machine?" is this file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

# Accepts: 5m, 30m, 2h, 1d, 4h30m, 90s, 1d12h, 2h15m30s. Whitespace OK.
_DURATION_PART_RE = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_DURATION_FULL_RE = re.compile(
    r"^\s*(?:\d+\s*[smhd]\s*)+$",
    re.IGNORECASE,
)


_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


def parse_duration(value: str) -> timedelta:
    """Parse a duration string like ``5m``, ``2h``, ``1d``, ``4h30m``.

    Raises ``ValueError`` on empty / invalid input. Zero-second durations
    (e.g. ``0s``) are allowed and return a zero ``timedelta``.
    """
    if not isinstance(value, str):
        raise ValueError(f"duration must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError("duration cannot be empty")
    if not _DURATION_FULL_RE.match(text):
        raise ValueError(
            f"invalid duration {value!r}: use combos of <int><s|m|h|d>, "
            f"e.g. 5m, 2h, 1d, 4h30m"
        )
    total = 0
    for amount, unit in _DURATION_PART_RE.findall(text):
        total += int(amount) * _UNIT_SECONDS[unit.lower()]
    return timedelta(seconds=total)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


DEFAULT_REGISTRY_PATH = Path(
    os.environ.get(
        "RC_EPHEMERAL_REGISTRY",
        str(Path.home() / ".config" / "remote-compose" / "ephemeral.json"),
    )
)


def _now_utc() -> datetime:
    """Override-able UTC clock — kept private so tests can monkeypatch."""
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    """Serialize a datetime as an ISO 8601 UTC string ending in 'Z'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # drop microseconds for terraform tag readability
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_iso_utc(value: str) -> datetime:
    """Parse the ISO 8601 strings produced by ``to_iso_utc``."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class EphemeralRecord:
    project: str
    region: str
    expires_at: str           # ISO 8601 UTC, "...Z"
    rc_yml_path: str          # absolute path to rc.yml that produced this
    terraform_dir: str        # absolute path to the emitted terraform module
    created_at: str           # ISO 8601 UTC, "...Z" — first-seen timestamp
    aws_profile: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.project, self.region)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        when = now or _now_utc()
        return from_iso_utc(self.expires_at) <= when


def _load_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text() or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ephemeral registry {path} is corrupt JSON: {exc}. "
            f"Move it aside and re-run."
        ) from exc
    if not isinstance(data, list):
        raise ValueError(
            f"ephemeral registry {path} must contain a JSON list, "
            f"got {type(data).__name__}"
        )
    return data


def _write_raw(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pretty-print so users can eyeball / hand-edit if a stack gets stuck.
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")


def list_records(path: Optional[Path] = None) -> list[EphemeralRecord]:
    """Return every record in the registry. Order: file order."""
    p = Path(path) if path else DEFAULT_REGISTRY_PATH
    return [EphemeralRecord(**raw) for raw in _load_raw(p)]


def register_stack(
    *,
    project: str,
    region: str,
    expires_at: str,
    rc_yml_path: str,
    terraform_dir: str,
    aws_profile: Optional[str] = None,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> EphemeralRecord:
    """Add or update an ephemeral stack record.

    Idempotent on (project, region): re-registering the same stack
    refreshes ``expires_at`` (and any other supplied fields) but
    preserves the ORIGINAL ``created_at`` so age accounting stays
    honest across `rc deploy --ttl ...` reruns.
    """
    p = Path(path) if path else DEFAULT_REGISTRY_PATH
    raw = _load_raw(p)
    now_iso = to_iso_utc(now or _now_utc())
    new_record = EphemeralRecord(
        project=project,
        region=region,
        expires_at=expires_at,
        rc_yml_path=str(rc_yml_path),
        terraform_dir=str(terraform_dir),
        created_at=now_iso,
        aws_profile=aws_profile,
    )
    out: list[dict] = []
    found = False
    for entry in raw:
        if entry.get("project") == project and entry.get("region") == region:
            # Preserve original created_at so reruns don't reset stack age.
            new_record.created_at = entry.get("created_at", now_iso)
            out.append(asdict(new_record))
            found = True
        else:
            out.append(entry)
    if not found:
        out.append(asdict(new_record))
    _write_raw(p, out)
    return new_record


def remove_stack(
    *, project: str, region: str, path: Optional[Path] = None
) -> bool:
    """Remove a single stack from the registry. Returns True if removed."""
    p = Path(path) if path else DEFAULT_REGISTRY_PATH
    raw = _load_raw(p)
    out = [
        e for e in raw
        if not (e.get("project") == project and e.get("region") == region)
    ]
    if len(out) == len(raw):
        return False
    _write_raw(p, out)
    return True


def find_expired(
    *, now: Optional[datetime] = None, path: Optional[Path] = None
) -> list[EphemeralRecord]:
    """Return registry records whose ``expires_at`` is in the past."""
    when = now or _now_utc()
    return [r for r in list_records(path=path) if r.is_expired(when)]
