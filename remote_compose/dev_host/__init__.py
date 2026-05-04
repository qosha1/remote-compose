"""rc dev — generic EC2 dev-host with pluggable source bootstrap.

Lives outside `remote_compose.services` because the CLI must be usable
without Django configured (services/__init__.py imports Django ORM at
package-import time). This package is Django-free.
"""

from .bootstrap import (
    GitSource,
    ImageSource,
    LocalSource,
    ScriptSource,
    SourceSpec,
    detect_source_from_cwd,
    source_from_dict,
)
from .service import DevHostRecord, DevHostService, FilesystemKeyStore

__all__ = [
    "DevHostService",
    "DevHostRecord",
    "FilesystemKeyStore",
    "GitSource",
    "ImageSource",
    "LocalSource",
    "ScriptSource",
    "SourceSpec",
    "detect_source_from_cwd",
    "source_from_dict",
]
