"""Verify remote_compose.exceptions doesn't shadow Python builtins
(remote-compose-7fn).

The earlier ``class ConnectionError(RemoteComposeError)`` shadowed the
Python builtin ``ConnectionError``, so any ``except ConnectionError``
in this codebase silently caught network OS-level errors as if they
were rc-domain errors. The fix renames our class to
``RemoteConnectionError``.
"""

from __future__ import annotations

import builtins


import remote_compose.exceptions as rcx


def test_module_does_not_redefine_builtin_connectionerror():
    # If the module re-exported a `ConnectionError` symbol pointing at
    # something that's NOT the Python builtin, `from remote_compose.
    # exceptions import *` would shadow the builtin in caller scope.
    if not hasattr(rcx, "ConnectionError"):
        return  # cleanest outcome: name doesn't exist on the module
    assert rcx.ConnectionError is builtins.ConnectionError, (
        "remote_compose.exceptions.ConnectionError must be the Python "
        "builtin (remote-compose-7fn), not a custom subclass that "
        "would shadow it on `from ... import *`."
    )


def test_remote_connection_error_exists_and_subclasses_remotecomposeerror():
    assert hasattr(rcx, "RemoteConnectionError")
    assert issubclass(rcx.RemoteConnectionError, rcx.RemoteComposeError)


def test_ssh_errors_chain_via_remote_connection_error():
    # Subclass chain so ``except RemoteConnectionError`` catches every SSH
    # variant — same coverage we had before the rename.
    assert issubclass(rcx.SSHConnectionError, rcx.RemoteConnectionError)
    assert issubclass(rcx.SSHAuthenticationError, rcx.SSHConnectionError)
    assert issubclass(rcx.SSHTimeoutError, rcx.SSHConnectionError)
    assert issubclass(rcx.SSHHostKeyError, rcx.SSHConnectionError)


def test_remote_connection_error_does_not_inherit_from_builtin():
    # Defensive: rc errors are domain errors, not OS-level. They should
    # not silently flow through ``except OSError`` blocks.
    assert not issubclass(rcx.RemoteConnectionError, builtins.ConnectionError)
    assert not issubclass(rcx.RemoteConnectionError, OSError)


def test_star_import_does_not_pull_in_a_shadowing_connectionerror():
    """Cold smoke test: simulate ``from remote_compose.exceptions import *``
    in a fresh namespace. The resulting ConnectionError binding (if any)
    must be the Python builtin."""
    # Drive the same logic ``import *`` follows: respect __all__ if
    # defined, otherwise pull every name not starting with underscore.
    namespace: dict = {}
    all_names = getattr(rcx, "__all__", None)
    if all_names is None:
        all_names = [n for n in dir(rcx) if not n.startswith("_")]
    for name in all_names:
        namespace[name] = getattr(rcx, name)

    if "ConnectionError" in namespace:
        assert namespace["ConnectionError"] is builtins.ConnectionError
