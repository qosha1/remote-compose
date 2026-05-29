"""Per-resource translators: v1 stack + inventory → v2 rc.yml + terraform imports."""

from __future__ import annotations

from dataclasses import dataclass

from .discover import ResourceInventory, V1Stack

# ---------------------------------------------------------------------
# Warnings + import-block dataclass
# ---------------------------------------------------------------------


@dataclass
class TranslationWarning:
    service: str = ""
    message: str = ""
    severity: str = "info"


@dataclass
class UnsupportedV1FeatureWarning(TranslationWarning):
    pass


@dataclass
class ManualReviewRequiredWarning(TranslationWarning):
    pass


@dataclass
class StatefulRecreateBlocked(TranslationWarning):
    severity: str = "blocker"


@dataclass
class TerraformImportBlock:
    id: str
    to: str

    def render_hcl(self) -> str:
        return "import {\n" f'  id = "{self.id}"\n' f"  to = {self.to}\n" "}\n"


# ---------------------------------------------------------------------
# v1 → v2 schema
# ---------------------------------------------------------------------


def translate_v1_to_v2_schema(
    stack: V1Stack,
) -> tuple[dict, list[TranslationWarning]]:
    warnings: list[TranslationWarning] = []
    rc_yml: dict = {
        "version": 2,
        "project": stack.project_name,
        "provider": "ecs",
        "provider_config": {
            "ecs": {
                "region": stack.region,
                "cluster": stack.cluster,
                "aws_profile": stack.aws_profile,
                "vpc_cidr": stack.vpc_cidr,
                "default_launch_type": "FARGATE",
            },
        },
        "terraform": {
            "output_dir": "./terraform/${provider}",
            "backend": {"type": "local"},
        },
        "services": {},
    }

    for name, svc in stack.services.items():
        s: dict = {
            "cpu": svc.cpu,
            "memory": svc.memory,
            "type": svc.type,
        }
        if svc.health_check_path:
            s["health_check_path"] = svc.health_check_path
        if svc.ephemeral_storage:
            s["ephemeral_storage"] = svc.ephemeral_storage
        if svc.public:
            s["public"] = True
            s["port"] = svc.port or 80
            if svc.default_target:
                s["default_target"] = True
            if stack.domain:
                s["domain"] = stack.domain
        rc_yml["services"][name] = s

    if stack.compose_file:
        warnings.append(
            TranslationWarning(
                service="",
                message=(
                    f"v1 compose_file was {stack.compose_file!r}; v2 typically uses "
                    "docker-compose.local.yml. Reconcile manually."
                ),
                severity="info",
            )
        )

    return rc_yml, warnings


# ---------------------------------------------------------------------
# EFS — IMPORT
# ---------------------------------------------------------------------


def translate_efs_in_place(
    inv: ResourceInventory,
) -> tuple[dict, list[TerraformImportBlock], list[TranslationWarning]]:
    if inv.efs is None or not inv.efs.file_system_id:
        raise ValueError("EFS file system not present in inventory")
    imports: list[TerraformImportBlock] = []
    imports.append(
        TerraformImportBlock(
            id=inv.efs.file_system_id,
            to="module.efs.aws_efs_file_system.this",
        )
    )
    for ap in inv.efs.access_points:
        imports.append(
            TerraformImportBlock(
                id=ap.ap_id,
                to=f'module.efs.aws_efs_access_point.this["{ap.name}"]',
            )
        )
    overrides: dict = {
        "file_system_id": inv.efs.file_system_id,
        "size_bytes": inv.efs.size_bytes,
    }
    return overrides, imports, []


# ---------------------------------------------------------------------
# ALB — IMPORT
# ---------------------------------------------------------------------


def translate_alb_in_place(
    inv: ResourceInventory,
) -> tuple[dict, list[TerraformImportBlock], list[TranslationWarning]]:
    if inv.alb is None or not inv.alb.arn:
        raise ValueError("ALB not present in inventory")
    imports: list[TerraformImportBlock] = [
        TerraformImportBlock(
            id=inv.alb.arn,
            to="module.alb.aws_lb.this",
        ),
    ]
    for lst in inv.alb.listeners:
        imports.append(
            TerraformImportBlock(
                id=lst.arn,
                to=f'module.alb.aws_lb_listener.this["{lst.port}"]',
            )
        )
    for tg in inv.alb.target_groups:
        imports.append(
            TerraformImportBlock(
                id=tg.arn,
                to=f'module.alb.aws_lb_target_group.this["{tg.name}"]',
            )
        )
    overrides = {
        "alb_arn": inv.alb.arn,
        "dns_name": inv.alb.dns_name,
    }
    return overrides, imports, []


# ---------------------------------------------------------------------
# ACM — IMPORT
# ---------------------------------------------------------------------


def translate_acm_in_place(
    inv: ResourceInventory,
) -> tuple[dict, list[TerraformImportBlock], list[TranslationWarning]]:
    if inv.acm_cert is None or not inv.acm_cert.arn:
        raise ValueError("ACM certificate not present in inventory")
    imports = [
        TerraformImportBlock(
            id=inv.acm_cert.arn,
            to="module.acm.aws_acm_certificate.this",
        ),
    ]
    overrides = {
        "certificate_arn": inv.acm_cert.arn,
        "domain_name": inv.acm_cert.domain_name,
    }
    return overrides, imports, []


# ---------------------------------------------------------------------
# Secrets — REFERENCE BY ARN
# ---------------------------------------------------------------------


def translate_secrets_keep_arn(
    inv: ResourceInventory,
) -> tuple[list[dict], list[TranslationWarning]]:
    if not inv.secrets:
        raise ValueError(
            "no SM secrets in inventory; v1 stack uses 32 secrets — "
            "discovery must have failed to enumerate them"
        )
    rc_secrets: list[dict] = []
    for s in inv.secrets:
        # short name = trailing path segment
        short = s.name.rsplit("/", 1)[-1]
        rc_secrets.append(
            {
                "name": short,
                "source": "arn",
                "arn": s.arn,
            }
        )

    warnings: list[TranslationWarning] = []
    if inv.secrets_truncated:
        warnings.append(
            TranslationWarning(
                service="",
                message=(
                    "inventory snapshot truncated the secrets list; "
                    "real run against AWS will enumerate all 32"
                ),
                severity="warning",
            )
        )
    return rc_secrets, warnings


# ---------------------------------------------------------------------
# VPC — IMPORT
# ---------------------------------------------------------------------


def translate_vpc_in_place(
    inv: ResourceInventory,
) -> tuple[dict, list[TerraformImportBlock], list[TranslationWarning]]:
    if inv.vpc is None or not inv.vpc.id:
        raise ValueError("VPC not present in inventory")
    imports = [
        TerraformImportBlock(id=inv.vpc.id, to="module.vpc.aws_vpc.this"),
    ]
    for subnet_id in inv.vpc.subnets:
        imports.append(
            TerraformImportBlock(
                id=subnet_id,
                to=f'module.vpc.aws_subnet.this["{subnet_id}"]',
            )
        )
    for sg_id in inv.vpc.security_groups:
        imports.append(
            TerraformImportBlock(
                id=sg_id,
                to=f'module.vpc.aws_security_group.this["{sg_id}"]',
            )
        )
    overrides = {
        "vpc_id": inv.vpc.id,
        "cidr_block": inv.vpc.cidr_block,
    }
    return overrides, imports, []


# ---------------------------------------------------------------------
# IAM — REFERENCE BY ARN (no imports)
# ---------------------------------------------------------------------


def translate_iam_keep_external(
    inv: ResourceInventory,
) -> tuple[dict, list[TranslationWarning]]:
    if inv.iam is None:
        raise ValueError("IAM config not present in inventory")
    return (
        {
            "task_execution_role_arn": inv.iam.task_execution_role_arn,
            "task_role_arn": inv.iam.task_role_arn,
        },
        [],
    )


# ---------------------------------------------------------------------
# ECR — REUSE (no imports)
# ---------------------------------------------------------------------


def translate_ecr_reuse(
    inv: ResourceInventory,
) -> tuple[dict, list[TranslationWarning]]:
    return (
        {
            "ecr_repositories": {r.name: r.uri for r in inv.ecr_repositories},
        },
        [],
    )


# ---------------------------------------------------------------------
# ECS cluster — IMPORT
# ---------------------------------------------------------------------


def translate_ecs_cluster_in_place(
    inv: ResourceInventory,
) -> tuple[dict, list[TerraformImportBlock], list[TranslationWarning]]:
    if inv.ecs_cluster is None or not inv.ecs_cluster.arn:
        raise ValueError("ECS cluster not present in inventory")
    imports = [
        TerraformImportBlock(
            id=inv.ecs_cluster.arn,
            to="module.ecs.aws_ecs_cluster.this",
        ),
    ]
    warnings: list[TranslationWarning] = []
    if inv.ecs_cluster.running_tasks_count > 0:
        warnings.append(
            TranslationWarning(
                service="",
                message=(
                    f"{inv.ecs_cluster.running_tasks_count} running tasks will "
                    "drain during cutover (rolling task-def update); factor "
                    "into the maintenance window budget"
                ),
                severity="warning",
            )
        )
    overrides = {
        "cluster_name": inv.ecs_cluster.name,
        "cluster_arn": inv.ecs_cluster.arn,
    }
    return overrides, imports, warnings
