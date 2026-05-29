"""`rc dev push` — stream local source into an EFS-backed dev mount.

Pairs with the provider's dev-mode EFS emission (rc-e5u.45.8). The provider
has wired an EFS file system + access points + task-def mount points for
every services[*].dev_volumes entry; this module copies bytes from the
local source dir into the mounted path on a live ECS task so a Django dev
server (or any framework with file-watcher reload) sees the change in
seconds.

Transport: tar-pipe over `aws ecs execute-command`. The running task
already has the EFS mounted at the declared mount path, so we just stream
a tarball through stdin into a `tar -xzf - -C <mount>` command running
inside the container. No extra infrastructure (no DataSync, no sidecar).

Reload trigger: nothing. Most users will be running `python manage.py
runserver` (auto-reloads on file change) or a `uvicorn --reload` style
dev server. If a stack uses gunicorn or nginx, the user can wire a
post-push hook themselves; that's a follow-up (rc-e5u.45.10).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional


class DevPushError(Exception):
    """User-actionable failure from a dev-push run.

    Distinct from generic Exception so the CLI can surface the message
    without a traceback (the user typically just needs to fix the path
    or re-run after the task is RUNNING).
    """


# ---------------------------------------------------------------------------
# Resolution: rc.yml v2 → (service, dev_volume entry, source dir)
# ---------------------------------------------------------------------------


def resolve_targets(
    rc_yml_path: Path,
    service_filter: Optional[str] = None,
) -> list[dict]:
    """Walk rc.yml v2's services[*].dev_volumes and return push targets.

    Returns one entry per (service, dev_volume) pair, with absolute source
    paths resolved against the rc.yml's directory. When ``service_filter``
    is set, only that service's dev_volumes are returned.

    Raises DevPushError if the config has no dev_volumes anywhere or if
    the requested service has none.
    """
    from .cli_v2 import load_rc_yml

    version, _, v2 = load_rc_yml(rc_yml_path)
    if version != 2 or v2 is None:
        raise DevPushError(
            f"{rc_yml_path} is not an rc.yml v2 config; `rc dev` only " f"supports v2."
        )
    project_dir = rc_yml_path.parent.resolve()

    targets: list[dict] = []
    for svc_name, svc in v2.services.items():
        if service_filter and svc_name != service_filter:
            continue
        for dv in svc.dev_volumes or []:
            src = Path(dv["source"])
            if not src.is_absolute():
                src = (project_dir / src).resolve()
            targets.append(
                {
                    "service": svc_name,
                    "name": dv["name"],
                    "source": src,
                    "mount": dv["mount"],
                    "project": v2.project,
                    "cluster": (v2.provider_config or {}).get("ecs", {}).get("cluster")
                    or f"{v2.project}-cluster",
                    "region": (v2.provider_config or {}).get("ecs", {}).get("region"),
                    "aws_profile": (v2.provider_config or {})
                    .get("ecs", {})
                    .get("aws_profile"),
                }
            )
    if not targets:
        if service_filter:
            raise DevPushError(
                f"service {service_filter!r} declares no dev_volumes; "
                f"add a `dev_volumes:` entry to its rc.yml block."
            )
        raise DevPushError(
            "no services declare dev_volumes in this rc.yml; nothing to "
            "push. Add `dev_volumes:` entries first."
        )
    return targets


# ---------------------------------------------------------------------------
# Transport: tar-pipe via aws ecs execute-command
# ---------------------------------------------------------------------------


# Common dev cruft we never want to ship over the wire. Mirrors the
# defaults `rsync --exclude` would use for a Django/Node project.
_DEFAULT_EXCLUDE = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".ruff_cache",
    ".DS_Store",
    ".idea",
    ".vscode",
}


def _iter_files(source: Path, excludes: Iterable[str]) -> Iterable[Path]:
    """Walk ``source`` yielding every file path NOT under an excluded
    directory name. Excludes match by basename anywhere in the path."""
    excludes = set(excludes)
    for root, dirs, files in os.walk(source):
        # Mutate dirs in place so os.walk skips excluded subtrees entirely.
        dirs[:] = [d for d in dirs if d not in excludes]
        for f in files:
            if f in excludes:
                continue
            yield Path(root) / f


def find_running_task(
    session_factory: Callable[[], object],
    cluster: str,
    service: str,
) -> Optional[str]:
    """Return the ARN of one RUNNING task for ``service`` in ``cluster``,
    or None if no task is currently up. The first task wins — for a
    typical dev stack desired_count=1 anyway."""
    session = session_factory()
    ecs = session.client("ecs")
    resp = ecs.list_tasks(
        cluster=cluster,
        serviceName=service,
        desiredStatus="RUNNING",
    )
    arns = resp.get("taskArns") or []
    return arns[0] if arns else None


def push_one(
    target: dict,
    *,
    session_factory: Callable[[], object],
    runner: Optional[
        Callable[[list[str], bytes, dict], "subprocess.CompletedProcess"]
    ] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> float:
    """Tar-pipe ``target['source']`` into ``target['mount']`` on the live
    task. Returns elapsed seconds.

    ``runner`` is injected for testing — defaults to subprocess.run. The
    real call is::

        aws ecs execute-command \\
            --cluster <cluster> --task <arn> --container <service> \\
            --interactive \\
            --command "tar -xzf - -C <mount>"

    with a gzipped tarball streamed to stdin.

    Raises DevPushError if the source dir is missing, no task is running,
    or aws cli exits non-zero.
    """
    src: Path = target["source"]
    if not src.exists():
        raise DevPushError(
            f"dev_volume source {src} does not exist; check the path in "
            f"your rc.yml `dev_volumes[].source`."
        )
    if not src.is_dir():
        raise DevPushError(
            f"dev_volume source {src} is not a directory; rc dev push "
            f"streams entire trees, not individual files."
        )

    task_arn = find_running_task(
        session_factory,
        target["cluster"],
        target["service"],
    )
    if task_arn is None:
        raise DevPushError(
            f"no RUNNING task for service {target['service']!r} in "
            f"cluster {target['cluster']!r}. Wait for the deploy to come "
            f"up, then re-run `rc dev push`."
        )

    # Build the tarball entirely in memory. Dev source trees are typically
    # small (~10-50MB after default excludes); a Python project's full
    # source tree fits comfortably. For larger trees we'd switch to a
    # tempfile or a pipe-based subprocess pair, but not before there's
    # evidence anyone needs it.
    import io

    buf = io.BytesIO()
    file_count = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in _iter_files(src, _DEFAULT_EXCLUDE):
            arcname = str(path.relative_to(src))
            tar.add(str(path), arcname=arcname, recursive=False)
            file_count += 1
    payload = buf.getvalue()
    if progress:
        progress(
            f"  packed {file_count} files ({len(payload) / 1024:.1f}KB) " f"from {src}"
        )

    # `tar -xzf -` reads the gzipped tar from stdin; we tell it to land
    # in the EFS-mounted target directory.
    mount = target["mount"]
    inner_cmd = f"tar -xzf - -C {shlex.quote(mount)}"
    aws_cmd = [
        "aws",
        "ecs",
        "execute-command",
        "--cluster",
        target["cluster"],
        "--task",
        task_arn,
        "--container",
        target["service"],
        "--interactive",
        "--command",
        inner_cmd,
    ]
    if target.get("region"):
        aws_cmd.extend(["--region", target["region"]])

    env = os.environ.copy()
    if target.get("aws_profile"):
        env["AWS_PROFILE"] = target["aws_profile"]

    if progress:
        progress(
            f"  streaming → {target['service']} {mount} (task "
            f"{task_arn.rsplit('/', 1)[-1][:12]})"
        )

    start = time.monotonic()
    if runner is None:
        proc = subprocess.run(
            aws_cmd,
            input=payload,
            capture_output=True,
            env=env,
        )
    else:
        proc = runner(aws_cmd, payload, env)
    elapsed = time.monotonic() - start

    if proc.returncode != 0:
        # AWS CLI will dump session-manager-plugin diagnostics to stderr;
        # surface them so the user can tell a stale session apart from a
        # missing target dir.
        err = (
            proc.stderr.decode("utf-8", errors="replace")
            if isinstance(proc.stderr, (bytes, bytearray))
            else (proc.stderr or "")
        )
        raise DevPushError(
            f"aws ecs execute-command failed ({proc.returncode}) for "
            f"{target['service']}: {err.strip() or '(no stderr)'}"
        )
    return elapsed


def push_all(
    rc_yml_path: Path,
    service_filter: Optional[str] = None,
    *,
    session_factory: Optional[Callable[[], object]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """Push every dev_volume on the resolved targets, one at a time.

    Returns a list of result dicts (target + elapsed seconds). Aborts
    on the first failure — partial pushes leave the remaining services
    untouched, which is the right behavior in dev (you'll fix the
    failing one and re-run).
    """
    if session_factory is None:

        def _default_session_factory() -> object:
            import boto3

            # Region/profile come from the per-target dicts via env+args
            # on the aws cli, NOT here, so the boto3 session is just for
            # the ECS list_tasks call.
            #
            # We grab the first target's region/profile so list_tasks
            # hits the right account.
            targets_for_session = resolve_targets(rc_yml_path, service_filter)
            tt = targets_for_session[0]
            return boto3.Session(
                region_name=tt.get("region"),
                profile_name=tt.get("aws_profile"),
            )

        session_factory = _default_session_factory

    targets = resolve_targets(rc_yml_path, service_filter)
    results: list[dict] = []
    for t in targets:
        if progress:
            progress(f"\n  {t['service']} :: {t['name']} → {t['mount']}")
        elapsed = push_one(
            t,
            session_factory=session_factory,
            progress=progress,
        )
        if progress:
            progress(f"  done in {elapsed:.1f}s")
        results.append({**t, "elapsed_s": elapsed})
    return results


# ---------------------------------------------------------------------------
# Watch mode: poll fswatch / inotifywait, debounce, re-run push_all
# ---------------------------------------------------------------------------


def _detect_watcher() -> Optional[str]:
    """Pick the first available file-system watcher binary.

    fswatch ships with Homebrew (the macOS standard); inotifywait comes
    from inotify-tools on Linux. Returns None when neither is on PATH —
    the CLI surfaces an install hint then.
    """
    import shutil

    for binary in ("fswatch", "inotifywait"):
        if shutil.which(binary):
            return binary
    return None


def _build_watch_cmd(binary: str, sources: list[Path]) -> list[str]:
    if binary == "fswatch":
        # -0 null-separates events; -r recursive (default but explicit
        # for clarity); --latency 0.1 so we batch fast bursts together.
        return ["fswatch", "-0", "-r", "--latency", "0.1", *(str(s) for s in sources)]
    if binary == "inotifywait":
        # -m monitor mode, -r recursive, -q quiet (no startup banner),
        # -e modify,create,delete,move covers what dev edits do, --format
        # %w/%f gives us a path per event.
        return [
            "inotifywait",
            "-mrq",
            "-e",
            "modify,create,delete,move",
            "--format",
            "%w%f",
            *(str(s) for s in sources),
        ]
    raise DevPushError(f"unknown watcher binary {binary!r}")


def watch_and_push(
    rc_yml_path: Path,
    service_filter: Optional[str] = None,
    *,
    debounce_ms: int = 250,
    progress: Optional[Callable[[str], None]] = None,
    _popen: Optional[Callable[..., subprocess.Popen]] = None,
    _push: Optional[Callable[..., list[dict]]] = None,
) -> None:
    """Run fswatch/inotifywait, debounce events, push on each batch.

    Blocks forever; user kills it with Ctrl-C. ``_popen`` and ``_push``
    are injected for testing — defaults call subprocess.Popen and
    push_all respectively.

    Each event triggers at most ONE push per debounce window so a save
    that touches many files (Django collectstatic, IDE format-on-save)
    coalesces into a single tar-pipe.
    """
    binary = _detect_watcher()
    if binary is None:
        raise DevPushError(
            "no file watcher found. Install one:\n"
            "  macOS: brew install fswatch\n"
            "  Linux: apt-get install inotify-tools (or yum install)"
        )

    targets = resolve_targets(rc_yml_path, service_filter)
    sources = sorted({t["source"] for t in targets})

    if progress:
        progress(f"  watching ({binary}): {', '.join(str(s) for s in sources)}")
        progress(f"  debounce: {debounce_ms}ms — Ctrl-C to stop")

    push_fn = _push or (
        lambda: push_all(
            rc_yml_path,
            service_filter,
            progress=progress,
        )
    )

    # Initial seed so the running task starts with current source.
    push_fn()

    cmd = _build_watch_cmd(binary, sources)
    popen = _popen or subprocess.Popen
    proc = popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stdout is not None

    try:
        # Debounce loop: read events as fast as they arrive, push only
        # `debounce_ms` after the LAST event in a burst.
        debounce_s = debounce_ms / 1000.0
        pending = False
        last_event = 0.0
        # Use select to wait for either stdout activity OR the debounce
        # timer firing. fswatch -0 emits null bytes, inotifywait emits
        # newlines; either way we just need to know "an event happened".
        import select

        while True:
            timeout = None
            if pending:
                # Time until the next push fires.
                timeout = max(0.0, debounce_s - (time.monotonic() - last_event))
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            if ready:
                # Drain whatever's available so we don't get backlogged.
                # read1 if buffered; raw read on the underlying fd
                # otherwise (test fixtures use os.fdopen unbuffered).
                read1 = getattr(proc.stdout, "read1", None)
                if read1 is not None:
                    chunk = read1(4096)
                else:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    # Watcher exited.
                    break
                last_event = time.monotonic()
                pending = True
                continue
            # Timeout fired with no new events — the burst has settled.
            if pending:
                pending = False
                if progress:
                    progress("\n  change detected — pushing...")
                try:
                    push_fn()
                except DevPushError as exc:
                    if progress:
                        progress(f"  push failed: {exc}")
                    # Keep watching; transient task issues (rolling deploy)
                    # resolve themselves and the next event re-fires.
    except KeyboardInterrupt:
        if progress:
            progress("\n  stopping watch.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
