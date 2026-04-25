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

from .discover import CopilotService


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
