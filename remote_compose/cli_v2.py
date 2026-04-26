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
    cmd = svc_compose.get("command")
    if cmd is None:
        return []
    if isinstance(cmd, str):
        return [cmd]
    if isinstance(cmd, list):
        return [str(x) for x in cmd]
    return []


def _auto_secret_name_for(env_file_path: Path, compose_dir: Path) -> str:
    """Derive a stable, AWS-safe secret name from an env_file path.

    Names are slugged from the env_file path RELATIVE to the compose dir so
    `/big/abs/path/proj/.envs/.local/.django` and `.envs/.local/.django`
    (declared from the proj/ dir) produce the same `local-django` slug.
    Mirrors init_from_compose.secret_name_from_path so a hand-written rc.yml
    using `source: file` produces the same secret name an env_file_auto'd
    rc.yml would.
    """
    from .init_from_compose import secret_name_from_path
    try:
        rel = env_file_path.resolve().relative_to(compose_dir.resolve())
    except ValueError:
        # env_file lives outside compose dir (e.g., absolute path elsewhere) —
        # fall back to the basename so we don't leak host paths into AWS.
        rel = Path(env_file_path.name)
    return secret_name_from_path(str(rel))


def _expand_env_file_auto(
    secrets: list,
    compose_services: dict[str, dict],
    compose_path: Path,
) -> tuple[list, set[str]]:
    """Expand `source: env_file_auto` SecretRefV2 entries.

    For each env_file_auto entry, walks every compose service's `env_file:`
    list, resolves paths against the compose file's dir, and produces one
    `source: file` SecretRef per unique env_file. The original auto entry
    is dropped from the returned list.

    Returns: (new_secrets_list, set_of_env_keys_now_in_sm). The keys set
    is used by build_deploy_context to strip env_file values from each
    service's task-def `environment[]` so the same value isn't shipped as
    both plaintext env and an SM secret reference.
    """
    from .config.v2_schema import SecretRefV2 as _SecRefV2
    from .envfile import EnvFileError, keys as _env_keys

    has_auto = any(getattr(s, "source", None) == "env_file_auto" for s in secrets)
    if not has_auto:
        return list(secrets), set()

    compose_dir = compose_path.parent
    discovered: dict[str, Path] = {}  # secret-name -> absolute env_file path
    for svc_compose in compose_services.values():
        env_files_raw = svc_compose.get("env_file")
        if env_files_raw is None:
            continue
        entries = (
            [env_files_raw] if isinstance(env_files_raw, str) else list(env_files_raw)
        )
        for ref in entries:
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = (compose_dir / ref_path).resolve()
            name = _auto_secret_name_for(ref_path, compose_dir)
            # First-seen wins. Two compose services that share an env_file
            # collapse to one secret.
            discovered.setdefault(name, ref_path)

    suppressed_keys: set[str] = set()
    expanded: list = []
    for sec in secrets:
        if getattr(sec, "source", None) == "env_file_auto":
            continue  # drop; replaced by per-file entries below
        expanded.append(sec)
    for name, abs_path in discovered.items():
        expanded.append(_SecRefV2(
            name=name, source="file", path=str(abs_path),
        ))
        try:
            for k in _env_keys(abs_path):
                suppressed_keys.add(k)
        except EnvFileError:
            # Same lenience as _service_env: a missing env_file still
            # produces a secret entry (for terraform), but we skip key
            # suppression so we don't mask a typo by deleting nothing.
            pass

    return expanded, suppressed_keys


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
    v2_secrets_expanded, suppressed_env_keys = _expand_env_file_auto(
        list(v2.secrets), compose_services, compose_path,
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

    services: dict[str, ServiceSpec] = {}
    for name in sorted(deploy_names):
        svc = v2.services.get(name)
        svc_compose = compose_services.get(name) or {}
        bc, bargs, dfile, img, target = _service_build_info(svc_compose, compose_path)
        all_compose_ports = _service_compose_ports(svc_compose)
        env = _service_env(svc_compose, compose_path)
        if suppressed_env_keys:
            # env_file_auto: drop any key now sourced from SM so the task def
            # doesn't leak the same value via plaintext environment[].
            env = {k: v for k, v in env.items() if k not in suppressed_env_keys}
        cmd = _service_command(svc_compose)
        if svc is not None:
            # rc.yml-declared service; honor every override.
            primary_port = svc.port or (all_compose_ports[0] if all_compose_ports else None)
            extras = [p for p in all_compose_ports if p != primary_port]
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
                volumes=list(svc.volumes or []),
                build_context=bc,
                build_args=bargs,
                dockerfile=dfile,
                image=img,
                target=target,
                extra_ports=extras,
                env=env,
                command=cmd,
                lifecycle={
                    hook_name: {
                        "command": list(h.command),
                        "auto_on_deploy": h.auto_on_deploy,
                        "run_once": h.run_once,
                        "interactive": h.interactive,
                        "probe": list(h.probe) if h.probe else None,
                    }
                    for hook_name, h in (svc.lifecycle or {}).items()
                },
                domain=svc.domain,
                aliases=list(svc.aliases or []),
            )
        else:
            # Compose-only service: derive sensible defaults. type=worker
            # when no port (background processes), type=application when
            # the compose has a ports[] entry, never public by default.
            inferred_type = "application" if all_compose_ports else "worker"
            primary_port = all_compose_ports[0] if all_compose_ports else None
            extras = all_compose_ports[1:] if len(all_compose_ports) > 1 else []
            services[name] = ServiceSpec(
                name=name,
                cpu=256,
                memory=512,
                type=inferred_type,
                port=primary_port,
                build_context=bc,
                build_args=bargs,
                dockerfile=dfile,
                image=img,
                target=target,
                extra_ports=extras,
                env=env,
                command=cmd,
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


def _run_auto_on_deploy_hooks(provider, ctx, v2) -> None:
    """Run every services[*].lifecycle.<hook> with auto_on_deploy=true,
    in declaration order. Honors run_once via probe. Hook failures are
    surfaced as warnings, not deploy failures — the user can rerun a
    failing hook with `rc lifecycle <hook>` and see full output."""
    import click as _click
    hooks: list[tuple[str, str, "object"]] = []
    for svc_name, svc in v2.services.items():
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
        result = provider.deploy(ctx)
        click.echo(render_deploy(result))
        _run_auto_on_deploy_hooks(provider, ctx, v2)
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
    lines = [
        f"  {'service'.ljust(max_name)}  {'desired':>7}  {'running':>7}  {'health':<10}"
    ]
    lines.append("  " + "-" * (max_name + 30))
    for s in report.services:
        lines.append(
            f"  {s.name.ljust(max_name)}  {s.desired:>7}  {s.running:>7}  {s.health:<10}"
        )
    if report.ingress_url:
        lines.append("")
        lines.append(f"  ingress: {report.ingress_url}")
    return "\n".join(lines)
