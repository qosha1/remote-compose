"""Shared config exception type.

Lives in its own module so ``_schema_types`` and ``_network_types`` can both
raise it without importing each other. ``_schema_types`` re-exports
``ConfigError`` so the historical import path
(``from remote_compose.config._schema_types import ConfigError``) keeps working.
"""

from __future__ import annotations


class ConfigError(ValueError):
    """Raised when an rc.yml v2 document fails validation."""
