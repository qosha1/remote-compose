"""Shared polling utility for AWS resource wait operations."""
import time
import logging

logger = logging.getLogger(__name__)


def poll_until(check_fn, timeout, interval=10, timeout_msg='Operation timed out', on_poll=None):
    """Poll check_fn until it returns a truthy value or timeout is reached.

    Args:
        check_fn: Callable that returns truthy when condition is met, or raises on error.
        timeout: Maximum seconds to wait.
        interval: Seconds between polls.
        timeout_msg: Message for TimeoutError if timeout exceeded.
        on_poll: Optional callback(elapsed_seconds) called each poll cycle.

    Returns:
        The truthy return value from check_fn.

    Raises:
        TimeoutError: If timeout exceeded.
    """
    start = time.time()
    while True:
        result = check_fn()
        if result:
            return result
        elapsed = time.time() - start
        if elapsed >= timeout:
            raise TimeoutError(timeout_msg)
        if on_poll:
            on_poll(elapsed)
        time.sleep(interval)
