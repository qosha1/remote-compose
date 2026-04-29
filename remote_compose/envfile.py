"""Parse docker-compose / django-style .env files.

Standalone, no project dependencies. Used by the ECS provider at
emit-time to learn which KEY names live in a file-sourced secret (so
task definitions can map one ECS secret entry per key), and by `rc
secrets push` at upload-time to turn a file into a JSON blob for
Secrets Manager.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Union


_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(ValueError):
    """Raised on unparseable env files or invalid key names."""


def parse(path: Union[str, Path]) -> dict[str, str]:
    """Read a .env file and return a {KEY: value} dict.

    Accepted shapes per line:
        KEY=value
        KEY="value"
        KEY='value'
        export KEY=value
        # comment                (skipped)
        <blank>                  (skipped)

    Raises EnvFileError on missing file, malformed line, or a key that
    isn't a valid POSIX env var name. Values are returned verbatim with
    surrounding matched quotes stripped; nothing else is unescaped.
    """
    p = Path(path)
    if not p.is_file():
        raise EnvFileError(f"env file not found: {path}")

    out: dict[str, str] = {}
    with p.open() as f:
        for line_num, raw in enumerate(f, start=1):
            line = raw.rstrip("\n").rstrip("\r").strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                raise EnvFileError(
                    f"{path}:{line_num}: expected KEY=value, got {raw.rstrip()!r}"
                )
            key, value = line.split("=", 1)
            key = key.strip()
            if not _KEY_RE.match(key):
                raise EnvFileError(
                    f"{path}:{line_num}: invalid env var name {key!r} "
                    f"(must match [A-Za-z_][A-Za-z0-9_]*)"
                )
            if key in out:
                raise EnvFileError(
                    f"{path}:{line_num}: duplicate key {key!r}"
                )
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            out[key] = value
    return out


def keys(path: Union[str, Path]) -> list[str]:
    """Return just the KEY names from a .env file, in declaration order."""
    return list(parse(path).keys())
