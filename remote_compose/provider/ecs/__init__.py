"""AWS ECS provider."""

from .. import register
from .provider import ECSProvider

register("ecs", ECSProvider)

__all__ = ["ECSProvider"]
