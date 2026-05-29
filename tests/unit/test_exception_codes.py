"""Every exception class carries a default code in its declared range
(remote-compose-t8j).

Earlier behavior: ranges (1xxx validation, 2xxx connection, ...) were
documented but no class assigned a default ``code`` attribute. Every
raised instance had ``code=None`` so the documented system was unused
infrastructure.

Fix: each subclass declares ``code = NNNN`` as a class attribute; the
base class __init__ falls back to it when ``code=`` isn't passed
explicitly. These tests verify each class's code is in its documented
range.
"""

from __future__ import annotations

import pytest

from remote_compose import exceptions as ex

# ---------------------------------------------------------------------------
# Range expectations: (class, expected_range)
# ---------------------------------------------------------------------------


_EXPECTATIONS = [
    # 1xxx: Validation
    (ex.ValidationError, (1000, 1999)),
    (ex.ConfigurationError, (1000, 1999)),
    # 2xxx: Connection
    (ex.RemoteConnectionError, (2000, 2999)),
    (ex.SSHConnectionError, (2000, 2999)),
    (ex.SSHAuthenticationError, (2000, 2999)),
    (ex.SSHTimeoutError, (2000, 2999)),
    (ex.SSHHostKeyError, (2000, 2999)),
    # 3xxx: Docker
    (ex.DockerError, (3000, 3999)),
    (ex.DockerContextError, (3000, 3999)),
    (ex.DockerComposeError, (3000, 3999)),
    (ex.ComposeFileError, (3000, 3999)),
    # 4xxx: Deployment
    (ex.DeploymentError, (4000, 4999)),
    (ex.DeploymentTimeoutError, (4000, 4999)),
    (ex.RollbackError, (4000, 4999)),
    (ex.DeploymentInProgressError, (4000, 4999)),
    # 5xxx: Credential
    (ex.CredentialError, (5000, 5999)),
    (ex.EncryptionError, (5000, 5999)),
    # 6xxx: AWS generic
    (ex.AWSError, (6000, 6999)),
    (ex.EC2Error, (6000, 6999)),
    (ex.AWSCredentialError, (6000, 6999)),
    # 7xxx: ECS
    (ex.ECSError, (7000, 7999)),
    (ex.ECSClusterError, (7000, 7999)),
    (ex.ECSClusterNotFoundError, (7000, 7999)),
    (ex.ECSServiceError, (7000, 7999)),
    (ex.ECSServiceNotFoundError, (7000, 7999)),
    (ex.ECSTaskDefinitionError, (7000, 7999)),
    (ex.ECSTaskError, (7000, 7999)),
    (ex.ECSDeploymentError, (7000, 7999)),
    (ex.ECSDeploymentTimeoutError, (7000, 7999)),
    (ex.ComposeConversionError, (7000, 7999)),
    # 8xxx: ECR
    (ex.ECRError, (8000, 8999)),
    (ex.ECRRepositoryError, (8000, 8999)),
    (ex.ECRAuthenticationError, (8000, 8999)),
    (ex.ECRImageError, (8000, 8999)),
    # 9xxx: EFS
    (ex.EFSError, (9000, 9999)),
    (ex.EFSFileSystemError, (9000, 9999)),
    (ex.EFSAccessPointError, (9000, 9999)),
    (ex.EFSMountTargetError, (9000, 9999)),
]


@pytest.mark.parametrize("cls,expected_range", _EXPECTATIONS)
def test_class_has_code_in_documented_range(cls, expected_range):
    low, high = expected_range
    assert cls.code is not None, (
        f"{cls.__name__}: class-level ``code`` attribute is None — "
        f"every subclass should declare a code in {expected_range} "
        f"so raised instances carry a stable identifier "
        f"(remote-compose-t8j)."
    )
    assert low <= cls.code <= high, (
        f"{cls.__name__}: code {cls.code} is outside documented "
        f"range {expected_range}. If the class belongs in a different "
        f"range, update either the code or the range docs in "
        f"exceptions.py module docstring."
    )


# ---------------------------------------------------------------------------
# Codes are unique within a class hierarchy (no two siblings collide)
# ---------------------------------------------------------------------------


def test_codes_are_unique_across_all_classes():
    seen: dict[int, str] = {}
    for cls, _ in _EXPECTATIONS:
        if cls.code in seen:
            pytest.fail(
                f"{cls.__name__} and {seen[cls.code]} both use code "
                f"{cls.code} — assign a unique code per class."
            )
        seen[cls.code] = cls.__name__


# ---------------------------------------------------------------------------
# Instance picks up class default when code= not passed
# ---------------------------------------------------------------------------


class TestInstanceInheritsClassCode:
    def test_no_code_kwarg_falls_back_to_class_default(self):
        e = ex.ECSClusterError("oops")
        assert e.code == ex.ECSClusterError.code
        assert "[7001]" in str(e)

    def test_explicit_code_kwarg_overrides_class_default(self):
        e = ex.ECSClusterError("oops", code=99999)
        assert e.code == 99999

    def test_str_omits_code_when_none(self):
        # Base RemoteComposeError still has code=None as a class default.
        e = ex.RemoteComposeError("naked")
        assert e.code is None
        assert str(e) == "naked"
