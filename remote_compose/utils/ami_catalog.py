"""AL2023 AMI ID catalog for `rc dev` EC2 dev-hosts.

Static map of (region, arch) -> AL2023 AMI ID. Refresh quarterly via the
AWS SSM parameter store. Run `bin/refresh-ami-catalog.sh` (TODO) or by hand:

    for r in us-east-1 us-east-2 us-west-1 us-west-2 eu-west-1; do
      aws ssm get-parameter --region $r \\
        --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64
      aws ssm get-parameter --region $r \\
        --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64
    done

A live SSM lookup at deploy time is intentionally avoided here: it adds an AWS
API call to every `rc dev up`, and Amazon publishes new AMIs more often than
rc needs to track. Pinning gives deterministic deploys.
"""

from __future__ import annotations

from .ec2_instance_types import Arch
from ..exceptions import ValidationError


# Snapshot taken 2026-05-03 from SSM /aws/service/ami-amazon-linux-latest/.
# Refresh quarterly.
AMI_CATALOG: dict[str, dict[Arch, str]] = {
    "us-east-1": {
        "arm64": "ami-0d6fc8f787cd9a417",
        "x86_64": "ami-0ed094fb1304fd857",
    },
    "us-east-2": {
        "arm64": "ami-0857189c7373017e0",
        "x86_64": "ami-018d49b53eee64386",
    },
    "us-west-1": {
        "arm64": "ami-085c59cb28c95e47a",
        "x86_64": "ami-0a21b93c10617c1a5",
    },
    "us-west-2": {
        "arm64": "ami-08282034f0f6175a3",
        "x86_64": "ami-09667c8f5c7c258a2",
    },
    "eu-west-1": {
        "arm64": "ami-00602d9e13dfdc4bd",
        "x86_64": "ami-0841e304792db6a16",
    },
}


def get_ami_id(region: str, arch: Arch) -> str:
    """Return the AL2023 AMI ID for the given region and architecture.

    Raises ValidationError if the region isn't in the catalog. Add a new
    entry to AMI_CATALOG above to support a new region.
    """
    region_map = AMI_CATALOG.get(region)
    if region_map is None:
        raise ValidationError(
            f"No AL2023 AMI catalogued for region {region!r}. "
            f"Supported regions: {sorted(AMI_CATALOG.keys())}"
        )
    return region_map[arch]
