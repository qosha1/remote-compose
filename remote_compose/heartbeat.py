"""rc-60x: heartbeat helper — emit progress every N seconds during long
blocking calls so users can tell stuck vs progressing.

Sentinal repro: rc up went silent for 9 min during terraform apply, 25 min
during the buildx cache-to hang, 5 min during the warning collector. Each
silent stretch made the user wonder if the deploy was hung.

Usage:

    from remote_compose.heartbeat import heartbeat

    with heartbeat(self._emit, label="terraform apply"):
        runner.apply()

The context manager spawns a daemon thread that emits
``  ... <label> still running (elapsed=Ns)`` every ``interval`` seconds
until the context body exits (success OR exception). Thread is daemon so
it never blocks process exit.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

_DEFAULT_INTERVAL_S = 30


def _interval_from_env() -> int:
    """RC_HEARTBEAT_INTERVAL_S overrides the default (used in tests + by
    operators tuning chatty output)."""
    raw = os.environ.get("RC_HEARTBEAT_INTERVAL_S")
    if not raw:
        return _DEFAULT_INTERVAL_S
    try:
        n = int(raw)
        if n < 1:
            return _DEFAULT_INTERVAL_S
        return n
    except ValueError:
        return _DEFAULT_INTERVAL_S


@contextmanager
def heartbeat(
    emit: Optional[Callable[[str], None]],
    label: str,
    interval: Optional[int] = None,
) -> Iterator[None]:
    """Emit ``... <label> still running (elapsed=Ns)`` every ``interval``
    seconds until the body of the with-block exits.

    Daemon thread; never blocks shutdown. Silent when ``emit`` is None
    (e.g. a provider invoked without a progress callback). Honors
    ``RC_HEARTBEAT_INTERVAL_S`` env var for test/operator overrides.
    """
    if emit is None:
        # Nothing to do — silently no-op. Common for FakeProvider /
        # tests that don't wire a progress callback.
        yield
        return
    if interval is None:
        interval = _interval_from_env()
    stop = threading.Event()
    start = time.monotonic()

    def _tick() -> None:
        # `wait(interval)` returns True if stop fires; False on timeout.
        # Loop while it times out (= no stop yet) and emit each tick.
        while not stop.wait(interval):
            elapsed = int(time.monotonic() - start)
            try:
                emit(f"  ... {label} still running (elapsed={elapsed}s)")
            except Exception:  # noqa: BLE001
                # Don't let a buggy progress callback kill the heartbeat
                # thread — it would silently stop ticking forever.
                pass

    thread = threading.Thread(target=_tick, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # Best-effort join. If interval was big and thread is mid-wait,
        # this returns quickly because stop.wait() returns immediately
        # once stop is set.
        thread.join(timeout=2)
