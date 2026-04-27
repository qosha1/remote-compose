"""Bounded retries on Duplicate-error race-condition handlers
(remote-compose-jzp).

Earlier behavior: when create_security_group / create_secret raced
against another caller and AWS returned InvalidGroup.Duplicate /
ResourceExistsException, the handler called itself recursively. If the
find-existing path was broken (permissions, eventual-consistency lag,
mocked test that always returns nothing), the recursion would blow the
Python stack — verified in unit tests by setting up a mock that
doesn't have the SG visible to describe but DOES report duplicate on
create.

Fix replaces recursion with a bounded for-loop. After
``_MAX_DUPLICATE_RETRIES`` (3) iterations the handler raises a clear
provisioning error instead of stack-overflowing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from remote_compose.exceptions import EFSError


def _client_error(code: str, op: str = "Create") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"simulated {code}"}},
        op,
    )


# ---------------------------------------------------------------------------
# EFS get_or_create_efs_security_group
# ---------------------------------------------------------------------------


class TestEfsSgBoundedRetry:
    def _service(self):
        from remote_compose.services.efs_service import EFSService
        # Skip Django factory-style construction; the AWS factory call
        # is the only thing we need to mock.
        svc = EFSService.__new__(EFSService)
        svc._observers = []
        svc.log_info = MagicMock()
        svc.log_warning = MagicMock()
        svc.log_error = MagicMock()
        svc.notify_observers = MagicMock()
        svc.aws_factory = MagicMock()
        return svc

    def test_create_race_recovers_when_find_succeeds_on_retry(self):
        svc = self._service()

        ec2 = MagicMock()
        # First find returns no results; first create raises Duplicate;
        # second find returns the existing SG (race resolved).
        find_results = [
            {"SecurityGroups": []},
            {"SecurityGroups": [{
                "GroupId": "sg-real",
                "GroupName": "test-sg",
                "VpcId": "vpc-1",
                "Description": "found by retry",
            }]},
        ]
        ec2.describe_security_groups.side_effect = find_results
        ec2.create_security_group.side_effect = _client_error(
            "InvalidGroup.Duplicate",
        )
        # Don't actually need create to succeed in this case.

        svc._get_ec2_client = MagicMock(return_value=ec2)
        svc._get_vpc_cidr = MagicMock(return_value="10.0.0.0/16")

        out = svc.get_or_create_efs_security_group(
            vpc_id="vpc-1", name="test-sg",
        )
        assert out["security_group_id"] == "sg-real"
        # find was called twice (initial + retry); create attempted once.
        assert ec2.describe_security_groups.call_count == 2
        assert ec2.create_security_group.call_count == 1

    def test_persistent_create_race_eventually_raises_no_stack_overflow(self):
        svc = self._service()

        ec2 = MagicMock()
        # Both find passes return nothing — simulates the broken-find
        # case (e.g. permission denied to describe, or filter mismatch).
        ec2.describe_security_groups.return_value = {"SecurityGroups": []}
        ec2.create_security_group.side_effect = _client_error(
            "InvalidGroup.Duplicate",
        )
        svc._get_ec2_client = MagicMock(return_value=ec2)
        svc._get_vpc_cidr = MagicMock(return_value="10.0.0.0/16")

        # Old behavior: infinite recursion. New: bounded raise.
        with pytest.raises(EFSError, match="retries"):
            svc.get_or_create_efs_security_group(
                vpc_id="vpc-1", name="test-sg",
            )
        # Bounded at _MAX_DUPLICATE_RETRIES.
        assert ec2.create_security_group.call_count == \
            svc._MAX_DUPLICATE_RETRIES

    def test_non_duplicate_create_error_raises_immediately(self):
        svc = self._service()
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {"SecurityGroups": []}
        ec2.create_security_group.side_effect = _client_error(
            "AccessDenied",
        )
        svc._get_ec2_client = MagicMock(return_value=ec2)
        svc._get_vpc_cidr = MagicMock(return_value="10.0.0.0/16")

        with pytest.raises(EFSError, match="Failed to create"):
            svc.get_or_create_efs_security_group(
                vpc_id="vpc-1", name="test-sg",
            )
        # No retry on non-Duplicate errors — fail-fast.
        assert ec2.create_security_group.call_count == 1
