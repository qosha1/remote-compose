"""rc-2kp: track when an `rc fix *` subcommand has modified Dockerfiles
or source files since the last build, so the next `rc up` can force
``--no-cache`` and avoid stale layer-cache hits.

Real-world repro 2026-04-30: ran ``rc fix bake-bind-mount-source django``
which appended ``COPY ./backend /app`` to the Dockerfile. The next
``rc up`` reported "Deploy complete duration: 211s" but the resulting
image's /app was empty — buildx had cache-hit a stage that never had the
COPY (the registry cache contained an image whose Dockerfile predated the
fix; buildx pulled those layers despite the source Dockerfile's content
change).

The defensive move: when any `rc fix *` writes to disk, drop a sentinel
file. The next `rc up` reads it, passes ``--no-cache`` to the build, and
clears the sentinel. Subsequent builds use cache normally.
"""

from __future__ import annotations

from pathlib import Path

_SENTINEL_REL = ".rc/no-cache-next-build"


def _sentinel_path(project_dir: Path) -> Path:
    return project_dir / _SENTINEL_REL


def mark_no_cache(project_dir: Path, reason: str = "") -> None:
    """Drop the sentinel file. Idempotent. ``reason`` is written into the
    file body (for diagnostics) but the file's mere existence is the
    signal."""
    p = _sentinel_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason or "")


def consume_no_cache(project_dir: Path) -> bool:
    """Return True + delete the sentinel when present, else False. The
    next build path calls this to learn whether to force --no-cache."""
    p = _sentinel_path(project_dir)
    if not p.exists():
        return False
    try:
        p.unlink()
    except OSError:
        return False
    return True


def is_no_cache_pending(project_dir: Path) -> bool:
    """Read-only check — does NOT consume. Useful for tests + diagnostics."""
    return _sentinel_path(project_dir).exists()
