"""Tests for EFSService._find_file_system_by_name efficient tag scan
(remote-compose-4rm).

Earlier behavior:
  for fs in describe_file_systems_paginator():
      tags = client.describe_tags(FileSystemId=fs['FileSystemId'])
      ...

Two problems:
1. ``describe_tags`` is deprecated (AWS replaced with
   list_tags_for_resource).
2. N+1 round trips — one extra API call per file system in the account.

Fix: read tags from fs['Tags'] in the existing describe_file_systems
response, drop the extra call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from remote_compose.services.efs_service import EFSService


@pytest.fixture
def svc():
    s = EFSService.__new__(EFSService)
    s._observers = []
    s.log_info = MagicMock()
    s.log_warning = MagicMock()
    s.log_error = MagicMock()
    s.notify_observers = MagicMock()
    s.default_region = "us-west-1"
    return s


def _paginator(pages):
    """Stand-in for boto3 paginator. ``pages`` is a list of
    {'FileSystems': [...]} dicts."""
    p = MagicMock()
    p.paginate.return_value = iter(pages)
    return p


class TestFindByName:
    def test_reads_tags_from_describe_response_no_extra_call(self, svc):
        client = MagicMock()
        client.get_paginator.return_value = _paginator([{
            "FileSystems": [
                {
                    "FileSystemId": "fs-1",
                    "Tags": [{"Key": "Name", "Value": "wanted"}],
                },
            ],
        }])
        # Stub helpers used downstream so they don't call AWS.
        svc._get_efs_client = MagicMock(return_value=client)
        svc._get_mount_target_ids = MagicMock(return_value=[])
        svc._format_file_system = MagicMock(return_value={"id": "fs-1"})

        out = svc._find_file_system_by_name("wanted")
        assert out == {"id": "fs-1"}
        # Crucial: NO describe_tags call (deprecated API).
        assert not client.describe_tags.called

    def test_returns_none_when_no_match(self, svc):
        client = MagicMock()
        client.get_paginator.return_value = _paginator([{
            "FileSystems": [
                {
                    "FileSystemId": "fs-1",
                    "Tags": [{"Key": "Name", "Value": "other"}],
                },
            ],
        }])
        svc._get_efs_client = MagicMock(return_value=client)
        svc._get_mount_target_ids = MagicMock(return_value=[])
        svc._format_file_system = MagicMock(return_value={})

        assert svc._find_file_system_by_name("wanted") is None
        assert not client.describe_tags.called

    def test_handles_missing_tags_block(self, svc):
        # Some EFS responses may omit Tags entirely (untagged systems);
        # the iteration must not crash.
        client = MagicMock()
        client.get_paginator.return_value = _paginator([{
            "FileSystems": [
                {"FileSystemId": "fs-untagged"},
            ],
        }])
        svc._get_efs_client = MagicMock(return_value=client)
        svc._get_mount_target_ids = MagicMock(return_value=[])
        svc._format_file_system = MagicMock(return_value={})

        assert svc._find_file_system_by_name("wanted") is None

    def test_walks_multiple_pages(self, svc):
        client = MagicMock()
        client.get_paginator.return_value = _paginator([
            {"FileSystems": [
                {"FileSystemId": "fs-a",
                 "Tags": [{"Key": "Name", "Value": "miss-1"}]},
            ]},
            {"FileSystems": [
                {"FileSystemId": "fs-b",
                 "Tags": [{"Key": "Name", "Value": "wanted"}]},
            ]},
        ])
        svc._get_efs_client = MagicMock(return_value=client)
        svc._get_mount_target_ids = MagicMock(return_value=[])
        svc._format_file_system = MagicMock(return_value={"id": "fs-b"})

        out = svc._find_file_system_by_name("wanted")
        assert out == {"id": "fs-b"}
        # Still no describe_tags across multiple pages.
        assert not client.describe_tags.called
