"""EFS access-point defaults are safe (remote-compose-29w).

Earlier defaults: UID=0 / GID=0 / perms=0777 — root-and-world-writable
for every access point unless the caller explicitly overrode. Means
any container in the cluster could write to any EFS-backed mount as
root with no group/other restrictions. Compounded by a docstring that
LIED, claiming the defaults were 1000/1000/0755 (so reviewers reading
``create_access_point`` would believe the safe values were already in
effect).

Fix: defaults flipped to 1000/1000/0755. Permissive 0/0/0777 are
exposed as a separate set of opt-in constants for the rare image that
needs them.
"""

from __future__ import annotations


from remote_compose.services.efs_service import EFSService


class TestSafeDefaultsAreInEffect:
    def test_default_uid_is_1000(self):
        assert EFSService.DEFAULT_UID == 1000

    def test_default_gid_is_1000(self):
        assert EFSService.DEFAULT_GID == 1000

    def test_default_permissions_are_0755(self):
        assert EFSService.DEFAULT_PERMISSIONS == "0755"


class TestPermissiveOptInIsAvailable:
    """Legacy callers that NEED root-writable behavior should still
    have a clear, named path to opt in. The constants are explicit so
    they show up in code review."""

    def test_permissive_uid_is_root(self):
        assert EFSService.PERMISSIVE_UID == 0

    def test_permissive_gid_is_root(self):
        assert EFSService.PERMISSIVE_GID == 0

    def test_permissive_permissions_are_world_writable(self):
        assert EFSService.PERMISSIVE_PERMISSIONS == "0777"


class TestCreateAccessPointSignatureMatchesDefaults:
    """The function-default kwargs use the class constants. If someone
    flips DEFAULT_* back to 0/0/0777 in the future, the docstring's
    documented behavior will diverge from runtime — better to lock the
    relationship in test."""

    def test_create_access_point_uses_default_uid_kwarg(self):
        import inspect

        sig = inspect.signature(EFSService.create_access_point)
        assert sig.parameters["uid"].default == EFSService.DEFAULT_UID
        assert sig.parameters["gid"].default == EFSService.DEFAULT_GID
        assert sig.parameters["permissions"].default == EFSService.DEFAULT_PERMISSIONS
