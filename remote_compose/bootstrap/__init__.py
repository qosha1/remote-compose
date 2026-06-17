"""Committed GitHub-OIDC CI deploy-role stack (rc-kiz).

The CI/bootstrap IAM — the role CI assumes via OIDC to trigger deploys — is not a
per-service runtime resource, so it does not belong in the regenerated workload
stack (``deploy/<project>/terraform/``). This package emits it into a separate,
COMMITTED stack with its own terraform state, derived from the ``bootstrap:``
section of rc.yml.

``build`` holds pure, deterministic helpers (interpolation + IAM derivation, no AWS
calls). ``emit`` renders the committed stack from a parsed config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .emit import emit_bootstrap_stack


def __getattr__(name: str):
    # Lazy re-export so importing `build` (pure, dependency-light) doesn't pull
    # in the Jinja2 emitter.
    if name == "emit_bootstrap_stack":
        from .emit import emit_bootstrap_stack

        return emit_bootstrap_stack
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["emit_bootstrap_stack"]
