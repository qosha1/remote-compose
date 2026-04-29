"""rc.yml v2 portable terraform state — bootstrap + adopt.

Two pieces:

  bootstrap.py  Idempotent S3 bucket + DynamoDB lock-table creation.
                Used by `rc init --remote-backend` to make the very first
                `rc deploy` work from any box.

  adopt.py      Walk live AWS via boto3, generate terraform import
                addresses (reuses v1_migrate), populate state in the
                configured backend. Closes the v1→v2 cutover gap where
                no terraform state exists yet.

The point: a fresh laptop + git clone + `rc deploy` = green stack, with
state shared via S3 + lock-table coordination.
"""

from .bootstrap import (
    BucketOwnershipError,
    bootstrap_bucket,
    bootstrap_lock_table,
    discover_account_id,
)

__all__ = [
    "BucketOwnershipError",
    "bootstrap_bucket",
    "bootstrap_lock_table",
    "discover_account_id",
]
