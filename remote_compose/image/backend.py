"""Pluggable build backends (rc-8j7).

``rc deploy`` builds images WHEREVER the CLI runs. On the ECS deploy path
that's a GitHub Actions runner with a cold ``rc-cache`` buildx builder — it
builds N images serially, each paying a full ECR registry-cache round-trip.
The :class:`BuildBackend` seam makes WHERE the build happens pluggable: the
default ``local`` backend runs docker buildx on the CLI host (today's
behavior); a configured deploy can swap in a remote backend that builds off
the runner (AWS CodeBuild / a persistent remote BuildKit — see the rc-8j7.5
design) without the provider caring.

A backend takes a list of already-resolved :class:`ImageBuildSpec`s (tags,
cache refs, cache mode, and push mode baked in per spec by the provider) and
returns the service names it successfully built + pushed. Registry auth and
ECR repo resolution stay in the provider — a backend only turns specs into
pushed images.
"""

from __future__ import annotations

import fnmatch
import io
import os
import re
import shlex
import tarfile
import time
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

import yaml

from .builder import (
    ImageBuildSpec,
    ImageBuilder,
    buildx_build_flags,
    resolve_dockerfile,
)
from .pusher import ImagePusher

# rc-8j7.2: default backend name — today's on-runner build.
DEFAULT_BUILD_BACKEND = "local"
# rc-8j7.4: default registry cache mode. mode=max exports every
# intermediate layer (the pip/apt stages need it to cache); mode=min only
# the final stage. Kept at max so a zero-config deploy behaves as before.
DEFAULT_CACHE_MODE = "max"
VALID_CACHE_MODES = ("max", "min")
# rc-8j7.3: parallelize independent image groups. Bounded so a fleet of
# services can't fork an unbounded number of concurrent docker builds (each
# is CPU + IO heavy). 4 is a sensible default for a CI runner; override via
# provider_config.ecs.build.max_workers or RC_BUILD_MAX_WORKERS.
DEFAULT_BUILD_MAX_WORKERS = 4

_BACKEND_ENV = "RC_BUILD_BACKEND"
_CACHE_MODE_ENV = "RC_BUILD_CACHE_MODE"
_MAX_WORKERS_ENV = "RC_BUILD_MAX_WORKERS"

# rc-8j7.5: AWS CodeBuild backend name + defaults.
AWS_CODEBUILD_BACKEND = "aws-codebuild"
# BUILD_GENERAL1_LARGE: 8 vCPU / 15 GB — headroom for the heavy Django + a
# browser image. Config-overridable via build.codebuild.compute_type.
DEFAULT_CODEBUILD_COMPUTE_TYPE = "BUILD_GENERAL1_LARGE"
# AWS-managed Linux image that ships Docker + buildx (privileged builds).
DEFAULT_CODEBUILD_IMAGE = "aws/codebuild/standard:7.0"
CODEBUILD_ENVIRONMENT_TYPE = "LINUX_CONTAINER"
# Build wall-clock cap (minutes). CodeBuild's own default is 60.
DEFAULT_CODEBUILD_TIMEOUT_MINUTES = 60
# Derived project name is rc-build-<project> when none is configured.
CODEBUILD_PROJECT_NAME_PREFIX = "rc-build-"
# Dedicated buildx builder created inside the CodeBuild container (the
# docker-container driver is required to export --cache-to type=registry).
CODEBUILD_REMOTE_BUILDER = "rc-remote"
# S3 key prefix under which each deploy's tarred build context is uploaded.
CODEBUILD_SOURCE_KEY_PREFIX = "rc-build-context"
# Derived source-bucket name prefix when none is configured.
CODEBUILD_SOURCE_BUCKET_PREFIX = "rc-build-source"
# How often to poll BatchGetBuilds while streaming logs, and how many trailing
# log lines to keep for the failure message.
CODEBUILD_POLL_INTERVAL_SECONDS = 5
CODEBUILD_LOG_TAIL_LINES = 50
# CodeBuild buildStatus values that mean the build is finished.
CODEBUILD_TERMINAL_STATUSES = (
    "SUCCEEDED",
    "FAILED",
    "FAULT",
    "STOPPED",
    "TIMED_OUT",
)
# CodeBuild buildspec schema version (a bare float per AWS's spec).
CODEBUILD_BUILDSPEC_VERSION = 0.2

_CODEBUILD_PROJECT_ENV = "RC_CODEBUILD_PROJECT"
_CODEBUILD_ROLE_ENV = "RC_CODEBUILD_ROLE_ARN"
_CODEBUILD_COMPUTE_ENV = "RC_CODEBUILD_COMPUTE_TYPE"
_CODEBUILD_IMAGE_ENV = "RC_CODEBUILD_IMAGE"
_CODEBUILD_BUCKET_ENV = "RC_CODEBUILD_SOURCE_BUCKET"
_CODEBUILD_REGION_ENV = "RC_CODEBUILD_REGION"
_CODEBUILD_TIMEOUT_ENV = "RC_CODEBUILD_TIMEOUT_MINUTES"


class UnknownBuildBackendError(ValueError):
    """Raised when a configured build backend name isn't registered."""

    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(
            f"unknown build backend {name!r}; available: {', '.join(available)}"
        )
        self.name = name
        self.available = available


class BuildBackend(ABC):
    """Turn a set of build specs into pushed images.

    Implementations decide WHERE the build runs (this host, CodeBuild, a
    remote BuildKit, ...). The contract is uniform so the provider is
    backend-agnostic.
    """

    #: registry key; set by subclasses.
    name: str

    @abstractmethod
    def build_and_push(self, specs: list[ImageBuildSpec]) -> list[str]:
        """Build every spec and push its tags.

        Returns the service names (``spec.service``) that were built +
        pushed, in the SAME order as ``specs`` regardless of the backend's
        internal concurrency (rc-8j7.3 determinism). Raises on the first
        failure so a broken image group fails the whole deploy.
        """


class LocalBuildBackend(BuildBackend):
    """Build + push on the machine running rc — today's path.

    Wraps :class:`ImageBuilder` (docker buildx) + :class:`ImagePusher`
    (docker push). When ``max_workers`` > 1 independent specs build + push
    concurrently (rc-8j7.3); output order and the returned list stay
    deterministic. Per-image + total timings are emitted through the
    progress callback (rc-8j7.6).
    """

    name = "local"

    def __init__(
        self,
        *,
        authenticator: Optional[Callable[[str], Any]] = None,
        docker_bin: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
        max_workers: int = 1,
        # Remote-backend kwargs (session / codebuild / project / region) are
        # accepted-and-ignored here so create_build_backend constructs every
        # backend uniformly — the local build never touches AWS.
        session: Optional[Any] = None,
        codebuild: Optional["CodeBuildConfig"] = None,
        project: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self._builder = ImageBuilder(docker_bin=docker_bin, progress=progress)
        self._pusher = ImagePusher(
            authenticator=authenticator,
            docker_bin=docker_bin,
            progress=progress,
        )
        self._progress = progress
        self._max_workers = max(1, int(max_workers))

    def build_and_push(self, specs: list[ImageBuildSpec]) -> list[str]:
        if not specs:
            return []
        total_start = time.monotonic()
        workers = min(self._max_workers, len(specs))
        if workers <= 1:
            pushed = [self._build_one(spec) for spec in specs]
        else:
            pushed = self._build_parallel(specs, workers)
        plural = "s" if len(specs) != 1 else ""
        self._emit(
            f"  build+push total: {time.monotonic() - total_start:.1f}s "
            f"({len(specs)} image{plural}, {workers} worker{'s' if workers != 1 else ''})"
        )
        return pushed

    def _build_parallel(
        self, specs: list[ImageBuildSpec], workers: int
    ) -> list[str]:
        # Slot results by input index so the returned list stays in input
        # order even though builds finish out of order. The first spec to
        # raise fails the deploy (its exception propagates).
        results: list[Optional[str]] = [None] * len(specs)
        first_error: Optional[BaseException] = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._build_one, spec): i
                for i, spec in enumerate(specs)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except BaseException as exc:  # noqa: BLE001 — re-raised below
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error
        return [name for name in results if name is not None]

    def _build_one(self, spec: ImageBuildSpec) -> str:
        start = time.monotonic()
        tags = self._builder.build(spec)
        # rc-8j7.4: buildx `--push` builds AND pushes in one step, so skip the
        # separate `docker push`. Default (spec.push False) keeps the
        # `--load` + separate-push path — the local image store is populated
        # and ImagePusher pushes each tag.
        if not getattr(spec, "push", False):
            self._pusher.push(tags)
        # rc-8j7.6: per-image timing so before/after is measurable.
        self._emit(
            f"  {spec.service}: built+pushed in {time.monotonic() - start:.1f}s"
        )
        return spec.service

    def _emit(self, msg: str) -> None:
        if self._progress:
            self._progress(msg)


# ---------------------------------------------------------------------------
# Registry + config resolution
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, Callable[..., BuildBackend]] = {}


def register_backend(name: str, factory: Callable[..., BuildBackend]) -> None:
    """Register a backend factory under ``name`` (idempotent overwrite)."""
    _BACKENDS[name] = factory


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


def create_build_backend(
    name: str,
    *,
    authenticator: Optional[Callable[[str], Any]] = None,
    docker_bin: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    max_workers: int = 1,
    session: Optional[Any] = None,
    codebuild: Optional["CodeBuildConfig"] = None,
    project: Optional[str] = None,
    region: Optional[str] = None,
) -> BuildBackend:
    """Instantiate the backend registered under ``name``.

    All backends take the same keyword args so the provider constructs any
    of them uniformly. The base four (authenticator/docker_bin/progress/
    max_workers) drive the local build; ``session``/``codebuild``/``project``/
    ``region`` are the remote-backend inputs (a boto3 session, the resolved
    :class:`CodeBuildConfig`, the deploy's project name for deriving defaults,
    and the AWS region) — the local backend ignores them. Raises
    :class:`UnknownBuildBackendError` on a name that isn't registered (a config
    typo surfaces as a clear error).
    """
    factory = _BACKENDS.get(name)
    if factory is None:
        raise UnknownBuildBackendError(name, available_backends())
    return factory(
        authenticator=authenticator,
        docker_bin=docker_bin,
        progress=progress,
        max_workers=max_workers,
        session=session,
        codebuild=codebuild,
        project=project,
        region=region,
    )


register_backend("local", LocalBuildBackend)


class CodeBuildError(RuntimeError):
    """Raised when a remote AWS CodeBuild build ends non-SUCCEEDED, or when
    the backend is misconfigured (no session / role / resolvable region)."""


@dataclass(frozen=True)
class CodeBuildConfig:
    """Resolved AWS CodeBuild knobs (rc-8j7.5).

    project_name: CodeBuild project to run; None → derive ``rc-build-<project>``.
    service_role_arn: IAM role the project runs as (REQUIRED — rc references,
        never creates, IAM roles).
    compute_type: CodeBuild compute size (default BUILD_GENERAL1_LARGE).
    image: managed build image with Docker + buildx (privileged).
    source_bucket: S3 bucket the tarred build context is uploaded to; None →
        derive ``rc-build-source-<account>-<region>`` and create-if-missing.
    region: AWS region; None → the ECS region / parsed from the ECR tag host.
    timeout_minutes: build wall-clock cap (default 60).
    """

    project_name: Optional[str] = None
    service_role_arn: Optional[str] = None
    compute_type: str = DEFAULT_CODEBUILD_COMPUTE_TYPE
    image: str = DEFAULT_CODEBUILD_IMAGE
    source_bucket: Optional[str] = None
    region: Optional[str] = None
    timeout_minutes: int = DEFAULT_CODEBUILD_TIMEOUT_MINUTES


# ---------------------------------------------------------------------------
# CodeBuild helpers (module-level + pure, so tests can exercise them directly)
# ---------------------------------------------------------------------------

_ECR_HOST_RE = re.compile(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com")


def _registry_host(ref: str) -> Optional[str]:
    """The registry hostname of an image/cache ref, or None for a bare name."""
    if "/" not in ref:
        return None
    head = ref.split("/", 1)[0]
    return head if ("." in head or ":" in head) else None


def _region_from_specs(specs: list[ImageBuildSpec]) -> Optional[str]:
    """Parse the AWS region out of any ECR tag/cache host in the batch."""
    for spec in specs:
        for ref in list(spec.tags) + list(spec.cache_from) + list(spec.cache_to):
            m = _ECR_HOST_RE.search(ref)
            if m:
                return m.group(1)
    return None


def _account_from_specs(specs: list[ImageBuildSpec]) -> Optional[str]:
    """Parse the 12-digit AWS account id out of an ECR host (``<acct>.dkr...``)."""
    for spec in specs:
        for ref in list(spec.tags):
            host = _registry_host(ref) or ""
            head = host.split(".", 1)[0]
            if head.isdigit():
                return head
    return None


def _buildcache_repo_from_specs(specs: list[ImageBuildSpec]) -> Optional[str]:
    """The buildcache repo URL (sans ``:tag``) from any cache ref, or None."""
    for spec in specs:
        for ref in list(spec.cache_to) + list(spec.cache_from):
            # <host>/<path>/buildcache:<svc>-cache → strip the trailing :tag.
            return ref.rsplit(":", 1)[0] if ":" in ref.rsplit("/", 1)[-1] else ref
    return None


def _common_context_root(specs: list[ImageBuildSpec]) -> Path:
    """The common ancestor directory of every spec's build context.

    One S3 upload covers all images: the tar is rooted here and each buildx
    invocation references its context path RELATIVE to this root. For the
    Django layout (django context ``.`` + small images under
    ``compose/production/*``) this resolves to the repo root.
    """
    resolved = [str(Path(s.context).resolve()) for s in specs]
    if len(resolved) == 1:
        return Path(resolved[0])
    return Path(os.path.commonpath(resolved))


def _relpath(path: Path, root: Path) -> str:
    """POSIX path of ``path`` relative to ``root`` (``.`` when equal)."""
    rel = os.path.relpath(str(Path(path).resolve()), str(Path(root).resolve()))
    return rel.replace(os.sep, "/")


def _load_dockerignore_patterns(root: Path) -> list[str]:
    """Best-effort ``.dockerignore`` patterns at ``root`` (comments/negations
    skipped). buildx inside CodeBuild is the real enforcer — this only trims
    the upload, so exact + glob (via fnmatch) coverage is enough."""
    df = Path(root) / ".dockerignore"
    if not df.is_file():
        return []
    patterns: list[str] = []
    for raw in df.read_text(errors="replace").splitlines():
        line = raw.strip()
        # Skip blanks, comments, and negations (re-includes) — dropping a
        # negation just keeps the path, which buildx would keep anyway.
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line.rstrip("/"))
    return patterns


def _dockerignored(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat):
            return True
        if rel == pat or rel.startswith(pat + "/"):
            return True
    return False


def tar_build_context(root: Path, *, keep: Optional[set[str]] = None) -> bytes:
    """Tar+gzip ``root`` honoring its ``.dockerignore`` (rc-8j7.5).

    Deterministic (sorted walk), prunes ignored subtrees, and always keeps any
    path in ``keep`` (the referenced Dockerfiles — excluding one would break
    the build). Returns the gzipped tar bytes for a single S3 upload.
    """
    root = Path(root).resolve()
    patterns = _load_dockerignore_patterns(root)
    keep = keep or set()

    def _under_keep(rel: str) -> bool:
        # A kept file forces its ancestor dirs to survive pruning, so we can
        # descend to it even when the dir itself matches .dockerignore.
        return any(kp == rel or kp.startswith(rel + "/") for kp in keep)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            kept_dirs: list[str] = []
            for d in sorted(dirnames):
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if _dockerignored(rel, patterns) and not _under_keep(rel):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for fname in sorted(filenames):
                rel = f"{rel_dir}/{fname}" if rel_dir else fname
                if _dockerignored(rel, patterns) and rel not in keep:
                    continue
                tar.add(os.path.join(dirpath, fname), arcname=rel, recursive=False)
    return buf.getvalue()


def generate_codebuild_buildspec(
    specs: list[ImageBuildSpec],
    *,
    root: Path,
    region: str,
    remote_builder: str = CODEBUILD_REMOTE_BUILDER,
) -> str:
    """Render the CodeBuild buildspec that runs the SAME buildx build for each
    spec (rc-8j7.5).

    pre_build logs docker into every ECR registry the batch touches (tags +
    cache refs) and creates a docker-container buildx builder (needed to export
    ``--cache-to type=registry``). build then runs, per spec, the identical
    ``docker buildx build`` the local backend emits — same ``buildx_build_flags``
    (``-f``/``--target``/``--platform``/``--no-cache``/``--build-arg``/
    ``--cache-from``/``--cache-to``/``-t``) — but with ``--push`` (CodeBuild
    auths to ECR directly, so no separate load+push) and a context path
    relative to the extracted tar root. Commands run sequentially; CodeBuild
    fails the build on the first non-zero exit, matching the local backend's
    fail-on-first-error contract.
    """
    hosts: list[str] = []
    for spec in specs:
        for ref in list(spec.tags) + list(spec.cache_from) + list(spec.cache_to):
            host = _registry_host(ref)
            if host and host not in hosts:
                hosts.append(host)

    pre_build: list[str] = []
    for host in hosts:
        pre_build.append(
            f"aws ecr get-login-password --region {region} "
            f"| docker login --username AWS --password-stdin {host}"
        )
    pre_build.append(
        f"docker buildx create --name {remote_builder} "
        f"--driver docker-container --use --bootstrap"
    )

    build_cmds: list[str] = []
    for spec in specs:
        df = resolve_dockerfile(spec)
        df_rel = _relpath(df, root) if df else None
        ctx_rel = _relpath(Path(spec.context), root)
        flags = buildx_build_flags(
            spec,
            cache_from=list(spec.cache_from),
            cache_to=list(spec.cache_to),
            dockerfile=df_rel,
        )
        argv = (
            ["docker", "buildx", "build", "--builder", remote_builder, "--push"]
            + flags
            + [ctx_rel]
        )
        build_cmds.append(" ".join(shlex.quote(a) for a in argv))

    doc = {
        "version": CODEBUILD_BUILDSPEC_VERSION,
        "phases": {
            "pre_build": {"commands": pre_build},
            "build": {"commands": build_cmds},
        },
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


class AwsCodeBuildBackend(BuildBackend):
    """Build off the runner via AWS CodeBuild (rc-8j7.5).

    ``build_and_push`` tars the build context (honoring ``.dockerignore``),
    uploads it once to S3, ensures the CodeBuild project exists (create-if-
    missing, referencing a configured IAM service role — rc never creates the
    role), starts a build whose generated buildspec runs the SAME ``docker
    buildx`` per image against the SAME ECR + buildcache repos, streams the
    CloudWatch logs back to rc's progress output, and polls to completion. A
    non-SUCCEEDED build raises :class:`CodeBuildError` with the log tail so the
    deploy fails. Returns the built service names in input order — identical
    contract to :class:`LocalBuildBackend`.

    ``authenticator`` / ``docker_bin`` are unused (the build runs remotely);
    ``session``/``codebuild``/``project``/``region`` carry the AWS inputs.
    """

    name = AWS_CODEBUILD_BACKEND

    def __init__(
        self,
        *,
        authenticator: Optional[Callable[[str], Any]] = None,
        docker_bin: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
        max_workers: int = 1,
        session: Optional[Any] = None,
        codebuild: Optional[CodeBuildConfig] = None,
        project: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self._progress = progress
        self._max_workers = max(1, int(max_workers))
        self._session = session
        self._config = codebuild or CodeBuildConfig()
        self._project = project
        self._region = region

    def build_and_push(self, specs: list[ImageBuildSpec]) -> list[str]:
        if not specs:
            return []
        total_start = time.monotonic()
        cfg = self._config
        if not cfg.service_role_arn:
            raise CodeBuildError(
                "build.backend 'aws-codebuild' requires a CodeBuild service "
                "role — set build.codebuild.service_role_arn. rc references, "
                "never creates, IAM roles; see docs/rc-remote-build-backend.md."
            )
        if self._session is None:
            raise CodeBuildError(
                "the aws-codebuild backend needs a boto3 session (the provider "
                "passes ctx's session); none was supplied."
            )
        region = cfg.region or self._region or _region_from_specs(specs)
        if not region:
            raise CodeBuildError(
                "could not resolve an AWS region for CodeBuild — set "
                "build.codebuild.region or provider_config.ecs.region."
            )
        project_name = cfg.project_name or (
            f"{CODEBUILD_PROJECT_NAME_PREFIX}{self._project or 'rc'}"
        )

        # 1. package the build context (one tar covers all images).
        root = _common_context_root(specs)
        keep = {
            _relpath(df, root)
            for df in (resolve_dockerfile(s) for s in specs)
            if df is not None
        }
        tar_bytes = tar_build_context(root, keep=keep)
        self._emit(
            f"  codebuild: packaged build context {root} "
            f"({_human_bytes(len(tar_bytes))})"
        )

        # 2. upload once to S3.
        bucket = cfg.source_bucket or self._derived_bucket(region, specs)
        key = (
            f"{CODEBUILD_SOURCE_KEY_PREFIX}/"
            f"{self._project or 'rc'}/{uuid4().hex}.tar.gz"
        )
        s3 = self._session.client("s3", region_name=region)
        self._ensure_bucket(s3, bucket, region)
        s3.put_object(Bucket=bucket, Key=key, Body=tar_bytes)
        self._emit(f"  codebuild: uploaded context to s3://{bucket}/{key}")

        # 3. generate the buildspec (same buildx per image).
        buildspec = generate_codebuild_buildspec(specs, root=root, region=region)

        # 4. ensure the project (create-if-missing, referenced IAM role).
        cb = self._session.client("codebuild", region_name=region)
        self._ensure_project(cb, project_name, bucket, cfg)

        # 5. start the build (source override = the S3 object, buildspec, env).
        build_id = self._start_build(
            cb, project_name, bucket, key, buildspec, region, specs
        )
        self._emit(
            f"  codebuild: started build {build_id} on project {project_name!r}"
        )

        # 6. stream logs + poll to completion (raises on non-SUCCEEDED).
        self._stream_and_wait(cb, build_id, region)

        plural = "s" if len(specs) != 1 else ""
        self._emit(
            f"  build+push total (codebuild): "
            f"{time.monotonic() - total_start:.1f}s ({len(specs)} image{plural})"
        )
        return [spec.service for spec in specs]

    # -- AWS steps -----------------------------------------------------

    def _derived_bucket(self, region: str, specs: list[ImageBuildSpec]) -> str:
        account = _account_from_specs(specs) or (self._project or "rc")
        return f"{CODEBUILD_SOURCE_BUCKET_PREFIX}-{account}-{region}"

    def _ensure_bucket(self, s3: Any, bucket: str, region: str) -> None:
        """Create the source bucket if it doesn't exist (best-effort mirror of
        the buildcache ECR-repo ensure). A configured source_bucket is assumed
        operator-owned; a derived one is created here."""
        try:
            s3.head_bucket(Bucket=bucket)
            return
        except Exception:  # noqa: BLE001 — most likely 404/NoSuchBucket
            pass
        params: dict[str, Any] = {"Bucket": bucket}
        # us-east-1 rejects an explicit LocationConstraint; every other region
        # requires it.
        if region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            s3.create_bucket(**params)
            self._emit(f"  codebuild: created source bucket {bucket}")
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code not in {
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
                "OperationAborted",
            }:
                raise CodeBuildError(
                    f"could not create S3 source bucket {bucket!r}: {exc!s}. "
                    f"Set build.codebuild.source_bucket to an existing bucket."
                ) from exc

    def _ensure_project(
        self, cb: Any, project_name: str, bucket: str, cfg: CodeBuildConfig
    ) -> None:
        """Ensure the CodeBuild project exists (create-if-missing).

        Mirrors the provider's buildcache-repo ensure: a describe, then create.
        The per-build source key + buildspec are supplied as overrides on
        start-build, so a project created once is reused across deploys.
        """
        existing = cb.batch_get_projects(names=[project_name]).get("projects") or []
        if existing:
            self._emit(f"  codebuild: reusing project {project_name!r}")
            return
        cb.create_project(
            name=project_name,
            source={"type": "S3", "location": f"{bucket}/"},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={
                "type": CODEBUILD_ENVIRONMENT_TYPE,
                "image": cfg.image,
                "computeType": cfg.compute_type,
                # Docker builds require the privileged flag.
                "privilegedMode": True,
            },
            serviceRole=cfg.service_role_arn,
            timeoutInMinutes=cfg.timeout_minutes,
        )
        self._emit(
            f"  codebuild: created project {project_name!r} "
            f"(role {cfg.service_role_arn}, {cfg.compute_type})"
        )

    def _start_build(
        self,
        cb: Any,
        project_name: str,
        bucket: str,
        key: str,
        buildspec: str,
        region: str,
        specs: list[ImageBuildSpec],
    ) -> str:
        env_overrides = [
            {"name": "AWS_DEFAULT_REGION", "value": region, "type": "PLAINTEXT"},
        ]
        cache_repo = _buildcache_repo_from_specs(specs)
        if cache_repo:
            env_overrides.append(
                {"name": "RC_BUILDCACHE_REPO", "value": cache_repo, "type": "PLAINTEXT"}
            )
        resp = cb.start_build(
            projectName=project_name,
            sourceTypeOverride="S3",
            sourceLocationOverride=f"{bucket}/{key}",
            buildspecOverride=buildspec,
            environmentVariablesOverride=env_overrides,
        )
        return resp["build"]["id"]

    def _stream_and_wait(self, cb: Any, build_id: str, region: str) -> None:
        logs = self._session.client("logs", region_name=region)
        tail: deque[str] = deque(maxlen=CODEBUILD_LOG_TAIL_LINES)
        forward_token: Optional[str] = None
        status = "IN_PROGRESS"
        while True:
            builds = cb.batch_get_builds(ids=[build_id]).get("builds") or []
            build = builds[0] if builds else {}
            status = build.get("buildStatus", "IN_PROGRESS")
            log_info = build.get("logs") or {}
            group = log_info.get("groupName")
            stream = log_info.get("streamName")
            if group and stream:
                forward_token = self._drain_logs(
                    logs, group, stream, forward_token, tail
                )
            if status in CODEBUILD_TERMINAL_STATUSES:
                break
            time.sleep(CODEBUILD_POLL_INTERVAL_SECONDS)
        if status != "SUCCEEDED":
            tail_text = "\n".join(tail) if tail else "(no logs captured)"
            raise CodeBuildError(
                f"CodeBuild build {build_id} finished {status}. "
                f"Last {len(tail)} log line(s):\n{tail_text}"
            )

    def _drain_logs(
        self,
        logs: Any,
        group: str,
        stream: str,
        forward_token: Optional[str],
        tail: "deque[str]",
    ) -> Optional[str]:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": True,
        }
        if forward_token is not None:
            kwargs["nextToken"] = forward_token
        try:
            resp = logs.get_log_events(**kwargs)
        except Exception:  # noqa: BLE001 — stream may not exist yet
            return forward_token
        for event in resp.get("events") or []:
            msg = (event.get("message") or "").rstrip("\n")
            self._emit(f"  [codebuild] {msg}")
            tail.append(msg)
        return resp.get("nextForwardToken", forward_token)

    def _emit(self, msg: str) -> None:
        if self._progress:
            self._progress(msg)


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


register_backend(AWS_CODEBUILD_BACKEND, AwsCodeBuildBackend)


@dataclass(frozen=True)
class BuildConfig:
    """Resolved build knobs (rc-8j7.2 / .4 / .5).

    backend: which BuildBackend runs the build (default ``local``).
    cache_mode: registry cache export mode — ``max`` (default) or ``min``.
    push: when True, build via buildx ``--push`` instead of ``--load`` +
        separate ``docker push`` (default False).
    max_workers: bounded concurrency for the local backend (rc-8j7.3).
    codebuild: AWS CodeBuild knobs — only meaningful when
        ``backend == "aws-codebuild"`` (rc-8j7.5).
    """

    backend: str = DEFAULT_BUILD_BACKEND
    cache_mode: str = DEFAULT_CACHE_MODE
    push: bool = False
    max_workers: int = DEFAULT_BUILD_MAX_WORKERS
    codebuild: CodeBuildConfig = field(default_factory=CodeBuildConfig)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_build_config(
    provider_config: Optional[dict],
    rc_yml_v2: Optional[dict],
    env: Optional[dict] = None,
) -> BuildConfig:
    """Resolve build knobs from env > provider_config.ecs.build > rc.yml build.

    Precedence (highest first) for each knob:
      1. ``RC_BUILD_*`` environment variable (operator escape hatch)
      2. ``provider_config.ecs.build.<knob>`` (the canonical, ECS-scoped spot)
      3. ``rc.yml`` top-level ``build.<knob>``
      4. baked-in default

    Reads defensively (plain ``.get`` chains) so a stack that never mentions
    ``build`` gets the all-default :class:`BuildConfig`. Validates the
    resolved backend name against the registry and the cache mode against
    :data:`VALID_CACHE_MODES`.
    """
    env = os.environ if env is None else env
    ecs_build = (((provider_config or {}).get("ecs") or {}).get("build")) or {}
    yml_build = ((rc_yml_v2 or {}).get("build")) or {}

    def _pick(env_key: str, knob: str, default: Any) -> Any:
        if env_key in env and env.get(env_key) not in (None, ""):
            return env.get(env_key)
        if knob in ecs_build and ecs_build.get(knob) is not None:
            return ecs_build.get(knob)
        if knob in yml_build and yml_build.get(knob) is not None:
            return yml_build.get(knob)
        return default

    backend = str(_pick(_BACKEND_ENV, "backend", DEFAULT_BUILD_BACKEND))
    if backend not in _BACKENDS:
        raise UnknownBuildBackendError(backend, available_backends())

    cache_mode = str(_pick(_CACHE_MODE_ENV, "cache_mode", DEFAULT_CACHE_MODE))
    if cache_mode not in VALID_CACHE_MODES:
        raise ValueError(
            f"invalid build cache_mode {cache_mode!r}; "
            f"expected one of {', '.join(VALID_CACHE_MODES)}"
        )

    push = _as_bool(_pick("RC_BUILD_PUSH", "push", False))

    try:
        max_workers = int(
            _pick(_MAX_WORKERS_ENV, "max_workers", DEFAULT_BUILD_MAX_WORKERS)
        )
    except (TypeError, ValueError):
        max_workers = DEFAULT_BUILD_MAX_WORKERS
    max_workers = max(1, max_workers)

    codebuild = _resolve_codebuild_config(env, ecs_build, yml_build)
    # rc-8j7.5: fail fast (before any AWS call) when the CodeBuild backend is
    # selected without the IAM service role it requires — rc references, never
    # creates, IAM roles.
    if backend == AWS_CODEBUILD_BACKEND and not codebuild.service_role_arn:
        raise ValueError(
            "build.backend 'aws-codebuild' requires a CodeBuild service role: "
            "set build.codebuild.service_role_arn (or RC_CODEBUILD_ROLE_ARN). "
            "rc references, never creates, IAM roles — provision the role once "
            "and reference its ARN. See docs/rc-remote-build-backend.md."
        )

    return BuildConfig(
        backend=backend,
        cache_mode=cache_mode,
        push=push,
        max_workers=max_workers,
        codebuild=codebuild,
    )


def _resolve_codebuild_config(
    env: dict, ecs_build: dict, yml_build: dict
) -> CodeBuildConfig:
    """Resolve the ``build.codebuild`` sub-block with the same env >
    provider_config > rc.yml precedence as the parent knobs (rc-8j7.5)."""
    cb_pc = (ecs_build.get("codebuild")) or {}
    cb_yml = (yml_build.get("codebuild")) or {}

    def _pick(env_key: str, knob: str, default: Any) -> Any:
        if env_key in env and env.get(env_key) not in (None, ""):
            return env.get(env_key)
        if knob in cb_pc and cb_pc.get(knob) is not None:
            return cb_pc.get(knob)
        if knob in cb_yml and cb_yml.get(knob) is not None:
            return cb_yml.get(knob)
        return default

    try:
        timeout = int(
            _pick(
                _CODEBUILD_TIMEOUT_ENV,
                "timeout_minutes",
                DEFAULT_CODEBUILD_TIMEOUT_MINUTES,
            )
        )
    except (TypeError, ValueError):
        timeout = DEFAULT_CODEBUILD_TIMEOUT_MINUTES

    return CodeBuildConfig(
        project_name=_pick(_CODEBUILD_PROJECT_ENV, "project_name", None) or None,
        service_role_arn=(
            _pick(_CODEBUILD_ROLE_ENV, "service_role_arn", None) or None
        ),
        compute_type=str(
            _pick(_CODEBUILD_COMPUTE_ENV, "compute_type", DEFAULT_CODEBUILD_COMPUTE_TYPE)
        ),
        image=str(_pick(_CODEBUILD_IMAGE_ENV, "image", DEFAULT_CODEBUILD_IMAGE)),
        source_bucket=_pick(_CODEBUILD_BUCKET_ENV, "source_bucket", None) or None,
        region=_pick(_CODEBUILD_REGION_ENV, "region", None) or None,
        timeout_minutes=timeout,
    )
