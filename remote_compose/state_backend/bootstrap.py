"""Idempotent bootstrap of the S3 bucket + DynamoDB lock table that hold
remote terraform state.

`rc init --remote-backend` calls these so the user's first `rc deploy`
works without a second manual step. Both functions are safe to re-run —
existing-resource exceptions are no-ops.

Naming defaults (per remote-compose-5h8.5 strategy):

  Bucket: ``<account_id>-rc-tfstate``  (one per AWS account, key-scoped per project)
  Lock table: ``rc-tfstate-locks``     (one per account, shared across projects)
"""

from __future__ import annotations

from typing import Any, Optional


class BucketOwnershipError(Exception):
    """Raised when the bucket exists but isn't owned by the calling account.

    Surfaced when boto3.s3.head_bucket returns 403 — common when an
    earlier rc setup ran under a different AWS account and the bucket
    name happens to collide. The recovery is to either pick a different
    naming scheme or have the bucket-owning account grant access.
    """


def discover_account_id(session: Optional[Any] = None) -> str:
    """Return the AWS account id of the calling identity via STS.

    Lazy boto3 import so this module is importable without the [ecs]
    extra installed.
    """
    if session is None:
        import boto3

        session = boto3.Session()
    sts = session.client("sts")
    return sts.get_caller_identity()["Account"]


def bootstrap_bucket(
    account_id: str,
    region: str,
    session: Optional[Any] = None,
    *,
    name: Optional[str] = None,
) -> str:
    """Create the rc-tfstate S3 bucket if missing; otherwise no-op.

    Args:
        account_id: AWS account id (used to derive default bucket name
            ``<account_id>-rc-tfstate``).
        region: AWS region for both the bucket location and the boto3
            client.
        session: Optional boto3 Session. When None, constructs one from
            ambient creds.
        name: Override default bucket name (mostly for testing).

    Returns:
        The bucket name (created or pre-existing).

    Raises:
        BucketOwnershipError: bucket exists but in another account.
    """
    if session is None:
        import boto3

        session = boto3.Session(region_name=region)

    bucket_name = name or f"{account_id}-rc-tfstate"
    s3 = session.client("s3", region_name=region)

    # Probe: does the bucket exist + do we own it?
    try:
        s3.head_bucket(Bucket=bucket_name)
        # 200 OK → exists + we own it. No-op.
        return bucket_name
    except Exception as exc:  # noqa: BLE001
        # ClientError carries response['Error']['Code']. We accept any
        # mock-like object that has the same shape, plus the real boto3
        # ClientError.
        code = _client_error_code(exc)
        if code == "403":
            raise BucketOwnershipError(
                f"S3 bucket {bucket_name!r} exists but is not owned by "
                f"account {account_id}. Either grant the calling account "
                f"access or use a different account_id."
            )
        if code != "404":
            # Some other error: NoSuchBucket on us-east-1 returns a
            # different shape. Surface it.
            raise
        # 404: bucket doesn't exist. Create it.

    # us-east-1 quirk: CreateBucket rejects LocationConstraint=us-east-1.
    # Every other region requires it.
    create_kwargs: dict[str, Any] = {"Bucket": bucket_name}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": region,
        }
    s3.create_bucket(**create_kwargs)

    s3.put_bucket_versioning(
        Bucket=bucket_name,
        VersioningConfiguration={"Status": "Enabled"},
    )
    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256",
                    },
                },
            ],
        },
    )
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    return bucket_name


def bootstrap_lock_table(
    region: str,
    session: Optional[Any] = None,
    *,
    name: str = "rc-tfstate-locks",
) -> str:
    """Create the DynamoDB lock table if missing; otherwise no-op.

    Schema is fixed by terraform's s3 backend contract: hash_key must be
    ``LockID``. Billing mode PAY_PER_REQUEST means near-zero cost at our
    deploy-frequency.

    Args:
        region: AWS region.
        session: Optional boto3 Session.
        name: Override default table name.

    Returns:
        The table name (created or pre-existing).
    """
    if session is None:
        import boto3

        session = boto3.Session(region_name=region)

    ddb = session.client("dynamodb", region_name=region)

    try:
        ddb.describe_table(TableName=name)
        # Exists. No-op.
        return name
    except Exception as exc:  # noqa: BLE001
        if _client_error_code(exc) != "ResourceNotFoundException":
            raise
        # Doesn't exist; create it.

    ddb.create_table(
        TableName=name,
        AttributeDefinitions=[
            {"AttributeName": "LockID", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "LockID", "KeyType": "HASH"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Wait until the table is ACTIVE before returning so callers can
    # immediately configure terraform to point at it.
    waiter = ddb.get_waiter("table_exists")
    waiter.wait(TableName=name)
    return name


def _client_error_code(exc: BaseException) -> Optional[str]:
    """Extract the boto3 ClientError code from an exception, or None.

    Tolerant of MagicMock-shaped errors used in tests (which have a
    ``response`` attribute but aren't real ClientError instances).
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    err = response.get("Error") or {}
    return err.get("Code")
