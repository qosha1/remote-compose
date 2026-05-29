"""Static allowlist of EC2 instance types supported by `rc dev`.

Maps each instance type name to its CPU architecture so DevHostService can
pick the matching AL2023 AMI from the catalog. Maintained as a static map
rather than a live ec2:DescribeInstanceTypes call: faster, deterministic,
testable. Refresh this file when new families are needed.
"""

from __future__ import annotations

from typing import Literal

from ..exceptions import ValidationError

Arch = Literal["arm64", "x86_64"]


KNOWN_INSTANCE_TYPES: dict[str, Arch] = {
    # Graviton (ARM) — preferred default for cost
    "t4g.nano": "arm64",
    "t4g.micro": "arm64",
    "t4g.small": "arm64",
    "t4g.medium": "arm64",
    "t4g.large": "arm64",
    "t4g.xlarge": "arm64",
    "t4g.2xlarge": "arm64",
    "m6g.medium": "arm64",
    "m6g.large": "arm64",
    "m6g.xlarge": "arm64",
    "m6g.2xlarge": "arm64",
    "c6g.medium": "arm64",
    "c6g.large": "arm64",
    "c6g.xlarge": "arm64",
    # Intel/AMD x86_64 — for images without ARM builds
    "t3.nano": "x86_64",
    "t3.micro": "x86_64",
    "t3.small": "x86_64",
    "t3.medium": "x86_64",
    "t3.large": "x86_64",
    "t3.xlarge": "x86_64",
    "t3.2xlarge": "x86_64",
    "m5.large": "x86_64",
    "m5.xlarge": "x86_64",
    "m5.2xlarge": "x86_64",
    "c5.large": "x86_64",
    "c5.xlarge": "x86_64",
}


def get_arch(instance_type: str) -> Arch:
    """Return the CPU architecture for `instance_type`.

    Raises ValidationError if the type is not in the known allowlist.
    Add new entries to KNOWN_INSTANCE_TYPES to extend support.
    """
    arch = KNOWN_INSTANCE_TYPES.get(instance_type)
    if arch is None:
        raise ValidationError(
            f"Unknown EC2 instance type: {instance_type!r}. "
            f"Supported types: {sorted(KNOWN_INSTANCE_TYPES.keys())}"
        )
    return arch
