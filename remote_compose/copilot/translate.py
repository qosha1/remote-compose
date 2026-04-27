"""Translate Copilot manifests → rc.yml v2 + docker-compose.yml.

Each translator is a focused function:
    translate_service_type(svc)  -> (rc_yml_overrides, warnings)
    translate_image(svc)         -> (compose_build_block, warnings)
    translate_resources(svc)     -> (rc_yml_overrides, warnings)
    translate_storage(svc)       -> (rc_yml_overrides, warnings)
    translate_env_and_secrets(svc) -> (compose_env, rc_secrets, warnings)

A composer (later — see translate.compose_app) wires them into a
single rc.yml + compose pair.

Warnings are typed dataclasses, not strings, so callers can group +
display them by category in a final import summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .discover import CopilotApp, CopilotService


# ---------------------------------------------------------------------
# Warning types — typed so the CLI summary can group/format by kind.
# ---------------------------------------------------------------------

@dataclass
class TranslationWarning:
    """Base. Subclasses get specific category names."""
    service: str
    message: str


@dataclass
class UnsupportedServiceTypeWarning(TranslationWarning):
    """Copilot service type that has no clean ECS equivalent.

    Includes Request-Driven Web Service (App Runner runtime) and
    Static Site (CloudFront + S3). The translator emits the best-
    effort partial output OR sets _skip=True; the CLI groups all
    instances of this warning into a 'manual review required' section.
    """


# ---------------------------------------------------------------------
# Service type → rc.yml shape (rc-e5u.43.2)
# ---------------------------------------------------------------------

# The full set of Copilot service types per
# https://aws.github.io/copilot-cli/docs/concepts/services/
_KNOWN_TYPES = {
    "Backend Service",
    "Worker Service",
    "Load Balanced Web Service",
    "Request-Driven Web Service",
    "Static Site",
}


def translate_service_type(
    svc: CopilotService,
) -> tuple[dict[str, Any], list[TranslationWarning]]:
    """Map Copilot service.type → rc.yml v2 service overrides.

    Returns (overrides, warnings). overrides is a partial rc.yml
    services[<name>] dict that other translators add to.
    """
    raw = svc.raw or {}
    image = raw.get("image") or {}
    http = raw.get("http") or {}
    warnings: list[TranslationWarning] = []
    out: dict[str, Any] = {}

    if svc.type == "Backend Service":
        # Private; ALB-fronted only if user explicitly turns it on.
        # Carry port if the manifest declares one (Service Connect uses it).
        port = image.get("port")
        if port is not None:
            out["port"] = int(port)
        _maybe_health_check_path(http, out)

    elif svc.type == "Worker Service":
        out["type"] = "worker"
        # Workers never expose a port even if Copilot has one set.

    elif svc.type == "Load Balanced Web Service":
        port = image.get("port")
        if port is None:
            raise ValueError(
                f"service {svc.name!r}: Load Balanced Web Service requires "
                f"image.port (no port = nothing to send to ALB)"
            )
        out["public"] = True
        out["port"] = int(port)
        out["default_target"] = True
        _apply_http_aliases(http, out)
        _maybe_health_check_path(http, out)

    elif svc.type == "Request-Driven Web Service":
        # AWS App Runner — distinct runtime. Best-effort translation
        # to a public ECS service so the user has something to start
        # from, but flag for manual review.
        port = image.get("port")
        if port is not None:
            out["public"] = True
            out["port"] = int(port)
        warnings.append(UnsupportedServiceTypeWarning(
            service=svc.name,
            message=(
                f"Request-Driven Web Service uses AWS App Runner, not ECS. "
                f"Best-effort translated to a public ECS service for review; "
                f"adjust scaling + cold-start expectations or migrate to a "
                f"separate App Runner deployment if those matter."
            ),
        ))

    elif svc.type == "Static Site":
        # CloudFront + S3 — no ECS analogue. Skip emission entirely.
        out["_skip"] = True
        warnings.append(UnsupportedServiceTypeWarning(
            service=svc.name,
            message=(
                f"Static Site is a CloudFront + S3 stack, not ECS. Skipping "
                f"emission. Migrate by hosting the built assets on S3 + "
                f"CloudFront directly (terraform module not provided)."
            ),
        ))

    else:
        # Future / unknown Copilot type. Don't crash; flag.
        warnings.append(UnsupportedServiceTypeWarning(
            service=svc.name,
            message=(
                f"Unknown Copilot service type {svc.type!r} (known types: "
                f"{sorted(_KNOWN_TYPES)}). Manifest emitted with no "
                f"type-specific defaults; review."
            ),
        ))

    return out, warnings


def _maybe_health_check_path(http: dict[str, Any], out: dict[str, Any]) -> None:
    """http.healthcheck can be a string ('/health') OR a mapping
    ({command: [...], path: '/h', interval: 10s, ...}). For ALB
    purposes only the path matters."""
    hc = http.get("healthcheck")
    if isinstance(hc, str):
        out["health_check_path"] = hc
    elif isinstance(hc, dict) and isinstance(hc.get("path"), str):
        out["health_check_path"] = hc["path"]


@dataclass
class ScalingNotSupportedWarning(TranslationWarning):
    """Copilot count-as-mapping (range / cpu_percentage / memory_percentage)
    requests autoscaling. Provider currently uses a fixed replicas count;
    emit replicas=range_min and warn the user to revisit once we wire
    aws_appautoscaling_target into the ECS templates."""


@dataclass
class ExecDisabledIgnoredWarning(TranslationWarning):
    """Copilot 'exec: false' disables ECS Exec; our provider always
    enables it (services.tf.j2 emits enable_execute_command=true). Warn
    so the user knows."""


@dataclass
class PrivateSubnetUnsupportedWarning(TranslationWarning):
    """Copilot network.vpc.placement: private requires NAT-or-VPC-endpoint
    routing for ECR/SM/CloudWatch reachability. Provider currently runs
    Fargate in public subnets (rc-e5u.25 tracks the private + NAT variant)."""


# ---------------------------------------------------------------------
# image: build / location → compose service entry (rc-e5u.43.3)
# ---------------------------------------------------------------------

def translate_image(
    svc: CopilotService,
) -> tuple[dict[str, Any], list[TranslationWarning]]:
    """Map Copilot service.image → compose service block.

    Returns (compose_service_partial, warnings). The output is a
    partial compose 'services.<name>' dict — only the build OR image
    keys are populated here. Other fields (command, environment,
    env_file, volumes) are owned by other translators.

    image.port is intentionally NOT emitted; ports are handled by
    translate_service_type because they affect ALB wiring on rc.yml.
    """
    out: dict[str, Any] = {}
    warnings: list[TranslationWarning] = []
    image = (svc.raw or {}).get("image") or {}
    if not image:
        return out, warnings

    build = image.get("build")
    if build is not None:
        if isinstance(build, str):
            out["build"] = build
        elif isinstance(build, dict):
            block: dict[str, Any] = {}
            for key in ("context", "dockerfile", "target"):
                if key in build:
                    block[key] = build[key]
            args = build.get("args")
            if isinstance(args, dict):
                block["args"] = {str(k): str(v) for k, v in args.items()}
            if block:
                out["build"] = block
        return out, warnings

    location = image.get("location")
    if location is not None:
        out["image"] = str(location)
    return out, warnings


# ---------------------------------------------------------------------
# cpu / memory / count / exec → rc.yml service overrides (rc-e5u.43.4)
# ---------------------------------------------------------------------

def translate_resources(
    svc: CopilotService,
) -> tuple[dict[str, Any], list[TranslationWarning]]:
    """Map Copilot resource fields → rc.yml v2 services[<name>] overrides.

    Fields covered: cpu, memory, count, exec.
    """
    raw = svc.raw or {}
    out: dict[str, Any] = {}
    warnings: list[TranslationWarning] = []

    cpu = raw.get("cpu")
    if cpu is not None:
        out["cpu"] = int(cpu)
    memory = raw.get("memory")
    if memory is not None:
        out["memory"] = int(memory)

    count = raw.get("count")
    if isinstance(count, int):
        if count <= 0:
            out["replicas"] = 1
            warnings.append(ScalingNotSupportedWarning(
                service=svc.name,
                message=(
                    f"Copilot count={count} requests scale-to-zero, which "
                    f"ECS doesn't support at the service level. Emitted "
                    f"replicas=1; bring the service down explicitly via "
                    f"`rc destroy` or `aws ecs update-service "
                    f"--desired-count 0` when needed."
                ),
            ))
        else:
            out["replicas"] = count
    elif isinstance(count, dict):
        # {range: '2-10', cpu_percentage: 70} or similar autoscaling spec.
        rng = count.get("range")
        floor = 1
        if isinstance(rng, str) and "-" in rng:
            try:
                floor = int(rng.split("-", 1)[0])
            except ValueError:
                pass
        out["replicas"] = floor
        warnings.append(ScalingNotSupportedWarning(
            service=svc.name,
            message=(
                f"Copilot count {count!r} requests autoscaling. Provider "
                f"currently uses a fixed replicas value; emitted "
                f"replicas={floor} (the lower bound). Revisit once "
                f"aws_appautoscaling_target wiring lands."
            ),
        ))

    if raw.get("exec") is False:
        warnings.append(ExecDisabledIgnoredWarning(
            service=svc.name,
            message=(
                f"Copilot manifest sets exec=false (disable ECS Exec). "
                f"Provider always enables ECS Exec (rc exec / rc lifecycle "
                f"depend on it). Manual override would require editing the "
                f"emitted task def or filing a follow-up to make this opt-out."
            ),
        ))

    return out, warnings


# ---------------------------------------------------------------------
# storage.volumes → rc.yml v2 services[*].volumes (rc-e5u.43.5)
# ---------------------------------------------------------------------

def translate_storage(
    svc: CopilotService,
) -> tuple[dict[str, Any], list[TranslationWarning]]:
    """Map Copilot service.storage.volumes → rc.yml volumes list.

    Only EFS-backed volumes translate (Fargate ephemeral storage is
    handled separately via ephemeral_storage). Per-volume uid/gid pull
    from efs.uid / efs.gid when the efs key is a mapping.
    """
    raw = svc.raw or {}
    storage = raw.get("storage") or {}
    volumes = storage.get("volumes") or {}
    warnings: list[TranslationWarning] = []
    out: dict[str, Any] = {}

    rc_volumes: list[dict[str, Any]] = []
    for vol_name, vol_spec in sorted(volumes.items()):
        if not isinstance(vol_spec, dict):
            continue
        path = vol_spec.get("path")
        if not isinstance(path, str):
            # Malformed entry — skip rather than crash.
            continue
        efs = vol_spec.get("efs")
        if not efs:
            # Ephemeral local volume; nothing to declare in rc.yml on Fargate.
            continue
        entry: dict[str, Any] = {"name": vol_name, "mount": path}
        if isinstance(efs, dict):
            if "uid" in efs:
                entry["uid"] = int(efs["uid"])
            if "gid" in efs:
                entry["gid"] = int(efs["gid"])
        rc_volumes.append(entry)

    if rc_volumes:
        out["volumes"] = rc_volumes
    return out, warnings


# ---------------------------------------------------------------------
# network.vpc.placement → warning only (rc-e5u.43.5 sub-part)
# ---------------------------------------------------------------------

def translate_network(
    svc: CopilotService,
) -> tuple[dict[str, Any], list[TranslationWarning]]:
    """Surface network.vpc.placement: private as a TODO.

    Provider currently runs Fargate in public subnets (rc-e5u.25 tracks
    the private-subnet + NAT variant). Public placement is a no-op.
    """
    raw = svc.raw or {}
    placement = (raw.get("network") or {}).get("vpc", {}).get("placement")
    warnings: list[TranslationWarning] = []
    if placement == "private":
        warnings.append(PrivateSubnetUnsupportedWarning(
            service=svc.name,
            message=(
                f"Copilot network.vpc.placement=private requires NAT-gateway "
                f"or VPC-endpoint routing. Provider currently uses public "
                f"subnets for Fargate (no NAT cost) — tracked as rc-e5u.25 "
                f"to add the private + NAT variant. Test deployment will "
                f"work as public-subnet; rotate before production."
            ),
        ))
    return {}, warnings


# ---------------------------------------------------------------------
# variables + secrets → compose env + rc.yml secrets (rc-e5u.43.6)
# ---------------------------------------------------------------------

def translate_env_and_secrets(
    svc: CopilotService,
    env: str | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]], list[TranslationWarning]]:
    """Map Copilot variables: + secrets: → compose env dict + rc.yml secrets.

    Returns (compose_environment, rc_secrets, warnings).

    `compose_environment`: literal KEY=value pairs for docker-compose's
    environment: block (values stringified — compose requires strings).

    `rc_secrets`: list of rc.yml v2 secret entries with source=aws_sm
    pointing at the existing Secrets Manager / SSM ARN.

    `env` controls Copilot's `${COPILOT_ENVIRONMENT_NAME}` interpolation:
    when None the placeholder stays in the rc.yml ARN (so a downstream
    multi-env step can substitute); when set, it's replaced inline.
    """
    raw = svc.raw or {}
    compose_env: dict[str, str] = {}
    rc_secrets: list[dict[str, Any]] = []
    warnings: list[TranslationWarning] = []

    for key, val in (raw.get("variables") or {}).items():
        compose_env[str(key)] = str(val)

    for key, val in (raw.get("secrets") or {}).items():
        # Short form: `KEY: <arn-string>`.
        if isinstance(val, str):
            arn = val
        elif isinstance(val, dict):
            arn = val.get("secretsmanager") or val.get("ssm")
            if arn is None:
                # Unknown shape — skip gracefully; another translator
                # iteration could add a warning class for this.
                continue
        else:
            continue
        if env is not None:
            arn = arn.replace("${COPILOT_ENVIRONMENT_NAME}", env)
        rc_secrets.append({
            "name": str(key),
            "source": "aws_sm",
            "arn": arn,
        })

    return compose_env, rc_secrets, warnings


# ---------------------------------------------------------------------
# Per-environment overrides (rc-e5u.43.7)
# ---------------------------------------------------------------------

def apply_environment_overrides(
    svc: CopilotService, env: str | None,
) -> CopilotService:
    """Return a new CopilotService with the manifest's environments.<env>
    overrides deep-merged onto the base raw dict.

    When env is None or the named env isn't in the manifest's
    environments block, returns the service unchanged. Never mutates
    the input — translators may iterate the same base manifest for
    multiple envs.
    """
    if env is None:
        return svc
    envs_block = (svc.raw or {}).get("environments") or {}
    overrides = envs_block.get(env)
    if not overrides or not isinstance(overrides, dict):
        return svc
    merged = _deep_merge(svc.raw, overrides)
    # The collapsed manifest has no further use for the environments
    # block — drop it so downstream re-reads don't see stale overrides.
    merged.pop("environments", None)
    return CopilotService(
        name=svc.name,
        type=str(merged.get("type", svc.type)),
        manifest_path=svc.manifest_path,
        raw=merged,
        addons=list(svc.addons or []),
    )


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge: nested dicts merge per-key; everything else
    (scalars, lists) is replaced wholesale by the override side.

    Returns a fresh dict — neither input is mutated.
    """
    out: dict[str, Any] = {}
    for key in base:
        out[key] = base[key] if not isinstance(base[key], dict) else dict(base[key])
    for key, val in overrides.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(val, dict)
        ):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _apply_http_aliases(http: dict[str, Any], out: dict[str, Any]) -> None:
    """http.alias can be a single string, a list of strings, or a list
    of mapping objects {name: ..., hosted_zone: ...}. We extract names
    only; the first wins as primary domain, the rest are aliases."""
    alias = http.get("alias")
    names: list[str] = []
    if isinstance(alias, str):
        names = [alias]
    elif isinstance(alias, list):
        for entry in alias:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
    if names:
        out["domain"] = names[0]
        if len(names) > 1:
            out["aliases"] = names[1:]


# ---------------------------------------------------------------------
# Composer — wires every translator together (rc-e5u.43.9)
# ---------------------------------------------------------------------

@dataclass
class ImportResult:
    rc_yml: dict[str, Any]
    docker_compose: dict[str, Any]
    warnings: list[TranslationWarning]
    summary: str


def compose_app(
    app: CopilotApp,
    project: str | None = None,
    env: str | None = None,
) -> ImportResult:
    """Compose a CopilotApp into rc.yml v2 + docker-compose dicts.

    Pure function — never writes files. The CLI command (43.9) wraps
    this with file IO + a summary print.

    `project`: rc.yml v2 'project' field. Defaults to the parent
    directory name of the copilot/ tree, or 'myapp' if that's empty.
    `env`: when set, deep-merges environments.<env> overrides on each
    service before translation; also resolves the
    ``${COPILOT_ENVIRONMENT_NAME}`` placeholder in secret ARNs.
    """
    project_name = project or _default_project_name(app)
    rc_yml: dict[str, Any] = {
        "version": 2,
        "project": project_name,
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
        "provider_config": {"ecs": {
            "region": _guess_region(app) or "us-west-2",
            "cluster": f"{project_name}-cluster",
            "vpc_cidr": "10.0.0.0/16",
            "default_launch_type": "FARGATE",
        }},
        "terraform": {
            "output_dir": "./terraform/${provider}",
            "backend": {"type": "local"},
        },
        "services": {},
        "secrets": [],
    }
    compose: dict[str, Any] = {"services": {}}
    warnings: list[TranslationWarning] = []
    excluded: list[str] = []

    seen_secret_names: set[str] = set()
    for svc in app.services:
        merged = apply_environment_overrides(svc, env)

        type_overrides, w = translate_service_type(merged)
        warnings.extend(w)
        if type_overrides.get("_skip"):
            excluded.append(merged.name)
            continue

        image_block, w = translate_image(merged)
        warnings.extend(w)
        resource_overrides, w = translate_resources(merged)
        warnings.extend(w)
        storage_overrides, w = translate_storage(merged)
        warnings.extend(w)
        _net_out, w = translate_network(merged)
        warnings.extend(w)
        compose_env, svc_secrets, w = translate_env_and_secrets(merged, env=env)
        warnings.extend(w)

        # Build the rc.yml services entry — overrides layer in order:
        # type → resources → storage → image-side-effects (none today).
        rc_svc: dict[str, Any] = {}
        for layer in (type_overrides, resource_overrides, storage_overrides):
            for k, v in layer.items():
                if k.startswith("_"):
                    continue
                rc_svc[k] = v
        rc_yml["services"][merged.name] = rc_svc

        # Compose entry: image/build + environment.
        compose_svc: dict[str, Any] = dict(image_block)
        if compose_env:
            compose_svc["environment"] = compose_env
        compose["services"][merged.name] = compose_svc

        # Top-level secrets list dedupes by name.
        for s in svc_secrets:
            if s["name"] in seen_secret_names:
                continue
            seen_secret_names.add(s["name"])
            rc_yml["secrets"].append(s)

    if excluded:
        rc_yml["compose"] = {"exclude": excluded}

    summary = _build_summary(app, rc_yml, excluded, warnings, env)
    return ImportResult(
        rc_yml=rc_yml,
        docker_compose=compose,
        warnings=warnings,
        summary=summary,
    )


def _default_project_name(app: CopilotApp) -> str:
    # copilot/ usually lives under <repo>/copilot, so the parent dir
    # name is the natural project label. Fall back to 'myapp' for
    # repo-root copilot/ trees or odd shapes.
    parent = app.root.parent
    name = parent.name if parent and parent.name else ""
    if not name or name in {"/", ".", ""}:
        return "myapp"
    return name


def _guess_region(app: CopilotApp) -> str | None:
    """Best-effort: extract a region from any ACM cert ARN found in
    environment manifests. Returns None when nothing matches."""
    import re
    arn_re = re.compile(r"arn:aws:acm:([a-z0-9-]+):")
    for env in app.environments:
        certs = (env.raw.get("http", {}) or {}).get("public", {}).get("certificates") or []
        for c in certs:
            m = arn_re.match(str(c))
            if m:
                return m.group(1)
    return None


# rc-e5u.43.8: per-AWS-resource-type guidance for addons we don't translate.
# When a copilot addon CFN template declares one of these resource types, the
# import summary points the user at the right replacement path. Anything not
# in this map gets a generic "manual translation required" line, which is
# still better than silently dropping the addon.
_ADDON_RESOURCE_GUIDANCE: dict[str, str] = {
    "AWS::RDS::DBInstance": (
        "set up RDS yourself (or `rc up` with a separate Postgres/MySQL "
        "ECS service) and point DATABASE_URL at it"
    ),
    "AWS::RDS::DBCluster": (
        "set up RDS yourself; point DATABASE_URL / cluster endpoint env "
        "vars at the new cluster"
    ),
    "AWS::S3::Bucket": (
        "either use rc.yml `backup.bucket` for backup staging or add the "
        "bucket to your own terraform module + set BUCKET_NAME env var"
    ),
    "AWS::DynamoDB::Table": (
        "create the table via your own terraform module; pass the name "
        "via env var (Copilot Addon naming convention is "
        "<addon>_NAME / <addon>_ARN)"
    ),
    "AWS::ElastiCache::CacheCluster": (
        "either deploy redis as an rc.yml infrastructure service "
        "(persistence via EFS) or use ElastiCache via your own terraform"
    ),
    "AWS::ElastiCache::ReplicationGroup": (
        "either deploy redis/valkey as an rc.yml infrastructure service "
        "or wire ElastiCache via your own terraform module"
    ),
    "AWS::SQS::Queue": (
        "create the queue via your own terraform; pass QUEUE_URL via env "
        "var (or use celery/sidekiq with redis from rc.yml directly)"
    ),
    "AWS::SNS::Topic": (
        "create the topic via your own terraform; pass TOPIC_ARN via env"
    ),
    "AWS::SecretsManager::Secret": (
        "use rc.yml top-level `secrets:` with `source: file` for the value "
        "OR `source: arn` to reference an externally-managed secret"
    ),
    "AWS::IAM::Role": (
        "the ECS task role is provider-managed; if the addon role exists "
        "for cross-service access, replicate the policy in your own "
        "terraform module"
    ),
}


def _addon_resource_types(addon: "CopilotAddon") -> list[str]:
    """Extract Type: <X> values from a CFN-template-shaped addon."""
    raw = addon.raw or {}
    resources = raw.get("Resources") if isinstance(raw, dict) else None
    if not isinstance(resources, dict):
        return []
    out: list[str] = []
    for r in resources.values():
        if not isinstance(r, dict):
            continue
        t = r.get("Type")
        if isinstance(t, str):
            out.append(t)
    return out


def _build_summary(
    app: CopilotApp,
    rc_yml: dict[str, Any],
    excluded: list[str],
    warnings: list[TranslationWarning],
    env: str | None,
) -> str:
    """Human-readable wrap of what got translated and what needs review."""
    lines: list[str] = []
    lines.append(f"# Copilot import summary — {rc_yml['project']}")
    lines.append("")
    lines.append(f"Source: {app.root}")
    lines.append(f"Environment selected: {env or '(none — base manifest values)'}")
    lines.append(f"Services translated: {len(rc_yml['services'])}")
    if rc_yml["services"]:
        for name in sorted(rc_yml["services"]):
            lines.append(f"  - {name}")
    if excluded:
        lines.append("")
        lines.append(f"Services skipped: {len(excluded)} (Static Site or similar)")
        for name in excluded:
            lines.append(f"  - {name}")
    if warnings:
        lines.append("")
        lines.append(f"Warnings: {len(warnings)} — manual review recommended")
        # Group by warning class for scannability.
        by_kind: dict[str, list[TranslationWarning]] = {}
        for w in warnings:
            by_kind.setdefault(w.__class__.__name__, []).append(w)
        for kind, group in sorted(by_kind.items()):
            lines.append("")
            lines.append(f"  {kind}: {len(group)}")
            for w in group:
                lines.append(f"    [{w.service}] {w.message}")

    # rc-e5u.43.8: surface addon CFN templates we did NOT translate, with
    # per-resource-type guidance so the user knows what to do next.
    addon_count = sum(len(svc.addons or []) for svc in app.services)
    if addon_count:
        lines.append("")
        lines.append(
            f"Addon templates detected: {addon_count} — manual translation required"
        )
        # Aggregate by AWS resource type for the guidance section.
        by_type: dict[str, list[str]] = {}  # type -> [service/addon]
        unknown_types: list[tuple[str, str]] = []  # (svc, addon) pairs without recognized types
        for svc in app.services:
            for addon in (svc.addons or []):
                rtypes = _addon_resource_types(addon)
                if not rtypes:
                    unknown_types.append((svc.name, addon.name))
                    continue
                for rt in rtypes:
                    by_type.setdefault(rt, []).append(f"{svc.name}/{addon.name}.yml")
        for rt in sorted(by_type):
            sources = by_type[rt]
            lines.append("")
            lines.append(f"  {rt} ({len(sources)} resource(s) across "
                         f"{len({s.split('/')[0] for s in sources})} service(s))")
            for s in sorted(set(sources)):
                lines.append(f"    in: {s}")
            guidance = _ADDON_RESOURCE_GUIDANCE.get(rt)
            if guidance:
                lines.append(f"    next: {guidance}")
            else:
                lines.append(
                    f"    next: not yet auto-handled — replicate via your own "
                    f"terraform module or open an issue"
                )
        if unknown_types:
            lines.append("")
            lines.append(
                f"  Addon templates with no parseable Resources block: "
                f"{len(unknown_types)}"
            )
            for svc, addon in sorted(unknown_types):
                lines.append(f"    - {svc}/{addon}.yml")

    if app.pipelines:
        lines.append("")
        lines.append(
            f"Pipelines detected ({len(app.pipelines)}) — not translated. "
            f"Set up CI/CD separately (rc deploy is what your pipeline should call)."
        )
    return "\n".join(lines) + "\n"
