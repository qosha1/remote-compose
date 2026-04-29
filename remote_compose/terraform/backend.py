"""Emit ``terraform { backend "..." {...} }`` blocks from rc.yml config.

Each backend type has its own required/optional fields; rc.yml validation
(in ``config.v2_schema``) ensures the shape is right before this is called.
"""

from __future__ import annotations

from typing import Any


class UnsupportedBackendError(ValueError):
    """Raised when a backend type is not recognized."""


_BACKEND_FIELDS: dict[str, list[str]] = {
    "local": ["path"],
    "s3": ["bucket", "key", "region", "dynamodb_table", "encrypt", "profile"],
    "gcs": ["bucket", "prefix", "credentials"],
    "azurerm": [
        "storage_account_name", "container_name", "key",
        "resource_group_name", "subscription_id",
    ],
    "remote": ["hostname", "organization", "workspaces"],
}


def render_backend_block(backend_config: dict[str, Any]) -> str:
    """Render a terraform backend block as HCL.

    Returns a string like::

        terraform {
          backend "s3" {
            bucket         = "..."
            key            = "..."
            region         = "..."
            dynamodb_table = "..."
          }
        }
    """
    btype = backend_config.get("type", "local")
    if btype not in _BACKEND_FIELDS:
        raise UnsupportedBackendError(
            f"terraform backend type {btype!r} is not supported; "
            f"known: {sorted(_BACKEND_FIELDS.keys())}"
        )

    fields = _BACKEND_FIELDS[btype]
    lines = ["terraform {", f'  backend "{btype}" {{']
    max_key = max((len(k) for k in fields if backend_config.get(k) is not None), default=0)
    for key in fields:
        value = backend_config.get(key)
        if value is None:
            continue
        lines.append(f'    {key.ljust(max_key)} = {_hcl_literal(value)}')
    for key, value in (backend_config.get("extra") or {}).items():
        lines.append(f'    {key.ljust(max_key)} = {_hcl_literal(value)}')
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _hcl_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, dict):
        inner = ", ".join(f'{k} = {_hcl_literal(v)}' for k, v in value.items())
        return "{ " + inner + " }"
    if isinstance(value, list):
        inner = ", ".join(_hcl_literal(v) for v in value)
        return "[" + inner + "]"
    raise TypeError(f"cannot render HCL literal for {type(value).__name__}")
