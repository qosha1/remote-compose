"""
Base service class with common functionality.
"""

import logging
from contextlib import contextmanager
from typing import List, Callable

from django.db import transaction


class BaseService:
    """
    Abstract base service providing common functionality.
    """

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self._observers: List[Callable] = []

    def attach_observer(self, observer: Callable):
        """Attach an observer to receive event notifications."""
        self._observers.append(observer)

    def detach_observer(self, observer: Callable):
        """Detach an observer."""
        if observer in self._observers:
            self._observers.remove(observer)

    def notify_observers(self, event_type: str, **kwargs):
        """Notify all attached observers of an event."""
        for observer in self._observers:
            try:
                observer(event_type, **kwargs)
            except Exception as e:
                self.logger.error(f"Observer notification failed: {e}")

    @contextmanager
    def atomic_transaction(self):
        """Context manager for database transactions."""
        with transaction.atomic():
            yield

    def log_info(self, message: str, **extra):
        """Log an info message with optional extra context."""
        self.logger.info(message, extra=extra)

    def log_error(self, message: str, exc_info=False, **extra):
        """Log an error message with optional extra context."""
        self.logger.error(message, exc_info=exc_info, extra=extra)

    def log_warning(self, message: str, **extra):
        """Log a warning message with optional extra context."""
        self.logger.warning(message, extra=extra)

    def log_debug(self, message: str, **extra):
        """Log a debug message with optional extra context."""
        self.logger.debug(message, extra=extra)
