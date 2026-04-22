"""Click analytics worker — consumes the 'clicks' Redis queue and bumps
per-URL counters in Postgres.

Deliberately simple: BLPOP one event at a time, log, update, repeat. In
production you'd batch writes, add retries, emit metrics. The point is
to exercise the EC2 launch type path in the ECS provider and prove that
service-to-service DNS works (worker → db, worker → cache).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

import psycopg
import redis


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://demo:demo@db:5432/demo")
REDIS_URL = os.environ.get("REDIS_URL", "redis://cache:6379/0")
CLICK_QUEUE = "clicks"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")


_shutdown = False


def _handle_signal(signum, _frame) -> None:
    global _shutdown
    log.info(f"signal {signum} — draining and exiting")
    _shutdown = True


def _wait_for_deps() -> tuple[psycopg.Connection, redis.Redis]:
    log.info("connecting to postgres...")
    for attempt in range(60):
        try:
            conn = psycopg.connect(DATABASE_URL, autocommit=True)
            break
        except Exception as exc:
            if attempt == 59:
                raise
            if attempt % 5 == 0:
                log.info(f"  postgres not ready ({type(exc).__name__}); retrying")
            time.sleep(1)

    log.info("connecting to redis...")
    for attempt in range(60):
        try:
            r = redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()
            break
        except Exception as exc:
            if attempt == 59:
                raise
            if attempt % 5 == 0:
                log.info(f"  redis not ready ({type(exc).__name__}); retrying")
            time.sleep(1)
    return conn, r


def _process_event(conn: psycopg.Connection, event: dict) -> None:
    code = event.get("code")
    if not code:
        log.warning(f"skipping event with no code: {event!r}")
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE urls SET clicks = clicks + 1 WHERE code = %s",
            (code,),
        )
        affected = cur.rowcount
    if affected:
        log.info(f"tick: code={code} clicks+=1 at={event.get('at')}")
    else:
        log.info(f"tick: code={code} (unknown code, no row updated)")


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn, r = _wait_for_deps()
    log.info(f"worker ready, waiting on queue '{CLICK_QUEUE}'")

    while not _shutdown:
        try:
            popped = r.blpop([CLICK_QUEUE], timeout=2)
            if popped is None:
                continue
            _, payload = popped
            event = json.loads(payload)
            _process_event(conn, event)
        except redis.ConnectionError as exc:
            log.warning(f"redis dropped ({exc}); reconnecting")
            time.sleep(1)
            r = redis.from_url(REDIS_URL, decode_responses=True)
        except Exception as exc:
            log.exception(f"unhandled: {exc}")
            time.sleep(1)

    log.info("exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
