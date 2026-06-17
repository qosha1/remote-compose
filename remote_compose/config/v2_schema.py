"""rc.yml v2 schema — public entry point.

Splits across two private sub-modules:

  config/_schema_types.py   — dataclasses + per-instance validate() +
                              VALID_* constants + ConfigError + _looks_like_fqdn
  config/_schema_parser.py  — _parse_* helpers + cross-instance validation
                              (e.g. duplicate-hostname detection) + parse()
                              + load()

This file re-exports the public surface so existing imports
(``from remote_compose.config.v2_schema import RcConfigV2, parse, load``)
keep working without churn.

Design intent: NO pydantic, NO heavy dependencies — manual validation
keeps the core package lean (NFR-3). Extras-tolerant where it makes
sense (provider_config sub-keys), strict on everything else.
"""

from __future__ import annotations

from ._schema_parser import (  # noqa: F401  (re-export facade for tests/back-compat)
    _parse_backend,
    _parse_bootstrap,
    _parse_lifecycle,
    _parse_secret,
    _parse_service,
    _parse_terraform,
    load,
    parse,
)
from ._schema_types import (
    VALID_BOOTSTRAP_PERMISSIONS,
    VALID_CAPACITY_TYPES,
    VALID_LAUNCH_TYPES,
    VALID_SECRET_SOURCES,
    VALID_SERVICE_TYPES,
    VALID_TLS_MODES,
    BackupConfig,
    BootstrapConfig,
    ComposeConfig,
    ConfigError,
    GithubOidcDeployRole,
    LifecycleHookV2,
    RcConfigV2,
    SecretRefV2,
    ServiceV2,
    TerraformBackend,
    TerraformConfig,
    TlsConfig,
)

__all__ = [
    # Public types
    "BackupConfig",
    "BootstrapConfig",
    "ComposeConfig",
    "ConfigError",
    "GithubOidcDeployRole",
    "LifecycleHookV2",
    "RcConfigV2",
    "SecretRefV2",
    "ServiceV2",
    "TerraformBackend",
    "TerraformConfig",
    "TlsConfig",
    # Public functions
    "load",
    "parse",
    # Public constants
    "VALID_BOOTSTRAP_PERMISSIONS",
    "VALID_CAPACITY_TYPES",
    "VALID_LAUNCH_TYPES",
    "VALID_SECRET_SOURCES",
    "VALID_SERVICE_TYPES",
    "VALID_TLS_MODES",
]
