"""rc.yml v2 — raw dict → validated RcConfigV2 parser.

Cross-instance validation lives here (e.g. duplicate-hostname detection
across services). Per-instance validation lives on the dataclasses
themselves in _schema_types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ._iam_types import IamRoleV2, IamStatementV2, validate_iam_role_refs
from ._network_types import (
    NetworkRuleV2,
    NetworkV2,
    RepositoryV2,
    SecurityGroupV2,
    SubnetGroupV2,
    VpcEndpointV2,
    validate_network_refs,
)
from ._schema_types import (
    BackupConfig,
    BootstrapConfig,
    ComposeConfig,
    ConfigError,
    GithubOidcDeployRole,
    HealthCheckV2,
    LifecycleHookV2,
    RcConfigV2,
    SecretRefV2,
    ServiceV2,
    TerraformBackend,
    TerraformConfig,
    TlsConfig,
)


def _parse_backend(raw: dict[str, Any]) -> TerraformBackend:
    known = {"type", "bucket", "key", "region", "dynamodb_table"}
    extra = {k: v for k, v in raw.items() if k not in known}
    return TerraformBackend(
        type=raw.get("type", "local"),
        bucket=raw.get("bucket"),
        key=raw.get("key"),
        region=raw.get("region"),
        dynamodb_table=raw.get("dynamodb_table"),
        extra=extra,
    )


def _parse_terraform(raw: dict[str, Any]) -> TerraformConfig:
    return TerraformConfig(
        output_dir=raw.get("output_dir", "./terraform/${provider}"),
        backend=_parse_backend(raw.get("backend", {})),
    )


def _parse_lifecycle(svc_name: str, raw: dict[str, Any]) -> dict[str, LifecycleHookV2]:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"service {svc_name!r}: lifecycle must be a mapping of "
            f"hook-name → spec, got {type(raw).__name__}"
        )
    out: dict[str, LifecycleHookV2] = {}
    for hook_name, hook_raw in raw.items():
        if not isinstance(hook_raw, dict):
            raise ConfigError(
                f"service {svc_name!r}: lifecycle.{hook_name} must be a "
                f"mapping, got {type(hook_raw).__name__}"
            )
        cmd_raw = hook_raw.get("command")
        if cmd_raw is not None and not isinstance(cmd_raw, list):
            raise ConfigError(
                f"lifecycle hook {hook_name!r}: command must be a non-empty list, "
                f"got {type(cmd_raw).__name__}"
            )
        probe_raw = hook_raw.get("probe")
        if probe_raw is not None and not isinstance(probe_raw, list):
            raise ConfigError(
                f"lifecycle hook {hook_name!r}: probe must be a non-empty list[str], "
                f"got {type(probe_raw).__name__}"
            )
        hook = LifecycleHookV2(
            name=hook_name,
            command=list(cmd_raw or []),
            auto_on_deploy=bool(hook_raw.get("auto_on_deploy", False)),
            run_once=bool(hook_raw.get("run_once", False)),
            interactive=bool(hook_raw.get("interactive", False)),
            probe=list(probe_raw) if probe_raw else None,
            mode=str(hook_raw.get("mode", "exec")),
        )
        hook.validate()
        out[hook_name] = hook
    return out


def _parse_health_check(raw: Any) -> "HealthCheckV2 | None":
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("service.health_check must be a mapping")
    hc = HealthCheckV2(
        command=raw.get("command"),
        interval=int(raw.get("interval", 30)),
        timeout=int(raw.get("timeout", 10)),
        retries=int(raw.get("retries", 3)),
        start_period=int(raw.get("start_period", 0)),
    )
    hc.validate()
    return hc


def _parse_service(name: str, raw: dict[str, Any]) -> ServiceV2:
    try:
        return ServiceV2(
            name=name,
            # cpu / memory default to 256 / 512 when omitted so partial
            # overrides (rc.yml entry that only sets type or public)
            # match the auto-import path's defaults — see
            # cli_v2.build_deploy_context.
            cpu=int(raw.get("cpu", 256)),
            memory=int(raw.get("memory", 512)),
            replicas=int(raw.get("replicas", 1)),
            type=raw.get("type", "application"),
            launch_type=raw.get("launch_type"),
            health_check_path=raw.get("health_check_path"),
            health_check=_parse_health_check(raw.get("health_check")),
            health_check_grace_period=raw.get("health_check_grace_period"),
            public=bool(raw.get("public", False)),
            port=raw.get("port"),
            ephemeral_storage=raw.get("ephemeral_storage"),
            default_target=bool(raw.get("default_target", False)),
            volumes=list(raw.get("volumes", [])),
            lifecycle=(
                _parse_lifecycle(name, raw["lifecycle"]) if raw.get("lifecycle") else {}
            ),
            domain=raw.get("domain"),
            # Preserve raw shape so validate() can flag non-list values.
            aliases=raw["aliases"] if "aliases" in raw else [],
            # rc-e5u.46.1: optional Dockerfile override.
            dockerfile=raw.get("dockerfile"),
            # rc-2r1r: pre-built image, for services compose doesn't define
            # (and as an override for those it does).
            image=raw.get("image"),
            # Same — preserve raw shape for validate() to inspect.
            dev_volumes=raw["dev_volumes"] if "dev_volumes" in raw else [],
            # rc-e5u.46.4: extra env vars merged into the task def alongside
            # compose's ``environment:``. Coerce scalars to str so YAML
            # booleans (DJANGO_DEBUG: False) and ints become valid env
            # values. Non-scalar values are caught by ServiceV2.validate.
            env=(
                {
                    str(k): (str(v) if not isinstance(v, (dict, list)) else v)
                    for k, v in raw["env"].items()
                }
                if isinstance(raw.get("env"), dict)
                else (raw["env"] if "env" in raw else {})
            ),
            framework=raw.get("framework"),
            env_from_secret=(
                list(raw["env_from_secret"]) if "env_from_secret" in raw else []
            ),
            auto_roll=raw["auto_roll"] if "auto_roll" in raw else True,
            stateful=raw["stateful"] if "stateful" in raw else False,
            # rc-6akx: raw passthrough. The provider validates it (the rules
            # — percent ranges, the roll-deadlock check, the stateful
            # rejection — are all ECS semantics), so keep the shape intact
            # rather than coercing here and losing the user's typo.
            deployment=raw["deployment"] if "deployment" in raw else None,
            # Declared-network placement. Preserve raw shape so validate()
            # can flag a non-list / non-string; None means "rc defaults".
            security_groups=(
                raw["security_groups"] if "security_groups" in raw else None
            ),
            subnets=raw["subnets"] if "subnets" in raw else None,
            # Declared task role. Same treatment: keep the raw shape so
            # validate() can flag a non-string; None means the shared role.
            iam_role=raw["iam_role"] if "iam_role" in raw else None,
        )
    except KeyError as e:
        raise ConfigError(f"service {name!r}: missing required field {e.args[0]!r}")


def _parse_secret(raw: dict[str, Any]) -> SecretRefV2:
    if "name" not in raw or "source" not in raw:
        raise ConfigError(f"secret entry missing name or source: {raw!r}")
    return SecretRefV2(
        name=raw["name"],
        source=raw["source"],
        path=raw.get("path"),
        arn=raw.get("arn"),
        ref=raw.get("ref"),
    )


def _require_mapping(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(raw).__name__}")
    return raw


def _reject_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {sorted(unknown)} "
            f"(supported: {sorted(allowed)})"
        )


def _parse_security_group(name: str, raw: Any) -> SecurityGroupV2:
    where = f"network.security_groups.{name}"
    raw = _require_mapping(raw or {}, where)
    _reject_unknown(raw, {"description", "ingress", "egress"}, where)
    for key in ("ingress", "egress"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], list):
            raise ConfigError(
                f"{where}.{key} must be a list of rules, got "
                f"{type(raw[key]).__name__}"
            )
    return SecurityGroupV2(
        name=name,
        description=raw.get("description"),
        ingress=[
            NetworkRuleV2.parse(r, direction="ingress", where=f"{where}.ingress[{i}]")
            for i, r in enumerate(raw.get("ingress") or [])
        ],
        egress=[
            NetworkRuleV2.parse(r, direction="egress", where=f"{where}.egress[{i}]")
            for i, r in enumerate(raw.get("egress") or [])
        ],
    )


def _parse_subnet_group(name: str, raw: Any) -> SubnetGroupV2:
    where = f"network.subnets.{name}"
    raw = _require_mapping(raw or {}, where)
    _reject_unknown(raw, {"public", "egress", "count", "cidrs", "cidr_offset"}, where)
    public = bool(raw.get("public", False))
    # A public group's egress mode is not a choice — default it rather than
    # forcing every public declaration to spell out 'egress: igw'.
    default_egress = "igw" if public else "none"
    return SubnetGroupV2(
        name=name,
        public=public,
        egress=str(raw.get("egress", default_egress)),
        count=raw["count"] if "count" in raw else 2,
        cidrs=list(raw.get("cidrs") or []),
        cidr_offset=raw.get("cidr_offset"),
    )


def _parse_vpc_endpoint(name: str, raw: Any) -> VpcEndpointV2:
    where = f"network.endpoints.{name}"
    raw = _require_mapping(raw or {}, where)
    _reject_unknown(raw, {"services", "type", "subnets", "private_dns"}, where)
    services = raw.get("services")
    if services is None:
        services = []
    elif isinstance(services, str):
        services = [services]
    elif not isinstance(services, list):
        raise ConfigError(
            f"{where}.services must be a list, got {type(services).__name__}"
        )
    subnets = raw.get("subnets")
    if subnets is None:
        subnets = []
    elif isinstance(subnets, str):
        subnets = [subnets]
    elif not isinstance(subnets, list):
        raise ConfigError(
            f"{where}.subnets must be a list of network.subnets names, got "
            f"{type(subnets).__name__}"
        )
    return VpcEndpointV2(
        name=name,
        services=[str(s) for s in services],
        type=raw.get("type"),
        subnets=[str(s) for s in subnets],
        private_dns=bool(raw.get("private_dns", True)),
    )


def _parse_network(raw: Any) -> NetworkV2:
    raw = _require_mapping(raw or {}, "network")
    _reject_unknown(raw, {"security_groups", "subnets", "endpoints"}, "network")
    sgs_raw = _require_mapping(
        raw.get("security_groups") or {}, "network.security_groups"
    )
    subnets_raw = _require_mapping(raw.get("subnets") or {}, "network.subnets")
    endpoints_raw = _require_mapping(raw.get("endpoints") or {}, "network.endpoints")
    return NetworkV2(
        security_groups={n: _parse_security_group(n, r) for n, r in sgs_raw.items()},
        subnets={n: _parse_subnet_group(n, r) for n, r in subnets_raw.items()},
        endpoints={n: _parse_vpc_endpoint(n, r) for n, r in endpoints_raw.items()},
    )


def _parse_repositories(raw: Any) -> dict[str, RepositoryV2]:
    raw = _require_mapping(raw or {}, "repositories")
    repos: dict[str, RepositoryV2] = {}
    for name, body in raw.items():
        where = f"repositories.{name}"
        body = _require_mapping(body or {}, where)
        _reject_unknown(
            body,
            {
                "mirror",
                "mutable",
                "scan_on_push",
                "expire_untagged_days",
                "force_delete",
            },
            where,
        )
        repos[name] = RepositoryV2(
            name=name,
            mirror=body.get("mirror"),
            mutable=bool(body.get("mutable", True)),
            scan_on_push=bool(body.get("scan_on_push", True)),
            expire_untagged_days=body.get("expire_untagged_days"),
            force_delete=bool(body.get("force_delete", False)),
        )
    return repos


def _parse_iam_roles(raw: Any) -> dict[str, IamRoleV2]:
    raw = _require_mapping(raw or {}, "iam_roles")
    roles: dict[str, IamRoleV2] = {}
    for name, body in raw.items():
        where = f"iam_roles.{name}"
        body = _require_mapping(body or {}, where)
        _reject_unknown(
            body,
            {"description", "managed_policies", "statements", "tags"},
            where,
        )
        managed = body.get("managed_policies")
        if isinstance(managed, str):
            managed = [managed]
        elif managed is None:
            managed = []
        elif not isinstance(managed, list):
            raise ConfigError(
                f"{where}.managed_policies must be a list of IAM policy ARNs, "
                f"got {type(managed).__name__}"
            )
        statements_raw = body.get("statements")
        if statements_raw is None:
            statements_raw = []
        elif not isinstance(statements_raw, list):
            raise ConfigError(
                f"{where}.statements must be a list, got "
                f"{type(statements_raw).__name__}"
            )
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, dict):
            raise ConfigError(
                f"{where}.tags must be a mapping, got {type(tags).__name__}"
            )
        roles[name] = IamRoleV2(
            name=name,
            description=body.get("description"),
            managed_policies=[str(p) for p in managed],
            statements=[
                IamStatementV2.parse(s, where=f"{where}.statements[{i}]")
                for i, s in enumerate(statements_raw)
            ],
            # Coerce scalars to str the same way service env does: a YAML
            # `team: 42` is a perfectly reasonable tag value and AWS wants a
            # string. Non-scalars are left alone for validate() to reject.
            tags={
                str(k): (v if isinstance(v, (dict, list)) else str(v))
                for k, v in (tags or {}).items()
            },
        )
    return roles


def _parse_bootstrap(raw: dict[str, Any]) -> BootstrapConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"bootstrap must be a mapping, got {type(raw).__name__}")
    unknown = set(raw.keys()) - {"github_oidc_deploy_role", "output_dir"}
    if unknown:
        raise ConfigError(
            f"unknown bootstrap keys: {sorted(unknown)} "
            f"(supported: github_oidc_deploy_role, output_dir)"
        )
    role = None
    role_raw = raw.get("github_oidc_deploy_role")
    if role_raw is not None:
        if not isinstance(role_raw, dict):
            raise ConfigError(
                "bootstrap.github_oidc_deploy_role must be a mapping, got "
                f"{type(role_raw).__name__}"
            )
        unknown_role = set(role_raw.keys()) - {
            "github_repo",
            "github_branch",
            "role_name",
            "create_oidc_provider",
            "permissions",
        }
        if unknown_role:
            raise ConfigError(
                f"unknown bootstrap.github_oidc_deploy_role keys: "
                f"{sorted(unknown_role)}"
            )
        role = GithubOidcDeployRole(
            github_repo=role_raw.get("github_repo", ""),
            github_branch=role_raw.get("github_branch", "main"),
            role_name=role_raw.get("role_name"),
            create_oidc_provider=bool(role_raw.get("create_oidc_provider", False)),
            permissions=role_raw.get("permissions", {}) or {},
        )
    return BootstrapConfig(
        github_oidc_deploy_role=role,
        output_dir=raw.get("output_dir", "bootstrap/terraform"),
    )


def parse(raw: dict[str, Any]) -> RcConfigV2:
    """Parse a rc.yml v2 dict into a validated RcConfigV2."""
    if not isinstance(raw, dict):
        raise ConfigError(f"rc.yml v2 must be a mapping, got {type(raw).__name__}")

    services_raw = raw.get("services", {}) or {}
    services = {n: _parse_service(n, s) for n, s in services_raw.items()}

    # Per-service validation before cross-service checks so we surface the
    # most specific error first (e.g. "aliases must be a list" beats
    # "duplicate hostname" when aliases is mistakenly a string).
    for svc in services.values():
        svc.validate()

    # Cross-service uniqueness: two services can't claim the same hostname,
    # whether as a primary domain or as an alias of either.
    seen_hostnames: dict[str, str] = {}
    for svc in services.values():
        candidates = [svc.domain] if svc.domain else []
        candidates.extend(svc.aliases or [])
        for host in candidates:
            existing = seen_hostnames.get(host)
            if existing:
                raise ConfigError(
                    f"duplicate hostname {host!r}: claimed by both "
                    f"service {existing!r} and {svc.name!r}"
                )
            seen_hostnames[host] = svc.name

    secrets_raw = raw.get("secrets", []) or []
    secrets = [_parse_secret(s) for s in secrets_raw]

    backup = None
    if raw.get("backup"):
        backup = BackupConfig(
            bucket=raw["backup"].get("bucket"),
            service=raw["backup"].get("service"),
            bucket_managed=bool(raw["backup"].get("bucket_managed", True)),
            retention_days=(
                None
                if raw["backup"].get("retention_days") in (None, "never", 0)
                else int(raw["backup"]["retention_days"])
            ),
        )

    tls = None
    if raw.get("tls"):
        tls = TlsConfig(
            mode=raw["tls"].get("mode", "acm"),
            certificate_arn=raw["tls"].get("certificate_arn"),
        )

    compose_cfg = None
    if raw.get("compose"):
        cb = raw["compose"]
        if not isinstance(cb, dict):
            raise ConfigError(f"compose must be a mapping, got {type(cb).__name__}")
        unknown = set(cb.keys()) - {"include", "exclude"}
        if unknown:
            raise ConfigError(
                f"unknown compose keys: {sorted(unknown)} "
                f"(supported: include, exclude)"
            )
        compose_cfg = ComposeConfig(
            include=cb.get("include"),
            exclude=cb.get("exclude"),
        )

    bootstrap = None
    if raw.get("bootstrap"):
        bootstrap = _parse_bootstrap(raw["bootstrap"])

    network = _parse_network(raw.get("network"))
    repositories = _parse_repositories(raw.get("repositories"))
    iam_roles = _parse_iam_roles(raw.get("iam_roles"))
    network.validate()
    for repo in repositories.values():
        repo.validate()
    for role in iam_roles.values():
        role.validate()

    # Unlike the network refs below, this needs no second pass in the
    # provider: `iam_role:` can only be written on an rc.yml service, so the
    # rc.yml-only service set is already the complete set of referrers.
    validate_iam_role_refs(
        iam_roles,
        service_roles={n: s.iam_role for n, s in services.items()},
    )

    # Cross-resource reference resolution. service_names / has_alb are left
    # unknown here: a service referenced by 'service:<name>' may live only in
    # docker-compose.yml and never appear in rc.yml, so rejecting on the
    # rc.yml-only set would fail valid configs. The ECS provider re-runs this
    # with the merged service set once compose has been read.
    validate_network_refs(
        network,
        service_names=None,
        has_alb=None,
        service_sg_overrides={
            n: list(s.security_groups) for n, s in services.items() if s.security_groups
        },
        service_subnet_placements={
            n: s.subnets for n, s in services.items() if s.subnets
        },
        public_services={n: s.port for n, s in services.items() if s.public},
    )

    cfg = RcConfigV2(
        version=int(raw.get("version", 0)),
        project=raw.get("project", ""),
        compose_file=raw.get("compose_file", ""),
        provider=raw.get("provider", ""),
        provider_config=raw.get("provider_config", {}) or {},
        terraform=_parse_terraform(raw.get("terraform", {}) or {}),
        services=services,
        secrets=secrets,
        backup=backup,
        domain=raw.get("domain"),
        tls=tls,
        compose=compose_cfg,
        bootstrap=bootstrap,
        network=network,
        repositories=repositories,
        iam_roles=iam_roles,
    )
    cfg.validate()
    return cfg


def load(path: str | Path) -> RcConfigV2:
    """Load an rc.yml v2 file from disk and return a validated RcConfigV2."""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return parse(raw)
