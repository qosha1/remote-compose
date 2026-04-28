"""Project-wide defaults.

Single source of truth for values that used to be duplicated across
modules — when one of these changes, edit it here, not in five
different files.
"""

from __future__ import annotations


# Default VPC CIDR block for new ECS stacks. Picked to be roomy enough
# for two public + two private /24 subnets via cidrsubnet() (~512
# addresses each) without colliding with common private-network ranges.
# Override per stack via rc.yml provider_config.ecs.vpc_cidr.
VPC_CIDR_DEFAULT = "10.0.0.0/16"
