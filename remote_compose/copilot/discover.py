"""Walk a copilot/ tree and produce a typed CopilotApp model.

Copilot's on-disk layout (relevant pieces only):

    copilot/
    ├── <service-name>/
    │   ├── manifest.yml       (required — declares name + type + config)
    │   └── addons/*.yml       (optional — extra CFN resources)
    ├── environments/
    │   └── <env-name>/manifest.yml
    └── pipelines/             (out of scope; presence noted but skipped)

The parser is corpus-driven: it must work on any third-party Copilot
app, not just the ones we've personally inspected. We deliberately
keep `raw` (the underlying yaml dict) on every model so translators
can reach into Copilot-specific fields without us having to grow the
typed surface every time AWS adds a knob.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class DiscoveryError(Exception):
    """Raised when the copilot/ tree can't be parsed cleanly."""


@dataclass
class CopilotAddon:
    name: str           # filename stem
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CopilotService:
    name: str
    type: str           # 'Backend Service', 'Worker Service', 'Load Balanced Web Service', 'Static Site', etc.
    manifest_path: Path
    raw: dict[str, Any] = field(default_factory=dict)
    addons: list[CopilotAddon] = field(default_factory=list)


@dataclass
class CopilotEnvironment:
    name: str
    manifest_path: Path
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CopilotApp:
    root: Path
    services: list[CopilotService] = field(default_factory=list)
    environments: list[CopilotEnvironment] = field(default_factory=list)
    pipelines: list[Path] = field(default_factory=list)  # presence noted; not translated

    def service(self, name: str) -> CopilotService:
        for s in self.services:
            if s.name == name:
                return s
        raise KeyError(f"no service named {name!r} in copilot app at {self.root}")

    def environment(self, name: str) -> CopilotEnvironment:
        for e in self.environments:
            if e.name == name:
                return e
        raise KeyError(f"no environment named {name!r} in copilot app at {self.root}")


# Names under copilot/ that are NOT services (parser-internal).
_RESERVED_TOP_LEVEL = {"environments", "pipelines"}


def discover(path: str | Path) -> CopilotApp:
    """Walk a copilot/ directory and build a CopilotApp model.

    Raises DiscoveryError if the path doesn't exist, contains no
    parseable service manifests, or has a manifest that's malformed
    YAML / missing required name/type fields.
    """
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise DiscoveryError(f"copilot/ path not found or not a directory: {root}")

    services: list[CopilotService] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in _RESERVED_TOP_LEVEL:
            continue
        manifest = child / "manifest.yml"
        if not manifest.exists():
            # Some directories under copilot/ aren't services (e.g. a
            # team's bespoke `notes/` or an empty addons dir). Skip
            # silently rather than error.
            continue
        services.append(_parse_service(child, manifest))

    if not services:
        raise DiscoveryError(
            f"no service manifests found under {root} — expected "
            f"copilot/<service>/manifest.yml entries"
        )

    environments: list[CopilotEnvironment] = []
    env_root = root / "environments"
    if env_root.is_dir():
        for env_dir in sorted(env_root.iterdir()):
            if not env_dir.is_dir():
                continue
            env_manifest = env_dir / "manifest.yml"
            if not env_manifest.exists():
                continue
            environments.append(_parse_environment(env_dir, env_manifest))

    pipelines: list[Path] = []
    pipe_root = root / "pipelines"
    if pipe_root.is_dir():
        for p in sorted(pipe_root.iterdir()):
            if p.is_dir():
                pipelines.append(p)

    return CopilotApp(
        root=root,
        services=services,
        environments=environments,
        pipelines=pipelines,
    )


def _parse_service(svc_dir: Path, manifest_path: Path) -> CopilotService:
    raw = _load_yaml(manifest_path)
    if "name" not in raw:
        raise DiscoveryError(
            f"{manifest_path}: service manifest missing required field 'name'"
        )
    if "type" not in raw:
        raise DiscoveryError(
            f"{manifest_path}: service manifest missing required field 'type'"
        )
    addons = _discover_addons(svc_dir / "addons")
    return CopilotService(
        name=str(raw["name"]),
        type=str(raw["type"]),
        manifest_path=manifest_path,
        raw=raw,
        addons=addons,
    )


def _parse_environment(env_dir: Path, manifest_path: Path) -> CopilotEnvironment:
    raw = _load_yaml(manifest_path)
    # Environment manifests reliably have 'name' but tolerate the dir
    # name as a fallback (some teams omit the field).
    name = str(raw.get("name") or env_dir.name)
    return CopilotEnvironment(
        name=name,
        manifest_path=manifest_path,
        raw=raw,
    )


def _discover_addons(addons_dir: Path) -> list[CopilotAddon]:
    if not addons_dir.is_dir():
        return []
    out: list[CopilotAddon] = []
    for f in sorted(addons_dir.iterdir()):
        if f.is_file() and f.suffix in {".yml", ".yaml"}:
            try:
                raw = _load_yaml_cfn(f)
            except DiscoveryError:
                # Last-resort fallback: CFN template that PyYAML cannot
                # represent even with the lenient loader. Empty dict means
                # the import summary's "no parseable Resources block"
                # bucket. Better than failing the whole discovery for one
                # bad file.
                raw = {}
            out.append(CopilotAddon(name=f.stem, path=f, raw=raw))
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise DiscoveryError(f"{path}: malformed YAML — {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DiscoveryError(
            f"{path}: expected a mapping at the document root, got "
            f"{type(data).__name__}"
        )
    return data


# rc-e5u.43.8: CFN intrinsic tag tolerance for addon YAML parsing.
# AWS Copilot addons are CFN templates that use !Ref / !Sub / !GetAtt /
# !Join / !FindInMap / !If / !Select / !Split / !ImportValue / !Equals.
# safe_load chokes on these. We register a generic constructor on a
# subclass of SafeLoader so the resource-type extraction still works
# without writing manifests like RDS / S3 / DynamoDB to a "no parseable
# Resources" bucket and missing the per-type guidance.
class _CfnTolerantLoader(yaml.SafeLoader):
    """A SafeLoader that treats unknown ``!Tag`` constructors as opaque
    scalars/sequences/mappings instead of erroring."""


def _cfn_tag_constructor(loader, tag_suffix, node):
    """multi_constructor signature: (loader, tag_suffix, node) — tag_suffix is
    the part after the prefix the constructor was registered for. We treat
    every ``!Foo`` as the underlying scalar/sequence/mapping value."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


_CfnTolerantLoader.add_multi_constructor("!", _cfn_tag_constructor)


def _load_yaml_cfn(path: Path) -> dict[str, Any]:
    """Like _load_yaml but tolerant of CFN ``!Ref`` / ``!Sub`` / etc."""
    try:
        with path.open() as f:
            data = yaml.load(f, Loader=_CfnTolerantLoader)
    except yaml.YAMLError as exc:
        raise DiscoveryError(f"{path}: malformed YAML — {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DiscoveryError(
            f"{path}: expected a mapping at the document root, got "
            f"{type(data).__name__}"
        )
    return data
