"""Failing unit tests for state_backend.bootstrap (5h8.3 RED gate).

Cover:
  c. bootstrap_bucket idempotent (BucketAlreadyOwnedByYou → no-op)
  d. bootstrap_lock_table idempotent (ResourceInUseException → no-op)

Plus extra coverage:
  - bucket name defaults to <account_id>-rc-tfstate
  - bucket created with versioning + SSE + public-access-block
  - lock table created with hash_key=LockID, on-demand billing
  - cross-account refusal: bucket exists but owned elsewhere → AccessDenied surfaces clearly
"""

from __future__ import annotations

from unittest import mock

import pytest


# These imports MUST fail today — state_backend doesn't exist yet.
# Each test re-imports lazily to surface the right error per test.


class TestBootstrapBucket:
    def test_creates_bucket_with_versioning_sse_and_pab(self):
        """First-call path: creates bucket, enables versioning, sets SSE-S3,
        sets public-access-block on all 4 controls."""
        from remote_compose.state_backend.bootstrap import bootstrap_bucket

        s3 = mock.MagicMock()
        # head_bucket raises 404 on first call (bucket doesn't exist yet)
        from botocore.exceptions import ClientError
        s3.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadBucket",
        )
        session = mock.MagicMock()
        session.client.return_value = s3

        name = bootstrap_bucket(
            account_id="033937118837",
            region="us-west-2",
            session=session,
        )

        assert name == "033937118837-rc-tfstate"
        s3.create_bucket.assert_called_once()
        s3.put_bucket_versioning.assert_called_once()
        s3.put_bucket_encryption.assert_called_once()
        s3.put_public_access_block.assert_called_once()
        # PAB args: BlockPublicAcls / IgnorePublicAcls / BlockPublicPolicy /
        # RestrictPublicBuckets all True.
        pab = s3.put_public_access_block.call_args.kwargs[
            "PublicAccessBlockConfiguration"
        ]
        assert pab == {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }

    def test_idempotent_on_already_owned(self):
        """Second-call path: bucket already exists in our account → no-op,
        no create_bucket call, returns same name."""
        from remote_compose.state_backend.bootstrap import bootstrap_bucket

        s3 = mock.MagicMock()
        # head_bucket succeeds → bucket exists + we own it
        s3.head_bucket.return_value = {}
        session = mock.MagicMock()
        session.client.return_value = s3

        name = bootstrap_bucket(
            account_id="033937118837",
            region="us-west-2",
            session=session,
        )

        assert name == "033937118837-rc-tfstate"
        s3.create_bucket.assert_not_called()

    def test_cross_account_collision_surfaces_clear_error(self):
        """Bucket exists but in another AWS account → head_bucket raises
        403; bootstrap must raise a clear error (not silently succeed)."""
        from remote_compose.state_backend.bootstrap import (
            BucketOwnershipError, bootstrap_bucket,
        )
        from botocore.exceptions import ClientError

        s3 = mock.MagicMock()
        s3.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}},
            "HeadBucket",
        )
        session = mock.MagicMock()
        session.client.return_value = s3

        with pytest.raises(BucketOwnershipError, match="not owned by"):
            bootstrap_bucket(
                account_id="033937118837",
                region="us-west-2",
                session=session,
            )
        s3.create_bucket.assert_not_called()


class TestBootstrapLockTable:
    def test_creates_lock_table_with_correct_schema(self):
        """First-call path: table doesn't exist → CreateTable with
        hash_key=LockID, billing=PAY_PER_REQUEST."""
        from remote_compose.state_backend.bootstrap import bootstrap_lock_table

        ddb = mock.MagicMock()
        from botocore.exceptions import ClientError
        ddb.describe_table.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "DescribeTable",
        )
        session = mock.MagicMock()
        session.client.return_value = ddb

        name = bootstrap_lock_table(region="us-west-2", session=session)

        assert name == "rc-tfstate-locks"
        ddb.create_table.assert_called_once()
        kwargs = ddb.create_table.call_args.kwargs
        assert kwargs["TableName"] == "rc-tfstate-locks"
        assert kwargs["BillingMode"] == "PAY_PER_REQUEST"
        # Hash key must be 'LockID' — that's what terraform's s3 backend writes.
        assert {"AttributeName": "LockID", "KeyType": "HASH"} in kwargs["KeySchema"]
        assert {"AttributeName": "LockID", "AttributeType": "S"} in kwargs["AttributeDefinitions"]

    def test_idempotent_on_existing_table(self):
        """Second-call path: describe_table returns the table → no-op,
        no create_table, returns same name."""
        from remote_compose.state_backend.bootstrap import bootstrap_lock_table

        ddb = mock.MagicMock()
        ddb.describe_table.return_value = {
            "Table": {"TableStatus": "ACTIVE", "TableName": "rc-tfstate-locks"},
        }
        session = mock.MagicMock()
        session.client.return_value = ddb

        name = bootstrap_lock_table(region="us-west-2", session=session)

        assert name == "rc-tfstate-locks"
        ddb.create_table.assert_not_called()
