"""CLI helpers for the rc.yml v2 code path.

When a command loads rc.yml and finds ``version: 2``, it dispatches through
this module to the new Provider interface instead of the legacy imperative
pipeline. The v1 pipeline stays in cli.py and runs when version is missing
or 1 (start-simpli-api etc. continue to work).
"""

from __future__ import annotations

import dataclasses
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from .config.v2_schema import RcConfigV2, parse as parse_v2
from .provider import (
    DeployContext,
    Provider,
    ProviderNotFoundError,
    SecretRef,
    ServiceSpec,
    get as get_provider,
)


def load_rc_yml(path: str | Path) -> tuple[int, dict, RcConfigV2 | None]:
    """Return ``(version, raw_dict, v2_config_or_None)``.

    version is 1 for legacy, 2 for v2. Only v2 gets parsed into a typed
    RcConfigV2 (v1 stays as a dict for the legacy code to chew on).
    """
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"rc.yml must be a mapping, got {type(raw).__name__}")
    version = int(raw.get("version", 1))
    if version == 2:
        return version, raw, parse_v2(raw)
    return version, raw, None


def _parse_compose_services(compose_path: Path) -> dict[str, dict]:
    """Extract build/image data from docker-compose.yml, keyed by service name."""
    if not compose_path.exists():
        return {}
    with compose_path.open() as f:
        data = yaml.safe_load(f) or {}
    return data.get("services") or {}


def _service_build_info(
    svc_compose: dict, compose_path: Path
) -> tuple[Optional[Path], dict[str, str], Optional[str], Optional[str], Optional[str]]:
    """Return (build_context, build_args, dockerfile, image, target) for a compose svc."""
    build = svc_compose.get("build")
    image = svc_compose.get("image")
    if build is None:
        return None, {}, None, image, None
    compose_dir = compose_path.parent
    if isinstance(build, str):
        return (compose_dir / build).resolve(), {}, None, image, None
    context = build.get("context", ".")
    context_path = (compose_dir / context).resolve()
    args = build.get("args") or {}
    if isinstance(args, list):
        args = dict(kv.split("=", 1) for kv in args if "=" in kv)
    dockerfile = build.get("dockerfile")
    target = build.get("target")
    return (
        context_path,
        {str(k): str(v) for k, v in args.items()},
        dockerfile,
        image,
        target,
    )


_COMPOSE_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_compose_vars(value: str) -> str:
    """Resolve docker-compose-style ``${VAR}`` / ``${VAR:-default}`` references.

    Terraform HCL uses ``${...}`` for its own interpolation and rejects the
    compose-style ``:-default`` form. Resolving here means the rendered HCL
    contains literal strings, with any unresolved var left as the empty
    default (matching `docker compose up` behavior).
    """

    def repl(m: "re.Match[str]") -> str:
        var, default = m.group(1), m.group(2)
        env_val = os.environ.get(var)
        if env_val is not None:
            return env_val
        return default if default is not None else ""

    return _COMPOSE_VAR_RE.sub(repl, value)


def _service_env(svc_compose: dict, compose_path: Optional[Path] = None) -> dict[str, str]:
    """Extract compose `environment:` and `env_file:` into a flat dict.

    Merge order (later wins):
      1. each env_file in declaration order
      2. environment: dict / list

    Supports environment as dict or list, env_file as string or list.
    Relative env_file paths resolve against the compose file's dir.
    Compose-style ${VAR} / ${VAR:-default} references in environment:
    values are expanded against the current process env.
    """
    out: dict[str, str] = {}
    compose_dir = compose_path.parent if compose_path else Path.cwd()

    env_files_raw = svc_compose.get("env_file")
    if env_files_raw is not None:
        env_files: list = (
            [env_files_raw] if isinstance(env_files_raw, str) else list(env_files_raw)
        )
        from .envfile import EnvFileError, parse as _parse_env
        for ref in env_files:
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = (compose_dir / ref_path).resolve()
            try:
                file_vars = _parse_env(ref_path)
            except EnvFileError:
                # Compose silently skips missing env_file in some modes; we
                # surface as warning by populating an empty dict so the
                # service still attempts to deploy. Hard failures should
                # come from the secrets/lifecycle layer, not env discovery.
                file_vars = {}
            out.update(file_vars)

    env = svc_compose.get("environment")
    if env is None:
        environment_dict: dict[str, str] = {}
    elif isinstance(env, dict):
        environment_dict = {str(k): str(v) for k, v in env.items()}
    elif isinstance(env, list):
        environment_dict = {}
        for entry in env:
            if "=" in str(entry):
                k, v = str(entry).split("=", 1)
                environment_dict[k] = v
    else:
        environment_dict = {}
    # environment: wins over env_file
    out.update({k: _expand_compose_vars(v) for k, v in environment_dict.items()})
    return out


def _service_compose_ports(svc_compose: dict) -> list[int]:
    """Parse compose ports[] into a list of unique containerPort ints,
    sorted. Accepts the "host:container" string form or the long-syntax
    dict form. Compose's published port mapping is irrelevant on Fargate
    (each task gets its own ENI), so only the container side matters."""
    out: list[int] = []
    for entry in svc_compose.get("ports") or []:
        if isinstance(entry, dict):
            target = entry.get("target")
            if target is not None:
                out.append(int(target))
            continue
        s = str(entry)
        # Strip optional "host_ip:" prefix.
        if s.count(":") == 2:
            s = s.split(":", 1)[1]
        if ":" in s:
            _host, container = s.split(":", 1)
        else:
            container = s
        # Container side may include /tcp or /udp suffix.
        container = container.split("/", 1)[0].strip()
        if container.isdigit():
            out.append(int(container))
    return sorted(set(out))


def _service_command(svc_compose: dict) -> list[str]:
    """Parse compose ``command:`` into the list form ECS task defs expect.

    Compose accepts two forms:
      - string: ``celery -A config worker --loglevel=info`` — interpreted
        like ``sh -c <string>`` by docker, but rc emits this directly as
        the container CMD which means ECS exec's the string AS A SINGLE
        BINARY NAME (failure mode: '/entrypoint: exec: <whole string>: not
        found' — verified .46.6 run #5 on celery-worker).
      - list: ``["celery", "-A", "config", "worker"]`` — exec form, used
        as-is.

    Use shlex.split for the string case so the result mirrors what compose
    does internally. Quoted args (``"--option=foo bar"``) round-trip
    correctly.
    """
    import shlex
    cmd = svc_compose.get("command")
    if cmd is None:
        return []
    if isinstance(cmd, str):
        return shlex.split(cmd)
    if isinstance(cmd, list):
        return [str(x) for x in cmd]
    return []


def _merge_framework_lifecycle(
    svc: "object",
    svc_compose: dict,
    compose_path: Path,
) -> dict[str, dict]:
    """Merge framework preset lifecycle hooks into a service's lifecycle dict.

    rc-e5u.35.7. The user's explicit rc.yml hooks always win; framework
    presets fill gaps for hooks the user didn't declare. The framework is
    resolved in priority order:
      1. ``services.<svc>.framework`` if explicitly set in rc.yml
      2. otherwise, ``frameworks.detect_framework`` against the compose
         service's Dockerfile (lights up the same surface for users who
         don't bother to declare it)

    Returns the merged lifecycle dict in the shape ServiceSpec.lifecycle
    expects (``hook_name -> {command, auto_on_deploy, run_once,
    interactive, probe}``). Framework-injected hooks default to
    auto_on_deploy=False / run_once=False / interactive=False / probe=None;
    the user can override these by declaring a full lifecycle entry under
    rc.yml.
    """
    from .frameworks import detect_framework, framework_by_name

    out: dict[str, dict] = {}
    # 1. Start with the user's explicit hooks — these win on collision.
    for hook_name, h in (getattr(svc, "lifecycle", None) or {}).items():
        out[hook_name] = {
            "command": list(h.command),
            "auto_on_deploy": h.auto_on_deploy,
            "run_once": h.run_once,
            "interactive": h.interactive,
            "probe": list(h.probe) if h.probe else None,
        }

    # 2. Framework resolution: explicit field wins, else detect.
    explicit_name = getattr(svc, "framework", None)
    fw = None
    if explicit_name:
        fw = framework_by_name(explicit_name)
    if fw is None:
        fw = detect_framework(svc_compose, compose_path)
    if fw is None or not fw.lifecycle_hooks:
        return out

    # 3. Fill in framework-provided hooks the user didn't declare.
    # Interactive hooks (shell, console, dbshell, dbconsole) get
    # interactive=True so the lifecycle CLI attaches a tty when running.
    interactive_hook_names = {
        "shell", "console", "dbshell", "dbconsole", "routes",
    }
    for hook_name, argv in fw.lifecycle_hooks.items():
        if hook_name in out:
            continue
        out[hook_name] = {
            "command": list(argv),
            "auto_on_deploy": False,
            "run_once": False,
            "interactive": hook_name in interactive_hook_names,
            "probe": None,
        }
    return out


def _compose_named_volume_mounts(svc_compose: dict) -> list[dict[str, str]]:
    """Extract NAMED-volume mounts from a compose service.

    Returns a list of ``{"name": <volume>, "mount": <path>}`` for each
    mount that references a docker-compose named volume. Bind mounts
    (``./local:/path``), anonymous volumes (just ``/path``), and
    tmpfs / overlay entries are intentionally skipped — the auto-EFS
    path (rc-e5u.46.11) only fires for declared named volumes the user
    expects to persist.

    Compose accepts three forms:
      - short string: ``"name:/path"``  → named volume.
      - short string: ``"./local:/path"`` → bind mount (skipped).
      - long form dict: ``{"type": "volume", "source": "name", "target": "/p"}``
    """
    out: list[dict[str, str]] = []
    raw = svc_compose.get("volumes")
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if isinstance(entry, str):
            # short-form: split on the FIRST colon. Skip bind mounts
            # (source begins with ``.``, ``/``, or ``~``) and anonymous
            # volumes (no source segment).
            if ":" not in entry:
                continue
            source, _, rest = entry.partition(":")
            source = source.strip()
            if not source or source[0] in {".", "/", "~"}:
                continue
            mount = rest.split(":", 1)[0].strip()
            if not mount:
                continue
            out.append({"name": source, "mount": mount})
        elif isinstance(entry, dict):
            # long-form: type='volume' with source+target.
            if entry.get("type") not in (None, "volume"):
                continue
            src = entry.get("source")
            tgt = entry.get("target")
            if not src or not tgt:
                continue
            # Long-form 'source' that starts with ./ /  ~ is technically a
            # bind mount in compose semantics — skip those too.
            if str(src)[0] in {".", "/", "~"}:
                continue
            out.append({"name": str(src), "mount": str(tgt)})
    return out


_AUTO_SECRET_PATH_DEPTH = 3


def _auto_secret_name_for(env_file_path: Path, compose_dir: Path) -> str:
    """Derive a stable, AWS-safe secret name from an env_file path.

    When env_file is INSIDE compose_dir: slug the relative path. So
    ``.envs/.local/.django`` → ``local-django`` (the leading ``envs`` is
    dropped — Django convention). Mirrors
    init_from_compose.secret_name_from_path so a hand-written rc.yml using
    ``source: file`` produces the same secret name an env_file_auto'd
    rc.yml would.

    When env_file is OUTSIDE compose_dir (rc-e5u.44.22): preserve the LAST
    three path segments instead of falling back to bare basename. Two
    env_files that share a basename across scopes (``.envs/.local/.django``
    vs ``.envs/.staging/.django``) keep distinct names; AND switching
    compose_file location (e.g. moving the rc.yml from in-tree to /tmp)
    does NOT silently rename the SM secret. Earlier behavior collapsed
    both to ``django`` and silently orphaned the populated SM blob,
    causing a deploy-time empty-secret cascade — see rc-e5u.44.20 + .44.22.
    """
    from .init_from_compose import secret_name_from_path
    try:
        rel = env_file_path.resolve().relative_to(compose_dir.resolve())
    except ValueError:
        # Outside compose dir — keep the last N segments (typically env-scope
        # dir + filename) to disambiguate across paths.
        parts = list(env_file_path.parts[-_AUTO_SECRET_PATH_DEPTH:])
        rel = Path(*parts) if parts else Path(env_file_path.name)
    return secret_name_from_path(str(rel))


def _expand_env_file_auto(
    secrets: list,
    compose_services: dict[str, dict],
    compose_path: Path,
) -> tuple[list, set[str], dict[str, list[str]]]:
    """Always-on auto-promotion of compose env_file directives to SM secrets.

    rc-12d: previously this fired only when rc.yml declared `source:
    env_file_auto`. Default rc.yml configs (source=file per secret) skipped
    this path entirely, leaving compose env_file values to land in the task
    def `environment[]` as plaintext. Now runs for every deploy.

    Walks every compose service's ``env_file:`` list, resolves paths against
    the compose file's dir, and produces one ``source: file`` SecretRef per
    unique env_file (auto-named via _auto_secret_name_for).

    rc.yml secrets[] precedence (R3): when a discovered auto-name collides
    with an rc.yml-declared secret name, the rc.yml entry wins — its
    ``path:`` is the SM-content source. The compose-side path is still
    used to determine which service references which secret (R2 routing).

    Returns:
      (expanded_secrets, suppressed_env_keys, per_service_secret_names)

      expanded_secrets: rc.yml secrets[] entries (with env_file_auto
        entries replaced by file entries) plus discovered auto-name
        entries that don't collide with any rc.yml-declared name.
      suppressed_env_keys: union of every key across every discovered
        env_file. Kept for backwards compat with callers that need
        a global suppression set; build_deploy_context uses the
        per_service_secret_names map for actual per-service routing.
      per_service_secret_names: dict mapping compose service name →
        list of secret names whose source path matches one of THAT
        service's env_file references. ECSProvider.emit_terraform
        filters task-def secrets[] per service against this list (R2).
    """
    from .config.v2_schema import SecretRefV2 as _SecRefV2
    from .envfile import EnvFileError, keys as _env_keys

    compose_dir = compose_path.parent

    # 1. Discover every compose env_file referenced by any service. Track
    #    BOTH the auto-name and the (per-service) list of auto-names so we
    #    can route per-service later. Also build a basename → set-of-services
    #    index used in step 2 to link rc.yml-declared file secrets that
    #    refer to the same logical file under a different path (e.g. rc.yml
    #    points at .test/.django while compose points at .local/.django).
    discovered: dict[str, Path] = {}  # auto-name -> abs path
    per_service: dict[str, list[str]] = {}
    basename_to_services: dict[str, set[str]] = {}
    for svc_name, svc_compose in compose_services.items():
        env_files_raw = svc_compose.get("env_file")
        if env_files_raw is None:
            per_service[svc_name] = []
            continue
        entries = (
            [env_files_raw] if isinstance(env_files_raw, str) else list(env_files_raw)
        )
        names_for_this_svc: list[str] = []
        for ref in entries:
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = (compose_dir / ref_path).resolve()
            auto_name = _auto_secret_name_for(ref_path, compose_dir)
            # First-seen wins for the global discovered map (two services
            # sharing an env_file collapse to one secret).
            discovered.setdefault(auto_name, ref_path)
            if auto_name not in names_for_this_svc:
                names_for_this_svc.append(auto_name)
            basename_to_services.setdefault(ref_path.name, set()).add(svc_name)
        per_service[svc_name] = names_for_this_svc

    # 2. Merge with rc.yml secrets[]. rc.yml-declared names WIN (R3) — keep
    #    the rc.yml entry as-is, drop the auto-discovered duplicate.
    #    rc-12d: ALSO link rc.yml file secrets to compose services by
    #    BASENAME of the secret's path. This lets a user declare
    #    `secrets:[{name: django, path: .test/.django}]` and have it
    #    auto-scope to compose services whose env_file uses .django
    #    (any path), avoiding the global-broadcast leak that put
    #    REDIS_URL on postgres in the sentinal repro.
    expanded: list = []
    rc_yml_names: set[str] = set()
    for sec in secrets:
        src = getattr(sec, "source", None)
        if src == "env_file_auto":
            continue  # legacy opt-in marker; replaced unconditionally below
        expanded.append(sec)
        rc_yml_names.add(getattr(sec, "name", None))
        # Basename-link: when the rc.yml file-secret's path basename
        # matches a compose env_file's basename, scope this secret to
        # those services so it's NOT broadcast globally.
        if src == "file":
            sec_path_str = getattr(sec, "path", None)
            if sec_path_str:
                basename = Path(sec_path_str).name
                linked_svcs = basename_to_services.get(basename) or set()
                for linked_svc in linked_svcs:
                    if sec.name not in per_service.get(linked_svc, []):
                        per_service.setdefault(linked_svc, []).append(sec.name)

    # 3. Append auto-discovered entries that don't collide with rc.yml names.
    for name, abs_path in discovered.items():
        if name in rc_yml_names:
            continue
        expanded.append(_SecRefV2(
            name=name, source="file", path=str(abs_path),
        ))

    # 4. Compute suppressed_keys (union across every discovered env_file)
    #    so legacy callers still get a global set. Per-service routing in
    #    build_deploy_context uses per_service.
    suppressed_keys: set[str] = set()
    for abs_path in discovered.values():
        try:
            for k in _env_keys(abs_path):
                suppressed_keys.add(k)
        except EnvFileError:
            pass
    # Plus rc.yml-declared file-sourced secrets — those keys are also in SM.
    for sec in secrets:
        if getattr(sec, "source", None) == "file" and getattr(sec, "path", None):
            sec_path = Path(sec.path)
            if not sec_path.is_absolute():
                sec_path = (compose_path.parent / sec_path).resolve()
            try:
                for k in _env_keys(sec_path):
                    suppressed_keys.add(k)
            except EnvFileError:
                pass

    return expanded, suppressed_keys, per_service


def build_deploy_context(
    v2: RcConfigV2,
    raw: dict,
    rc_yml_path: Path,
) -> DeployContext:
    """Convert a validated RcConfigV2 into a Provider-ready DeployContext."""
    project_dir = rc_yml_path.parent.resolve()

    compose_path = Path(v2.compose_file)
    if not compose_path.is_absolute():
        compose_path = (project_dir / compose_path).resolve()
    compose_services = _parse_compose_services(compose_path)

    # Expand env_file_auto BEFORE building service envs so the suppression
    # set is available — without this, env_file values land in the task-def
    # environment[] AND in secrets[], duplicating the data and defeating
    # the point of using SM.
    v2_secrets_expanded, suppressed_env_keys, per_service_secret_names = (
        _expand_env_file_auto(list(v2.secrets), compose_services, compose_path)
    )

    # Resolve the deploy set: union of compose services + rc.yml services,
    # filtered through compose.include (whitelist) or compose.exclude
    # (blacklist).
    compose_names = set(compose_services.keys())
    rc_names = set(v2.services.keys())
    deploy_names = compose_names | rc_names
    if v2.compose:
        if v2.compose.include is not None:
            unknown = set(v2.compose.include) - deploy_names
            if unknown:
                raise ValueError(
                    f"compose.include lists service(s) not present in compose "
                    f"or rc.yml: {sorted(unknown)}"
                )
            deploy_names = set(v2.compose.include)
        elif v2.compose.exclude is not None:
            deploy_names = deploy_names - set(v2.compose.exclude)

    # rc-e5u.46.11: import the singleton heuristic from the ECS provider so
    # we can decide whether to auto-promote a compose named-volume mount to
    # an EFS volume. The provider re-applies the same check at terraform
    # emit time; doing it here AS WELL means the ServiceSpec carries the
    # synthesized volumes[] entries (the provider already knows how to turn
    # those into EFS access points + task-def mounts).
    from .provider.ecs.provider import _looks_like_singleton_scheduler

    services: dict[str, ServiceSpec] = {}
    for name in sorted(deploy_names):
        svc = v2.services.get(name)
        svc_compose = compose_services.get(name) or {}
        bc, bargs, dfile, img, target = _service_build_info(svc_compose, compose_path)
        all_compose_ports = _service_compose_ports(svc_compose)
        env = _service_env(svc_compose, compose_path)
        # rc-12d: per-service plaintext suppression. Drop any key that THIS
        # service now sources via SM (either through compose env_file
        # auto-promotion or through an rc.yml-declared file secret), so
        # the task def doesn't ship the same value as both plaintext env
        # and a secrets[] reference. Per-service (not global) so a key
        # that only exists in service A's env_file isn't also stripped
        # from service B's plaintext env when B sets it via compose
        # `environment:`. Keys explicitly set via compose `environment:`
        # (or rc.yml's services.<svc>.env override) MUST survive — those
        # are user-intended plaintext overrides that win on collision.
        from .envfile import EnvFileError as _EFE, keys as _ekeys
        explicit_plaintext_keys: set[str] = set()
        compose_environment = svc_compose.get("environment")
        if isinstance(compose_environment, dict):
            explicit_plaintext_keys.update(str(k) for k in compose_environment.keys())
        elif isinstance(compose_environment, list):
            for entry in compose_environment:
                if "=" in str(entry):
                    explicit_plaintext_keys.add(str(entry).split("=", 1)[0])
        svc_secret_names = set(per_service_secret_names.get(name, []))
        # Also include rc.yml-declared file secrets that match a name in
        # this service's env_file_secret_names — rc.yml's path wins on
        # collision (R3) so we use the rc.yml path's key set.
        svc_suppressed: set[str] = set()
        for sec in v2_secrets_expanded:
            if getattr(sec, "name", None) not in svc_secret_names:
                continue
            if getattr(sec, "source", None) != "file":
                continue
            sec_path_str = getattr(sec, "path", None)
            if not sec_path_str:
                continue
            sec_path = Path(sec_path_str)
            if not sec_path.is_absolute():
                sec_path = (project_dir / sec_path).resolve()
            try:
                for k in _ekeys(sec_path):
                    svc_suppressed.add(k)
            except _EFE:
                pass
        # Preserve explicit plaintext overrides on collision.
        svc_suppressed -= explicit_plaintext_keys
        if svc_suppressed:
            env = {k: v for k, v in env.items() if k not in svc_suppressed}
        cmd = _service_command(svc_compose)
        compose_named_mounts = _compose_named_volume_mounts(svc_compose)
        if svc is not None:
            # rc.yml-declared service; honor every override.
            # rc-e5u.46.4: merge services.<svc>.env ON TOP of compose env so
            # rc.yml wins on key collision. Used by the scaffolder's
            # testing_defaults injection (DJANGO_ALLOWED_HOSTS=* etc.) and by
            # any user who wants to override an env var without editing
            # compose. env_file_auto suppression already ran above, so a key
            # the user pinned in rc.yml.env still lands in plaintext (correct
            # — these aren't secrets, they're host-validation knobs).
            if svc.env:
                env = {**env, **{k: str(v) for k, v in svc.env.items()}}
            primary_port = svc.port or (all_compose_ports[0] if all_compose_ports else None)
            extras = [p for p in all_compose_ports if p != primary_port]
            # rc-e5u.46.1: rc.yml services.<svc>.dockerfile overrides compose's
            # build.dockerfile. Path is interpreted relative to the build
            # context (matching compose semantics) — ImageBuilder will join
            # it to spec.build_context. Lets users keep their compose file
            # untouched while pointing rc at an ECS-aware Dockerfile (e.g.
            # one generated by `rc fix nginx-conf`).
            dockerfile_override = svc.dockerfile if svc.dockerfile else dfile
            # rc-e5u.46.11: auto-promote compose named-volume mounts to EFS
            # for singleton schedulers (celery-beat / -scheduler / -cron),
            # but ONLY when the user hasn't already declared volumes for
            # this service in rc.yml. Explicit rc.yml volumes always win —
            # they're the user's escape hatch.
            #
            # Why singleton-only: a stateless multi-instance worker can
            # safely lose its on-disk state across replicas (or never have
            # any), so we don't surprise the user with EFS-by-default for
            # every compose volume. Singletons are different: by definition
            # the volume IS the state and dropping it = crash-loop (verified
            # 2026-04-26 with celery-beat needing /celery-beat/celerybeat-
            # schedule).
            volumes_list = list(svc.volumes or [])
            if (
                not volumes_list
                and compose_named_mounts
                and _looks_like_singleton_scheduler(name, cmd)
            ):
                volumes_list = list(compose_named_mounts)
            services[name] = ServiceSpec(
                name=name,
                cpu=svc.cpu,
                memory=svc.memory,
                replicas=svc.replicas,
                type=svc.type,
                launch_type=svc.launch_type,
                health_check_path=svc.health_check_path,
                public=svc.public,
                port=primary_port,
                ephemeral_storage=svc.ephemeral_storage,
                volumes=volumes_list,
                dev_volumes=list(svc.dev_volumes or []),
                build_context=bc,
                build_args=bargs,
                dockerfile=dockerfile_override,
                image=img,
                target=target,
                extra_ports=extras,
                env=env,
                command=cmd,
                lifecycle=_merge_framework_lifecycle(
                    svc, svc_compose, compose_path,
                ),
                domain=svc.domain,
                aliases=list(svc.aliases or []),
                env_file_secret_names=list(per_service_secret_names.get(name, [])),
            )
        else:
            # Compose-only service: derive sensible defaults. type=worker
            # when no port (background processes), type=application when
            # the compose has a ports[] entry, never public by default.
            inferred_type = "application" if all_compose_ports else "worker"
            primary_port = all_compose_ports[0] if all_compose_ports else None
            extras = all_compose_ports[1:] if len(all_compose_ports) > 1 else []
            # rc-e5u.46.11: same auto-EFS-for-singleton path as the
            # rc.yml-declared branch. Compose-only services don't get the
            # rc.yml escape hatch (no svc.volumes to honor), so the trigger
            # is purely "named mount + singleton command".
            auto_volumes: list[dict] = []
            if compose_named_mounts and _looks_like_singleton_scheduler(name, cmd):
                auto_volumes = list(compose_named_mounts)
            services[name] = ServiceSpec(
                name=name,
                cpu=256,
                memory=512,
                type=inferred_type,
                port=primary_port,
                volumes=auto_volumes,
                build_context=bc,
                build_args=bargs,
                dockerfile=dfile,
                image=img,
                target=target,
                extra_ports=extras,
                env=env,
                command=cmd,
                env_file_secret_names=list(per_service_secret_names.get(name, [])),
            )

    secrets = [
        SecretRef(
            name=s.name, source=s.source,
            path=s.path, arn=s.arn, ref=s.ref,
        )
        for s in v2_secrets_expanded
    ]

    tf_backend = dataclasses.asdict(v2.terraform.backend)
    # Terraform rejects None-valued backend fields; strip them.
    tf_backend = {k: v for k, v in tf_backend.items() if v is not None}
    extra = tf_backend.pop("extra", None)
    if extra:
        tf_backend.update(extra)

    return DeployContext(
        project=v2.project,
        compose_path=compose_path,
        rc_yml_v2=raw,
        provider_config=v2.provider_config,
        tf_backend_config=tf_backend,
        working_dir=project_dir,
        services=services,
        secrets=secrets,
    )


_PROVIDER_MODULES = {
    "ecs": "remote_compose.provider.ecs",
    "k8s": "remote_compose.provider.k8s",
    "fake": "remote_compose.provider.fake",
}


def resolve_provider(v2: RcConfigV2) -> Provider:
    # Eagerly import the named provider's module so its register() call fires.
    mod_name = _PROVIDER_MODULES.get(v2.provider)
    if mod_name:
        try:
            __import__(mod_name)
        except ImportError as exc:
            raise ProviderNotFoundError(
                f"provider {v2.provider!r}: install extras "
                f"(`pip install 'remote-compose[{v2.provider}]'`). "
                f"Import failed: {exc}"
            )
    try:
        cls = get_provider(v2.provider)
    except ProviderNotFoundError:
        raise ProviderNotFoundError(
            f"provider {v2.provider!r} not registered. "
            f"Known: install extras (pip install 'remote-compose[{v2.provider}]') "
            f"or check the spelling."
        )
    return cls()


# ---------------------------------------------------------------------------
# Output formatters — keep step UX consistent with the legacy pipeline
# ---------------------------------------------------------------------------


def render_plan(result) -> str:
    lines = [
        "",
        f"  Terraform plan:",
        f"    create:  {result.create}",
        f"    update:  {result.update}",
        f"    destroy: {result.destroy}",
    ]
    if result.create == 0 and result.update == 0 and result.destroy == 0:
        lines.append("    (no changes — infrastructure matches config)")
    # Warnings come from compose-file detectors (rc-e5u.44.6/.7/.8/.9):
    # surface them right under the diff so the user reads them before
    # running rc deploy.
    if getattr(result, "warnings", None):
        lines.append("")
        lines.append("  Warnings:")
        for w in result.warnings:
            lines.append(f"    - {w}")
    return "\n".join(lines)


def render_deploy(result) -> str:
    lines = [
        "",
        f"  Deploy complete",
        f"    revision: {result.revision_id}",
        f"    services: {', '.join(sorted(result.services))}",
        f"    duration: {result.duration_s:.1f}s",
    ]
    alb = (result.terraform_outputs or {}).get("alb_dns_name")
    if isinstance(alb, dict):
        alb_v = alb.get("value")
        if alb_v:
            lines.append(f"    ALB:      http://{alb_v}")
    if result.warnings:
        lines.append("")
        lines.append("  Warnings:")
        for w in result.warnings:
            lines.append(f"    - {w}")
    return "\n".join(lines)


def _auto_push_empty_secrets_if_any(rc_path: Path, v2, raw: dict) -> None:
    """If any file-sourced SM secret is empty after a deploy, auto-run
    `rc secrets push`. Pairs with rc-e5u.44.20 — see _detect_empty_file_secrets
    in cli.py.

    Detection uses the SAME secret-name expansion as the deploy itself so we
    catch every case (env_file_auto, source: file, etc.). Quiet no-op when
    everything's populated. Errors during detection are surfaced as warnings
    rather than fatal — the deploy already succeeded by the time we run.
    """
    import click
    secrets = list(v2.secrets or [])
    if not secrets:
        return

    # Resolve compose path the same way build_deploy_context does so the
    # env_file_auto expansion matches.
    compose_path = Path(v2.compose_file)
    if not compose_path.is_absolute():
        compose_path = (Path(rc_path).parent / compose_path).resolve()
    compose_services = _parse_compose_services(compose_path)
    expanded, _suppressed, _per_svc = _expand_env_file_auto(
        secrets, compose_services, compose_path,
    )
    file_secrets = [s for s in expanded if getattr(s, "source", None) == "file"]
    if not file_secrets:
        return

    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    region = ecs_cfg.get("region")
    aws_profile = ecs_cfg.get("aws_profile")
    if not region:
        return  # provider.deploy would have errored earlier; skip silently

    try:
        from .cli import _detect_empty_file_secrets, _secrets_push_v2
        empty = _detect_empty_file_secrets(v2, region, aws_profile, file_secrets)
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"\n  WARN: could not check SM secret population: {exc!s} — "
            f"run `rc secrets push` if services fail to start.",
            err=True,
        )
        return
    if not empty:
        return

    click.echo(
        f"\n  Auto-pushing {len(empty)} empty secret(s): "
        f"{', '.join(sorted(empty))}"
    )
    click.echo(
        f"  (terraform created these but `rc secrets push` was never run "
        f"— pushing now so tasks can start)"
    )
    try:
        _secrets_push_v2(str(rc_path), rollout=True)
    except SystemExit:
        # _secrets_push_v2 raises Exit(1) on env-file errors; propagate
        # so the user sees the failure. The deploy itself already succeeded.
        raise
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"\n  WARN: auto-push failed: {exc!s}. Run `rc secrets push` "
            f"manually to recover.",
            err=True,
        )


def _run_auto_on_deploy_hooks(
    provider, ctx, v2, services_filter: Optional[list[str]] = None,
) -> None:
    """Run every services[*].lifecycle.<hook> with auto_on_deploy=true,
    in declaration order. Honors run_once via probe. Hook failures are
    surfaced as warnings, not deploy failures — the user can rerun a
    failing hook with `rc lifecycle <hook>` and see full output.

    When ``services_filter`` is set, only hooks on those services run —
    matches the deploy filter (e.g. ``rc deploy --services django`` only
    triggers django.migrate, not nginx.reload).
    """
    import click as _click
    allowed = set(services_filter) if services_filter else None
    hooks: list[tuple[str, str, "object"]] = []
    for svc_name, svc in v2.services.items():
        if allowed is not None and svc_name not in allowed:
            continue
        for hook_name, hook in (svc.lifecycle or {}).items():
            if hook.auto_on_deploy:
                hooks.append((svc_name, hook_name, hook))
    if not hooks:
        return
    _click.echo("\n  Running auto_on_deploy lifecycle hooks:")
    for svc_name, hook_name, hook in hooks:
        if hook.run_once and hook.probe:
            probe = provider.exec(ctx, svc_name, list(hook.probe))
            if probe.exit_code == 0:
                _click.echo(f"    {hook_name} on {svc_name}: skipped (run_once probe satisfied)")
                continue
        _click.echo(f"    {hook_name} on {svc_name}...")
        result = provider.exec(ctx, svc_name, list(hook.command))
        if result.exit_code == 0:
            _click.echo(f"      ok")
        else:
            _click.echo(
                f"      FAILED (exit {result.exit_code}); "
                f"rerun with `rc lifecycle {hook_name} {svc_name}` to see full output",
                err=True,
            )


def run_auto_on_deploy_hooks_for_path(
    config_path: str | Path | None,
    services_filter: Optional[list[str]] = None,
    wait_for_stable: bool = True,
) -> None:
    """Run auto_on_deploy lifecycle hooks for the v2 stack at config_path.

    Companion to ``dispatch_if_v2(... defer_lifecycle_hooks=True)``: ``rc up``
    defers hooks until after the outer secrets-push + force-roll have
    completed, then calls this helper so the hooks land on tasks with real
    env vars (rc-3q9). Silent no-op for v1 / non-existent rc.yml.

    When ``wait_for_stable=True`` (default for the rc-up path), polls the
    target services for ECS deployment stability — a single PRIMARY
    deployment with rolloutState=COMPLETED, meaning new task definition
    is fully live and old task definition has drained. Up to 5 minutes;
    override via RC_HOOK_WAIT_TIMEOUT_S.
    """
    import click as _click
    p = Path(config_path) if config_path else Path.cwd() / "rc.yml"
    if not p.exists():
        return
    try:
        version, raw, v2 = load_rc_yml(p)
    except Exception:
        return
    if version != 2 or v2 is None:
        return
    if not v2.services:
        return
    # Skip work when no service has an auto_on_deploy hook in the first place.
    has_auto = any(
        getattr(h, "auto_on_deploy", False)
        for svc in v2.services.values()
        for h in (svc.lifecycle or {}).values()
    )
    if not has_auto:
        return

    ctx = build_deploy_context(v2, raw, p)
    provider = resolve_provider(v2)

    if wait_for_stable:
        ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
        cluster = ecs_cfg.get("cluster") or f"{v2.project}-cluster"
        targets = sorted(set(services_filter)) if services_filter else sorted(v2.services.keys())
        try:
            session = provider.session_factory(ctx)
            ecs_client = session.client("ecs")
        except Exception as exc:  # noqa: BLE001
            _click.echo(
                f"  WARN: skipping deployment-stability wait — could not "
                f"contact ECS: {type(exc).__name__}: {exc}",
                err=True,
            )
        else:
            import os as _os
            wait_budget = int(_os.environ.get("RC_HOOK_WAIT_TIMEOUT_S", "300"))
            wait_interval = float(_os.environ.get("RC_HOOK_WAIT_INTERVAL_S", "10"))
            deadline = time.monotonic() + wait_budget
            _click.echo(
                f"  Waiting up to {wait_budget}s for "
                f"{len(targets)} service(s) to reach steady state before "
                f"running auto_on_deploy hooks..."
            )
            while True:
                try:
                    desc = ecs_client.describe_services(
                        cluster=cluster, services=targets,
                    )
                except Exception as exc:  # noqa: BLE001
                    _click.echo(
                        f"  WARN: describe_services failed: {exc!s}",
                        err=True,
                    )
                    break
                pending = []
                for svc in desc.get("services", []) or []:
                    deployments = svc.get("deployments") or []
                    if len(deployments) != 1:
                        pending.append(svc.get("serviceName"))
                        continue
                    dep = deployments[0]
                    rollout = dep.get("rolloutState")
                    # rolloutState may be None on classic ECS deployment
                    # controllers — fall back to runningCount == desiredCount
                    # in that case.
                    if rollout is None:
                        if dep.get("runningCount") != dep.get("desiredCount"):
                            pending.append(svc.get("serviceName"))
                    elif rollout != "COMPLETED":
                        pending.append(svc.get("serviceName"))
                if not pending:
                    break
                if time.monotonic() > deadline:
                    _click.echo(
                        f"  WARN: services {pending} did not stabilize "
                        f"within {wait_budget}s — running hooks anyway "
                        f"(may hit old tasks).",
                        err=True,
                    )
                    break
                time.sleep(wait_interval)

    _run_auto_on_deploy_hooks(provider, ctx, v2, services_filter=services_filter)


def dispatch_if_v2(config_path: str | Path | None, command: str, **kwargs) -> bool:
    """Dispatch a CLI command through the v2 Provider pathway.

    Returns True when rc.yml is v2 and the command has run (caller should
    return early). Returns False when rc.yml is v1/legacy (caller continues
    with the existing pipeline).

    Commands handled: plan, deploy, destroy, status. Others return False.
    """
    import click

    path = Path(config_path) if config_path else Path.cwd() / "rc.yml"
    if not path.exists():
        return False
    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        return False

    ctx = build_deploy_context(v2, raw, path)
    provider = resolve_provider(v2)

    click.echo(f"\nRemote Compose v2 — provider={v2.provider} project={v2.project}")

    if command == "plan":
        result = provider.plan(ctx)
        # Compose-file detectors run independently of the provider so they
        # work even when the provider's own plan() doesn't populate
        # warnings (fake / k8s today). Merge & dedupe.
        from .compose_warnings import collect_compose_warnings
        compose_warns = collect_compose_warnings(ctx.compose_path, raw)
        existing = list(getattr(result, "warnings", []) or [])
        for w in compose_warns:
            if w not in existing:
                existing.append(w)
        result.warnings = existing
        click.echo(render_plan(result))
        return True

    if command == "deploy":
        # Dev mode flag (rc-e5u.45.8): when True the provider materializes
        # services[*].dev_volumes as EFS-backed bind mounts so `rc dev push`
        # can stream local source into the live task. Production deploys
        # leave this False and dev_volumes are ignored entirely.
        if kwargs.get("dev"):
            ctx.dev_mode = True
            click.echo(
                "  Dev mode: dev_volumes will be EFS-backed for hot reload via "
                "`rc dev push`."
            )
        if kwargs.get("skip_terraform"):
            ctx.skip_terraform = True
            click.echo(
                "  No-state mode: skipping terraform entirely. "
                "Will only rebuild images + force-roll services."
            )
        if kwargs.get("skip_force_roll"):
            # rc-1bk: caller (today: rc up) wants to handle the rollout
            # itself after pushing secrets. Build + push images here, but
            # don't update_service so tasks aren't rolled with placeholder
            # secrets.
            ctx.skip_force_roll = True
        ttl = kwargs.get("ttl")
        if ttl:
            from datetime import datetime, timezone
            from .ephemeral import (
                parse_duration, register_stack, to_iso_utc,
            )
            try:
                delta = parse_duration(ttl)
            except ValueError as exc:
                click.echo(f"--ttl: {exc}", err=True)
                raise click.exceptions.Exit(1)
            expires_dt = datetime.now(timezone.utc) + delta
            ctx.expires_at = to_iso_utc(expires_dt)
            ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
            tf_dir = Path(ctx.working_dir) / "terraform"
            register_stack(
                project=v2.project,
                region=ecs_cfg.get("region") or "",
                expires_at=ctx.expires_at,
                rc_yml_path=str(path.resolve()),
                terraform_dir=str(tf_dir.resolve()),
                aws_profile=ecs_cfg.get("aws_profile"),
            )
            click.echo(
                f"  Ephemeral: stack expires at {ctx.expires_at} "
                f"(in {ttl}); reap with `rc reap`."
            )
        services_filter = kwargs.get("services")
        if services_filter:
            click.echo(
                f"  Single-service deploy: only {', '.join(sorted(services_filter))} "
                f"will rebuild + roll; other services keep their current revision."
            )
        tag = kwargs.get("tag")
        if tag and tag != "latest":
            click.echo(
                f"  Tag deploy: --tag {tag!r} — if {tag} exists in ECR, "
                f"docker build is skipped + tag is re-applied as :latest."
            )
        result = provider.deploy(
            ctx, services_filter=services_filter, tag=tag,
        )
        click.echo(render_deploy(result))
        # rc-e5u.44.20: detect empty file-sourced SM secrets after deploy
        # and auto-push to populate. Catches the silent-fail cascade where
        # terraform created the SM resource but `rc secrets push` was never
        # run for it (e.g. compose_file rename → env_file_auto generated a
        # different secret name → tasks fail to start with 'secret X did
        # not contain json key Y'). Push is idempotent + only fires on
        # ACTUALLY empty blobs.
        _auto_push_empty_secrets_if_any(path, v2, raw)
        # rc-3q9: when invoked from `rc up`, the orchestrator pushes secrets
        # + force-rolls AFTER this dispatcher returns, then runs hooks once
        # the latest task definition is live. Running hooks HERE on a fresh
        # deploy hits the still-old task (with placeholder env vars) →
        # 'manage.py migrate' fails to connect to Postgres → exit 254 →
        # noisy false alarm. defer_lifecycle_hooks=True opts into that
        # deferred run; default behavior (rc deploy / rc deploy --services)
        # still runs hooks right here for backward compat.
        if not kwargs.get("defer_lifecycle_hooks"):
            # On --services deploys, only run hooks for the targeted service(s)
            # — e.g., `rc deploy --services django` triggers django.migrate but
            # not nginx.reload.
            _run_auto_on_deploy_hooks(provider, ctx, v2, services_filter=services_filter)
        return True

    if command == "destroy":
        if not kwargs.get("yes"):
            if not click.confirm(
                f"\n  This will destroy ALL resources for {v2.project} via "
                f"terraform. Continue?", default=False,
            ):
                click.echo("  aborted.")
                raise click.exceptions.Exit(1)
        provider.destroy(ctx)
        click.echo("  Destroy complete.")
        return True

    if command == "status":
        report = provider.status(ctx)
        click.echo(render_status(report))
        return True

    # Unknown v2 command — let the legacy handler try.
    return False


def render_status(report) -> str:
    if not report.services:
        return "  (no services deployed)"
    max_name = max(len(s.name) for s in report.services)
    # Show task def revision when available (.44.24). Format "running/latest"
    # so stale services jump out: "1 → 2" means 'running 1, latest 2'.
    show_revs = any(
        getattr(s, "running_revision", None) is not None
        or getattr(s, "latest_revision", None) is not None
        for s in report.services
    )
    headers = ["service".ljust(max_name), "desired".rjust(7),
               "running".rjust(7), "health".ljust(10)]
    if show_revs:
        headers.append("revision".ljust(12))
    lines = ["  " + "  ".join(headers)]
    lines.append("  " + "-" * (sum(len(h) for h in headers) + 2 * (len(headers) - 1)))
    for s in report.services:
        cells = [
            s.name.ljust(max_name),
            f"{s.desired:>7}",
            f"{s.running:>7}",
            s.health.ljust(10),
        ]
        if show_revs:
            run = s.running_revision if s.running_revision is not None else "?"
            lat = s.latest_revision if s.latest_revision is not None else "?"
            arrow = "→" if getattr(s, "is_stale", False) else "="
            cells.append(f"{run} {arrow} {lat}".ljust(12))
        lines.append("  " + "  ".join(cells))

    stale_services = [s for s in report.services if getattr(s, "is_stale", False)]
    if stale_services:
        lines.append("")
        names = ", ".join(s.name for s in stale_services)
        lines.append(
            f"  STALE: {names} on older revision than the latest task def. "
            f"Run `rc deploy --reconcile` to force-roll."
        )
    if report.ingress_url:
        lines.append("")
        lines.append(f"  ingress: {report.ingress_url}")
    return "\n".join(lines)
