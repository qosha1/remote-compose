"""AWS Copilot → rc.yml v2 migration tool.

Copilot reaches end-of-support on 2026-06-12. This package reads any
copilot/ directory tree (services, environments, addons, pipelines)
and emits a working rc.yml v2 + docker-compose.yml the user can run
through `rc deploy` immediately.

Public surface:
    discover(path)  -> CopilotApp        (parser, copilot.discover)
    translate(app)  -> rc.yml + compose  (translators, copilot.translate.*)
"""

from .discover import (
    CopilotAddon,
    CopilotApp,
    CopilotEnvironment,
    CopilotService,
    DiscoveryError,
    discover,
)

__all__ = [
    "CopilotAddon",
    "CopilotApp",
    "CopilotEnvironment",
    "CopilotService",
    "DiscoveryError",
    "discover",
]
