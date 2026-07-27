"""Source plugins for `rc dev` EC2 bootstrap.

Each SourceSpec subtype declares HOW the dev host gets the code/image to run.
On boot, AL2023 cloud-init reads the rendered #cloud-config blob and executes
the per-source bootstrap (clone repo, pull image, etc.).

Adding a new source = new dataclass + new Jinja template under
remote_compose/templates/cloud_init/.
"""

from __future__ import annotations

import shlex
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


# Fields on a SourceSpec that hold live credentials. They are excluded from
# BOTH cloud-init rendering and state-file serialization; see
# _DevEnvMixin.dev_env_content and service._source_to_dict.
SECRET_SOURCE_FIELDS = ("gh_token", "extra_env")


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a git URL — used as the clone target dir."""
    # handles both https://github.com/owner/repo.git and git@github.com:owner/repo.git
    tail = url.rstrip("/").split("/")[-1].split(":")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


class _DevEnvMixin:
    """Builds the box's `.rc-dev-env` payload — and keeps it out of user-data.

    These values used to be rendered straight into the cloud-config blob. That
    put a live PAT in four places at once, only one of which is obvious:
    EC2 user-data (readable from inside the box over IMDS, and from outside
    with ec2:DescribeInstanceAttribute), and — because terraform takes
    user_data_base64 as an ordinary variable — the operator's local
    terraform.tfvars.json and terraform.tfstate as well. Confirmed by
    inflating the gzip blob out of a real .rc/terraform-state/ tree.

    So the payload is no longer part of the rendered cloud-init at all.
    `rc dev up` hands it to the box over the same post-boot SSH channel it
    already uses for --env and --compose files, and the bootstrap script waits
    for it before cloning. render_user_data() only records *that* a delivery is
    coming, never what's in it.
    """

    def dev_env_content(self) -> str:
        """Shell-sourceable `export` lines for /home/ec2-user/.rc-dev-env.

        SECRET. Delivered over SSH by the CLI — never rendered into user-data,
        never written to the state file.
        """
        lines = []
        if getattr(self, "gh_token", ""):
            lines.append(f"export GH_TOKEN={shlex.quote(self.gh_token)}")
        for k, v in (getattr(self, "extra_env", None) or {}).items():
            lines.append(f"export {k}={shlex.quote(str(v))}")
        return "\n".join(lines)


@dataclass
class GitSource(_DevEnvMixin):
    url: str = ""
    ref: str = "main"
    type: Literal["git"] = "git"
    # Optional secrets for the dev box. NOT rendered into user-data and NOT
    # persisted to .rc/dev-hosts.yml — see _DevEnvMixin. They live in this
    # object only for as long as `rc dev up` needs to hand them to the box.
    gh_token: str = ""
    extra_env: dict = field(default_factory=dict)
    # When True, the in-box claude tmux session boots with
    # --dangerously-skip-permissions so it can act without confirmations.
    # Use for autonomous unattended sessions; the agent will still operate
    # within the container/VM sandbox.
    skip_permissions: bool = False

    def render_user_data(self, *, docker_arch: str = "aarch64") -> str:
        repo_name = _repo_name_from_url(self.url)
        return _render(
            "git.yaml.j2",
            url=self.url,
            ref=self.ref,
            repo_name=repo_name,
            docker_arch=docker_arch,
            expect_env_delivery=bool(self.dev_env_content()),
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
class MultiGitSource(_DevEnvMixin):
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
    # See GitSource: secret, SSH-delivered, never in user-data or state.
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
        return _render(
            "multi-git.yaml.j2",
            repos=normalized,
            compose_filenames=self.compose_filenames,
            docker_arch=docker_arch,
            expect_env_delivery=bool(self.dev_env_content()),
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
