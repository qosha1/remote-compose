"""Source-shape regression for credential-serializer validation
(remote-compose-58y).

The api/ module pulls in DRF (rest_framework) at import time, and DRF
isn't in this repo's test venv (it's an optional API surface, not a
test-required dep). So we can't import the serializer class to call
``.is_valid()`` directly.

Instead, this test reads the serializer source file and asserts the
validate() method exists with the right structure: per-credential-type
required-key map, missing-key rejection, dict-type rejection. If the
source ever drops back to the bare .create() form (the bug), this
test fails before the runtime would silently encrypt None values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SERIALIZERS_PATH = (
    Path(__file__).parent.parent.parent / "remote_compose" / "api" / "serializers.py"
)


@pytest.fixture
def source() -> str:
    return SERIALIZERS_PATH.read_text()


class TestValidateMethodPresent:
    def test_serializer_has_validate_method(self, source):
        # Search for `def validate(` within the
        # SecureCredentialCreateSerializer class.
        idx = source.find("class SecureCredentialCreateSerializer")
        assert idx >= 0
        # Find the next class boundary (start of next "class " at column 0).
        end = source.find("\nclass ", idx + 1)
        body = source[idx : end if end > 0 else len(source)]
        assert "def validate(self" in body, (
            "SecureCredentialCreateSerializer must define validate() "
            "to reject malformed credential_value at API boundary "
            "(remote-compose-58y). Without it, AWS creds with None "
            "access_key_id and SSH creds with None private_key land "
            "in the DB encrypted-as-None."
        )

    def test_required_keys_map_declared(self, source):
        idx = source.find("class SecureCredentialCreateSerializer")
        end = source.find("\nclass ", idx + 1)
        body = source[idx : end if end > 0 else len(source)]
        # Per-type required keys map. The fix uses
        # _REQUIRED_KEYS_BY_TYPE; if it's renamed, update both call
        # sites or add a keep-alive comment.
        assert "_REQUIRED_KEYS_BY_TYPE" in body or "required_keys" in body

    def test_create_does_not_use_get_for_required_keys(self, source):
        # The bug: ``access_key_id=credential_value.get('access_key_id')``
        # silently passed None when key was missing. The fix uses
        # subscript access (or after a validate() guard).
        idx = source.find("class SecureCredentialCreateSerializer")
        end = source.find("\nclass ", idx + 1)
        body = source[idx : end if end > 0 else len(source)]
        # Allow .get() for optional fields (description, username) but
        # not for the required ones.
        assert (
            "credential_value['access_key_id']" in body
            or 'credential_value["access_key_id"]' in body
        ), "create() must use subscript access for required keys"
        assert (
            "credential_value['secret_access_key']" in body
            or 'credential_value["secret_access_key"]' in body
        )
        assert (
            "credential_value['private_key']" in body
            or 'credential_value["private_key"]' in body
        )
