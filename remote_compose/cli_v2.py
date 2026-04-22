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
) -> tuple[Optional[Path], dict[str, str], Optional[str], Optional[str]]:
    """Return (build_context, build_args, dockerfile, image) for a compose svc."""
    build = svc_compose.get("build")
    image = svc_compose.get("image")
    if build is None:
        return None, {}, None, image
    compose_dir = compose_path.parent
    if isinstance(build, str):
        return (compose_dir / build).resolve(), {}, None, image
    context = build.get("context", ".")
    context_path = (compose_dir / context).resolve()
    args = build.get("args") or {}
    if isinstance(args, list):
        args = dict(kv.split("=", 1) for kv in args if "=" in kv)
    dockerfile = build.get("dockerfile")
    return context_path, {str(k): str(v) for k, v in args.items()}, dockerfile, image


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


def _service_env(svc_compose: dict) -> dict[str, str]:
    """Extract compose `environment:` into a flat string dict.

    Supports both the dict form ({VAR: value}) and the list form
    ([VAR=value, VAR2=value2]). Compose-style ${VAR} / ${VAR:-default}
    references are expanded at parse time using the current process env.
    """
    env = svc_compose.get("environment")
    if env is None:
        raw = {}
    elif isinstance(env, dict):
        raw = {str(k): str(v) for k, v in env.items()}
    elif isinstance(env, list):
        raw = {}
        for entry in env:
            if "=" in str(entry):
                k, v = str(entry).split("=", 1)
                raw[k] = v
    else:
        raw = {}
    return {k: _expand_compose_vars(v) for k, v in raw.items()}


def _service_command(svc_compose: dict) -> list[str]:
    cmd = svc_compose.get("command")
    if cmd is None:
        return []
    if isinstance(cmd, str):
        return [cmd]
    if isinstance(cmd, list):
        return [str(x) for x in cmd]
    return []


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

    services: dict[str, ServiceSpec] = {}
    for name, svc in v2.services.items():
        svc_compose = compose_services.get(name) or {}
        bc, bargs, dfile, img = _service_build_info(svc_compose, compose_path)
        services[name] = ServiceSpec(
            name=name,
            cpu=svc.cpu,
            memory=svc.memory,
            replicas=svc.replicas,
            type=svc.type,
            launch_type=svc.launch_type,
            health_check_path=svc.health_check_path,
            public=svc.public,
            port=svc.port,
            ephemeral_storage=svc.ephemeral_storage,
            volumes=list(svc.volumes or []),
            build_context=bc,
            build_args=bargs,
            dockerfile=dfile,
            image=img,
            env=_service_env(svc_compose),
            command=_service_command(svc_compose),
        )

    secrets = [
        SecretRef(
            name=s.name, source=s.source,
            path=s.path, arn=s.arn, ref=s.ref,
        )
        for s in v2.secrets
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
        click.echo(render_plan(result))
        return True

    if command == "deploy":
        result = provider.deploy(ctx)
        click.echo(render_deploy(result))
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
