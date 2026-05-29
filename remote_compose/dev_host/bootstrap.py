"""Source plugins for `rc dev` EC2 bootstrap.

Each SourceSpec subtype declares HOW the dev host gets the code/image to run.
On boot, AL2023 cloud-init reads the rendered #cloud-config blob and executes
the per-source bootstrap (clone repo, pull image, etc.).

Adding a new source = new dataclass + new Jinja template under
remote_compose/templates/cloud_init/.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Union

import jinja2

from ..exceptions import (
    CloudInitRenderError,
    SourceDetectionError,
    ValidationError,
)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "cloud_init"


def _jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )


def _render(template_name: str, **context) -> str:
    try:
        return _jinja_env().get_template(template_name).render(**context)
    except jinja2.TemplateError as exc:
        raise CloudInitRenderError(
            f"failed to render cloud-init template {template_name!r}: {exc}"
        ) from exc


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a git URL — used as the clone target dir."""
    # handles both https://github.com/owner/repo.git and git@github.com:owner/repo.git
    tail = url.rstrip("/").split("/")[-1].split(":")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


@dataclass
class GitSource:
    url: str = ""
    ref: str = "main"
    type: Literal["git"] = "git"
    # Optional secrets to inject into the EC2 instance via cloud-init.
    # SECURITY: These values land in EC2 user-data, visible to anyone with
    # ec2:DescribeInstanceAttribute on the instance. Acceptable for v1
    # ephemeral dev hosts; not for production.
    gh_token: str = ""
    extra_env: dict = field(default_factory=dict)
    # When True, the in-box claude tmux session boots with
    # --dangerously-skip-permissions so it can act without confirmations.
    # Use for autonomous unattended sessions; the agent will still operate
    # within the container/VM sandbox.
    skip_permissions: bool = False

    def render_user_data(self, *, docker_arch: str = "aarch64") -> str:
        repo_name = _repo_name_from_url(self.url)
        env_lines = []
        if self.gh_token:
            env_lines.append(f"export GH_TOKEN={self.gh_token!r}")
        for k, v in (self.extra_env or {}).items():
            env_lines.append(f"export {k}={v!r}")
        rc_dev_env_content = "\n".join(env_lines)
        return _render(
            "git.yaml.j2",
            url=self.url,
            ref=self.ref,
            repo_name=repo_name,
            docker_arch=docker_arch,
            rc_dev_env_content=rc_dev_env_content,
            has_env=bool(env_lines),
            claude_flags=(
                "--dangerously-skip-permissions" if self.skip_permissions else ""
            ),
        )


@dataclass
class ImageSource:
    image: str = ""
    type: Literal["image"] = "image"

    def render_user_data(self) -> str:
        return _render("image.yaml.j2", image=self.image)


@dataclass
class LocalSource:
    path: str = ""
    type: Literal["local"] = "local"

    def render_user_data(self) -> str:
        target_name = Path(self.path).name or "workspace"
        return _render("local.yaml.j2", target_name=target_name)


@dataclass
class ScriptSource:
    script: str = ""
    type: Literal["script"] = "script"

    def render_user_data(self) -> str:
        return _render("script.yaml.j2", script=self.script)


@dataclass
class MultiGitSource:
    """Multi-repo dev-host source: clone N repos to N target dirs and run one
    or more user-supplied top-level docker-compose files at /home/ec2-user/.

    Each compose file becomes its own `docker compose -p <basename>` project
    so service-name conflicts across repos are avoided (e.g. sentinal and
    browser-mgr both define a service named 'django' — they live in separate
    Compose projects on the same docker daemon).

    The compose files themselves are NOT rendered into cloud-init (they may
    be large or sensitive); the CLI SCPs them post-boot. Cloud-init waits.
    """

    repos: list = field(default_factory=list)
    # New (preferred): list of compose filenames. Backwards-compat: still
    # accepts the old `compose_filename: str` via __post_init__ below.
    compose_filenames: list = field(default_factory=list)
    compose_filename: str = ""  # legacy single-file field, see __post_init__
    type: Literal["multi-git"] = "multi-git"
    gh_token: str = ""
    extra_env: dict = field(default_factory=dict)
    skip_permissions: bool = False

    def __post_init__(self):
        # Migrate legacy single-file kwarg into the new list.
        if self.compose_filename and not self.compose_filenames:
            self.compose_filenames = [self.compose_filename]

    def render_user_data(self, *, docker_arch: str = "aarch64") -> str:
        normalized = []
        for r in self.repos:
            url = r["url"]
            normalized.append(
                {
                    "url": url,
                    "ref": r.get("ref", "main"),
                    "target": r.get("target") or _repo_name_from_url(url),
                }
            )
        env_lines = []
        if self.gh_token:
            env_lines.append(f"export GH_TOKEN={self.gh_token!r}")
        for k, v in (self.extra_env or {}).items():
            env_lines.append(f"export {k}={v!r}")
        return _render(
            "multi-git.yaml.j2",
            repos=normalized,
            compose_filenames=self.compose_filenames,
            docker_arch=docker_arch,
            rc_dev_env_content="\n".join(env_lines),
            has_env=bool(env_lines),
            claude_flags=(
                "--dangerously-skip-permissions" if self.skip_permissions else ""
            ),
        )


SourceSpec = Union[GitSource, ImageSource, LocalSource, ScriptSource, MultiGitSource]


_SOURCE_CLASSES: dict[str, type] = {
    "git": GitSource,
    "image": ImageSource,
    "local": LocalSource,
    "script": ScriptSource,
    "multi-git": MultiGitSource,
}


def source_from_dict(d: dict) -> SourceSpec:
    """Reconstruct a SourceSpec from its serialized form (used by state file)."""
    type_ = d.get("type")
    cls = _SOURCE_CLASSES.get(type_)
    if cls is None:
        raise ValidationError(f"unknown source type {type_!r}")
    fields = {k: v for k, v in d.items() if k != "type"}
    return cls(**fields)


def detect_source_from_cwd(cwd: Path | str | None = None) -> SourceSpec:
    """Auto-detect a GitSource from the current working directory.

    Inspects `git remote get-url origin` and `git symbolic-ref --short HEAD`.
    Raises SourceDetectionError (a ValidationError subclass) if cwd is not in
    a git repo or the remote isn't configured.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()

    try:
        url = subprocess.run(
            ["git", "-C", str(cwd_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SourceDetectionError(
            f"could not detect git remote in {cwd_path}: {exc}"
        ) from exc

    try:
        ref = subprocess.run(
            ["git", "-C", str(cwd_path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        # detached HEAD or no commits — fall back to the commit SHA
        try:
            ref = subprocess.run(
                ["git", "-C", str(cwd_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            raise SourceDetectionError(
                f"git repo at {cwd_path} has no commits or detached HEAD"
            ) from exc

    return GitSource(url=url, ref=ref)
