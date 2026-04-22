"""rc.yml v1 loader — just enough to feed the migrator.

v1 is the legacy flat schema used by remote_compose prior to multi-provider
support. This module does not re-implement validation; it only loads the raw
dict and recognizes which fields will be migrated by migrate.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

V1_TOP_LEVEL_KEYS = {
    "cluster",
    "region",
    "aws_profile",
    "compose_file",
    "project_name",
    "vpc_cidr",
    "domain",
    "secrets",
    "backup",
    "services",
}

V1_SERVICE_KEYS = {
    "cpu",
    "memory",
    "type",
    "health_check_path",
    "ephemeral_storage",
    "public",
    "port",
    "default_target",
}


def load(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"rc.yml v1 must be a mapping, got {type(raw).__name__}"
        )
    return raw


def is_v1(raw: dict[str, Any]) -> bool:
    """Heuristic: v2 declares `version: 2`; anything else is v1-ish."""
    return int(raw.get("version", 0)) != 2
