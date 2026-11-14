"""Parse docker-compose / django-style .env files.

Standalone, no project dependencies. Used by the ECS provider at
emit-time to learn which KEY names live in a file-sourced secret (so
task definitions can map one ECS secret entry per key), and by `rc
secrets push` at upload-time to turn a file into a JSON blob for
Secrets Manager.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Union

JSON_PREFIX = "json:"
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
        KEY="line1\nline2"     (quoted value may span lines)
        KEY=json:"a\nb\"c"    (lossless: any string, JSON-escaped)

    Raises EnvFileError on missing file, malformed line, or a key that
    isn't a valid POSIX env var name. Values are returned verbatim with
    surrounding matched quotes stripped; nothing else is unescaped.
    """
    p = Path(path)
    if not p.is_file():
        raise EnvFileError(f"env file not found: {path}")

    out: dict[str, str] = {}
    with p.open() as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        line_num = i + 1
        raw = lines[i]
        i += 1
        line = raw.rstrip("\n").rstrip("\r").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
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
            raise EnvFileError(f"{path}:{line_num}: duplicate key {key!r}")
        value = value.strip()
        # A quoted value that does not close on its own line continues onto the
        # following lines (multi-line secrets such as PEM keys). Newlines inside
        # are kept verbatim.
        if value[:1] in {'"', "'"} and not (len(value) >= 2 and value[-1] == value[0]):
            q = value[0]
            end = next(
                (j for j in range(i, len(lines)) if lines[j].rstrip().endswith(q)),
                None,
            )
            # No closing line anywhere: keep the old behaviour (value verbatim).
            if end is not None:
                parts = [value] + [l.rstrip("\n").rstrip("\r") for l in lines[i : end + 1]]
                i = end + 1
                value = "\n".join(parts).rstrip()
        if value.startswith(JSON_PREFIX):
            # Lossless form written by tooling that must carry any value
            # (newlines, quotes, $, =, unicode): json:<JSON string literal>.
            try:
                decoded = json.loads(value[len(JSON_PREFIX):])
            except ValueError as exc:
                raise EnvFileError(f"{path}:{line_num}: bad json: value for {key!r}") from exc
            if not isinstance(decoded, str):
                raise EnvFileError(f"{path}:{line_num}: json: value for {key!r} must be a string")
            out[key] = decoded
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        out[key] = value
    return out


def keys(path: Union[str, Path]) -> list[str]:
    """Return just the KEY names from a .env file, in declaration order."""
    return list(parse(path).keys())
