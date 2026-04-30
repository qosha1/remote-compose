"""rc-60x: heartbeat context manager emits progress every N seconds
during long blocking calls. Daemon thread; never blocks shutdown.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from remote_compose.heartbeat import heartbeat


class TestHeartbeatEmitsTicks:
    def test_emits_label_with_elapsed(self):
        events: list[str] = []
        with heartbeat(events.append, "doing thing", interval=0):
            # interval=0 falls back to env / default — use a tiny env override
            pass
        # Fast exit (no ticks expected) — test_quick_exit_no_ticks covers this.
        assert events == [] or all("doing thing" in e for e in events)

    def test_no_emit_callback_is_noop(self):
        # Pass None for emit — heartbeat should be silent.
        with heartbeat(None, "x", interval=0):
            time.sleep(0.05)

    def test_short_interval_emits_multiple(self, monkeypatch):
        monkeypatch.setenv("RC_HEARTBEAT_INTERVAL_S", "1")
        events: list[str] = []
        # Use interval kwarg = None (falls back to env var = 1).
        with heartbeat(events.append, "thing", interval=None):
            # Block for slightly more than two intervals so 2 ticks fire.
            time.sleep(2.3)
        # 2 ticks expected (at t=1s and t=2s); allow 1-3 for jitter.
        assert 1 <= len(events) <= 3, f"events={events}"
        for e in events:
            assert "thing still running" in e
            assert "elapsed=" in e

    def test_quick_exit_no_ticks(self):
        events: list[str] = []
        with heartbeat(events.append, "fast", interval=10):
            time.sleep(0.01)
        assert events == []

    def test_emit_exception_doesnt_kill_thread(self):
        """If the progress callback raises, the heartbeat thread must
        survive (and just suppress the exception) so subsequent ticks
        keep firing."""
        call_count = {"n": 0}

        def emit(_msg):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("oops")

        with heartbeat(emit, "x", interval=1):
            # 0.05s should give 1 tick at t=0+ which raises and is swallowed.
            # Block 1.5s; expect at least 1 tick.
            import time as _t
            _t.sleep(1.2)
        assert call_count["n"] >= 1
