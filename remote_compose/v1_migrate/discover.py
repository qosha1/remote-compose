"""Discover the live v1 stack: parse rc v1 yaml + snapshot AWS state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class DiscoveryError(Exception):
    """Raised when discover() can't read or parse its inputs."""


# ---------------------------------------------------------------------
# V1 rc.yml shape
# ---------------------------------------------------------------------

@dataclass
class V1Service:
    name: str
    cpu: int = 0
    memory: int = 0
    type: str = ""
    public: bool = False
    port: int | None = None
    health_check_path: str | None = None
    default_target: bool = False
    ephemeral_storage: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class V1Stack:
    cluster: str = ""
    region: str = ""
    aws_profile: str = ""
    project_name: str = ""
    compose_file: str = ""
    domain: str = ""
    vpc_cidr: str = ""
    services: dict[str, V1Service] = field(default_factory=dict)
    secrets_files: list[str] = field(default_factory=list)
    backup: dict | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "V1Stack":
        path = Path(path)
        if not path.exists():
            raise DiscoveryError(f"v1 rc.yml not found: {path}")
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise DiscoveryError(f"failed to parse {path}: {e}") from e
        if not isinstance(raw, dict):
            raise DiscoveryError(f"failed to parse {path}: expected mapping at root")
        if raw.get("version") == 2:
            raise DiscoveryError(
                f"{path} is a v2 rc.yml (version: 2). v1_migrate only "
                "accepts v1-shaped rc.yml as input."
            )
        services = {}
        for name, svc_raw in (raw.get("services") or {}).items():
            services[name] = V1Service(
                name=name,
                cpu=svc_raw.get("cpu", 0),
                memory=svc_raw.get("memory", 0),
                type=svc_raw.get("type", ""),
                public=bool(svc_raw.get("public", False)),
                port=svc_raw.get("port"),
                health_check_path=svc_raw.get("health_check_path"),
                default_target=bool(svc_raw.get("default_target", False)),
                ephemeral_storage=svc_raw.get("ephemeral_storage"),
                raw=svc_raw,
            )
        return cls(
            cluster=raw.get("cluster", ""),
            region=raw.get("region", ""),
            aws_profile=raw.get("aws_profile", ""),
            project_name=raw.get("project_name", ""),
            compose_file=raw.get("compose_file", ""),
            domain=raw.get("domain", ""),
            vpc_cidr=raw.get("vpc_cidr", ""),
            services=services,
            secrets_files=list(raw.get("secrets") or []),
            backup=raw.get("backup"),
            raw=raw,
        )


# ---------------------------------------------------------------------
# Resource inventory dataclasses
# ---------------------------------------------------------------------

@dataclass
class EfsAccessPoint:
    ap_id: str
    name: str
    path: str
    uid: int
    gid: int
    live_postgres_mount: bool = False


@dataclass
class EfsFileSystem:
    file_system_id: str
    name: str
    size_bytes: int
    lifecycle_state: str
    access_points: list[EfsAccessPoint] = field(default_factory=list)

    def live_postgres_access_point(self) -> EfsAccessPoint:
        for ap in self.access_points:
            if ap.live_postgres_mount:
                return ap
        raise DiscoveryError(
            "no access point flagged live_postgres_mount=True; "
            "cannot identify the live postgres data volume"
        )


@dataclass
class AlbListener:
    arn: str
    port: int
    protocol: str
    default_action_type: str
    certificate_arn: str | None = None


@dataclass
class AlbTargetGroup:
    name: str
    arn: str
    port: int
    health_check_path: str = ""


@dataclass
class Alb:
    name: str
    arn: str
    dns_name: str
    scheme: str
    listeners: list[AlbListener] = field(default_factory=list)
    target_groups: list[AlbTargetGroup] = field(default_factory=list)


@dataclass
class AcmCert:
    arn: str
    domain_name: str
    status: str


@dataclass
class Route53Record:
    name: str
    type: str
    ttl: int


@dataclass
class Route53Zone:
    id: str
    name: str
    records: list[Route53Record] = field(default_factory=list)

    @property
    def apex_managed_externally(self) -> bool:
        types = {r.type for r in self.records}
        return types.issubset({"NS", "SOA"})


@dataclass
class SmSecret:
    name: str
    arn: str


@dataclass
class Vpc:
    id: str
    cidr_block: str
    tags: dict = field(default_factory=dict)
    subnets: list[str] = field(default_factory=list)
    security_groups: list[str] = field(default_factory=list)


@dataclass
class IamConfig:
    task_execution_role_arn: str
    task_role_arn: str
    external: bool = True


@dataclass
class EcsCluster:
    name: str
    arn: str
    active_services_count: int = 0
    running_tasks_count: int = 0


@dataclass
class EcrRepo:
    name: str
    uri: str


@dataclass
class ResourceInventory:
    region: str = ""
    account_id: str = ""
    ecs_cluster: EcsCluster | None = None
    ecs_services: list[dict] = field(default_factory=list)
    efs: EfsFileSystem | None = None
    alb: Alb | None = None
    acm_cert: AcmCert | None = None
    route53_zone: Route53Zone | None = None
    secrets: list[SmSecret] = field(default_factory=list)
    secrets_truncated: bool = False
    vpc: Vpc | None = None
    iam: IamConfig | None = None
    ecr_repositories: list[EcrRepo] = field(default_factory=list)

    @classmethod
    def from_json(cls, path: Path) -> "ResourceInventory":
        path = Path(path)
        if not path.exists():
            raise DiscoveryError(f"inventory snapshot not found: {path}")
        try:
            d = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise DiscoveryError(f"failed to parse {path}: {e}") from e
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceInventory":
        cluster_d = d.get("ecs_cluster") or {}
        cluster = EcsCluster(
            name=cluster_d.get("name", ""),
            arn=cluster_d.get("arn", ""),
            active_services_count=cluster_d.get("active_services_count", 0),
            running_tasks_count=cluster_d.get("running_tasks_count", 0),
        )

        efs_d = d.get("efs") or {}
        aps = [
            EfsAccessPoint(
                ap_id=ap.get("ap_id", ""),
                name=ap.get("name", ""),
                path=ap.get("path", ""),
                uid=ap.get("uid", 0),
                gid=ap.get("gid", 0),
                live_postgres_mount=bool(ap.get("live_postgres_mount", False)),
            )
            for ap in efs_d.get("access_points", [])
        ]
        efs = EfsFileSystem(
            file_system_id=efs_d.get("file_system_id", ""),
            name=efs_d.get("name", ""),
            size_bytes=efs_d.get("size_bytes", 0),
            lifecycle_state=efs_d.get("lifecycle_state", ""),
            access_points=aps,
        ) if efs_d else None

        alb_d = d.get("alb") or {}
        alb = Alb(
            name=alb_d.get("name", ""),
            arn=alb_d.get("arn", ""),
            dns_name=alb_d.get("dns_name", ""),
            scheme=alb_d.get("scheme", ""),
            listeners=[
                AlbListener(
                    arn=l.get("arn", ""),
                    port=l.get("port", 0),
                    protocol=l.get("protocol", ""),
                    default_action_type=l.get("default_action_type", ""),
                    certificate_arn=l.get("certificate_arn"),
                )
                for l in alb_d.get("listeners", [])
            ],
            target_groups=[
                AlbTargetGroup(
                    name=tg.get("name", ""),
                    arn=tg.get("arn", ""),
                    port=tg.get("port", 0),
                    health_check_path=tg.get("health_check_path", ""),
                )
                for tg in alb_d.get("target_groups", [])
            ],
        ) if alb_d else None

        acm_d = d.get("acm_cert") or {}
        acm = AcmCert(
            arn=acm_d.get("arn", ""),
            domain_name=acm_d.get("domain_name", ""),
            status=acm_d.get("status", ""),
        ) if acm_d else None

        zone_d = d.get("route53_zone") or {}
        zone = Route53Zone(
            id=zone_d.get("id", ""),
            name=zone_d.get("name", ""),
            records=[
                Route53Record(
                    name=r.get("name", ""),
                    type=r.get("type", ""),
                    ttl=r.get("ttl", 0),
                )
                for r in zone_d.get("records", [])
            ],
        ) if zone_d else None

        secrets_raw = d.get("sm_secrets") or []
        secrets: list[SmSecret] = []
        truncated = False
        for s in secrets_raw:
            if "_truncated_for_brevity" in s:
                truncated = True
                continue
            secrets.append(SmSecret(name=s.get("name", ""), arn=s.get("arn", "")))

        vpc_d = d.get("vpc") or {}
        vpc = Vpc(
            id=vpc_d.get("id", ""),
            cidr_block=vpc_d.get("cidr_block", ""),
            tags=vpc_d.get("tags", {}),
            subnets=list(vpc_d.get("subnets", [])),
            security_groups=list(vpc_d.get("security_groups", [])),
        ) if vpc_d else None

        iam_d = d.get("iam") or {}
        iam = IamConfig(
            task_execution_role_arn=iam_d.get("task_execution_role_arn", ""),
            task_role_arn=iam_d.get("task_role_arn", ""),
            external=True,
        ) if iam_d else None

        ecr = [
            EcrRepo(name=r.get("name", ""), uri=r.get("uri", ""))
            for r in d.get("ecr_repositories", [])
        ]

        return cls(
            region=d.get("region", ""),
            account_id=d.get("account_id", ""),
            ecs_cluster=cluster,
            ecs_services=list(d.get("ecs_services", [])),
            efs=efs,
            alb=alb,
            acm_cert=acm,
            route53_zone=zone,
            secrets=secrets,
            secrets_truncated=truncated,
            vpc=vpc,
            iam=iam,
            ecr_repositories=ecr,
        )

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "account_id": self.account_id,
            "ecs_cluster": {
                "name": self.ecs_cluster.name if self.ecs_cluster else "",
                "arn": self.ecs_cluster.arn if self.ecs_cluster else "",
                "active_services_count": self.ecs_cluster.active_services_count if self.ecs_cluster else 0,
                "running_tasks_count": self.ecs_cluster.running_tasks_count if self.ecs_cluster else 0,
            },
            "efs": {
                "file_system_id": self.efs.file_system_id,
                "name": self.efs.name,
                "size_bytes": self.efs.size_bytes,
                "lifecycle_state": self.efs.lifecycle_state,
                "access_points": [
                    {
                        "ap_id": ap.ap_id,
                        "name": ap.name,
                        "path": ap.path,
                        "uid": ap.uid,
                        "gid": ap.gid,
                        "live_postgres_mount": ap.live_postgres_mount,
                    }
                    for ap in self.efs.access_points
                ],
            } if self.efs else {},
            "alb": {
                "name": self.alb.name,
                "arn": self.alb.arn,
                "dns_name": self.alb.dns_name,
                "scheme": self.alb.scheme,
                "listeners": [
                    {
                        "arn": l.arn,
                        "port": l.port,
                        "protocol": l.protocol,
                        "default_action_type": l.default_action_type,
                        "certificate_arn": l.certificate_arn,
                    }
                    for l in self.alb.listeners
                ],
                "target_groups": [
                    {
                        "name": tg.name,
                        "arn": tg.arn,
                        "port": tg.port,
                        "health_check_path": tg.health_check_path,
                    }
                    for tg in self.alb.target_groups
                ],
            } if self.alb else {},
            "acm_cert": {
                "arn": self.acm_cert.arn,
                "domain_name": self.acm_cert.domain_name,
                "status": self.acm_cert.status,
            } if self.acm_cert else {},
            "route53_zone": {
                "id": self.route53_zone.id,
                "name": self.route53_zone.name,
                "records": [
                    {"name": r.name, "type": r.type, "ttl": r.ttl}
                    for r in self.route53_zone.records
                ],
            } if self.route53_zone else {},
            "sm_secrets": [
                {"name": s.name, "arn": s.arn} for s in self.secrets
            ],
            "vpc": {
                "id": self.vpc.id,
                "cidr_block": self.vpc.cidr_block,
                "tags": self.vpc.tags,
                "subnets": self.vpc.subnets,
                "security_groups": self.vpc.security_groups,
            } if self.vpc else {},
            "iam": {
                "task_execution_role_arn": self.iam.task_execution_role_arn,
                "task_role_arn": self.iam.task_role_arn,
            } if self.iam else {},
            "ecr_repositories": [
                {"name": r.name, "uri": r.uri} for r in self.ecr_repositories
            ],
        }

    def secret(self, short_name: str) -> SmSecret | None:
        for s in self.secrets:
            full = s.name
            if full == short_name or full.endswith(f"/{short_name}"):
                return s
        return None

    @classmethod
    def from_aws(cls, session: Any, cluster: str) -> "ResourceInventory":
        """Snapshot live AWS state via boto3 clients on `session`.

        Discovers everything tagged remote-compose:cluster=<cluster>
        plus the cluster's own ECS services + their referenced EFS,
        ALB, ACM, SM, ECR resources.
        """
        region = session.region_name
        sts = session.client("sts")
        try:
            account_id = sts.get_caller_identity()["Account"]
        except Exception:
            account_id = ""

        # ECS cluster
        ecs = session.client("ecs")
        ecs_cluster = EcsCluster(name=cluster, arn="")
        try:
            r = ecs.describe_clusters(clusters=[cluster])
            cs = r.get("clusters", [])
            if cs:
                ecs_cluster = EcsCluster(
                    name=cs[0].get("clusterName", cluster),
                    arn=cs[0].get("clusterArn", ""),
                    active_services_count=cs[0].get("activeServicesCount", 0),
                    running_tasks_count=cs[0].get("runningTasksCount", 0),
                )
        except Exception:
            pass

        # VPC by tag
        ec2 = session.client("ec2")
        vpc = None
        vpcs = ec2.describe_vpcs(Filters=[
            {"Name": "tag:remote-compose:cluster", "Values": [cluster]},
        ]).get("Vpcs", [])
        if not vpcs:
            # fall back to any tagged remote-compose:managed=true with no cluster filter
            vpcs = ec2.describe_vpcs(Filters=[
                {"Name": "tag:remote-compose:managed", "Values": ["true"]},
            ]).get("Vpcs", [])
        if vpcs:
            v = vpcs[0]
            tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
            subnets = [
                s["SubnetId"]
                for s in ec2.describe_subnets(Filters=[
                    {"Name": "vpc-id", "Values": [v["VpcId"]]},
                ]).get("Subnets", [])
            ]
            sgs = [
                g["GroupId"]
                for g in ec2.describe_security_groups(Filters=[
                    {"Name": "vpc-id", "Values": [v["VpcId"]]},
                ]).get("SecurityGroups", [])
            ]
            vpc = Vpc(
                id=v["VpcId"], cidr_block=v.get("CidrBlock", ""),
                tags=tags, subnets=subnets, security_groups=sgs,
            )

        # EFS — first file system in the region (sandbox shape)
        efs_client = session.client("efs")
        efs = None
        fs_list = efs_client.describe_file_systems().get("FileSystems", [])
        if fs_list:
            fs = fs_list[0]
            ap_list = efs_client.describe_access_points(
                FileSystemId=fs["FileSystemId"],
            ).get("AccessPoints", [])
            aps = [
                EfsAccessPoint(
                    ap_id=ap["AccessPointId"],
                    name=ap.get("Name", ""),
                    path=(ap.get("RootDirectory") or {}).get("Path", ""),
                    uid=(ap.get("PosixUser") or {}).get("Uid", 0),
                    gid=(ap.get("PosixUser") or {}).get("Gid", 0),
                    live_postgres_mount="postgres" in (
                        (ap.get("RootDirectory") or {}).get("Path", "").lower()
                    ),
                )
                for ap in ap_list
            ]
            efs = EfsFileSystem(
                file_system_id=fs["FileSystemId"],
                name=fs.get("Name", ""),
                size_bytes=(fs.get("SizeInBytes") or {}).get("Value", 0),
                lifecycle_state=fs.get("LifeCycleState", ""),
                access_points=aps,
            )

        # ALB — first one matching name prefix
        elbv2 = session.client("elbv2")
        alb = None
        lbs = elbv2.describe_load_balancers().get("LoadBalancers", [])
        if lbs:
            lb = lbs[0]
            listeners_raw = elbv2.describe_listeners(
                LoadBalancerArn=lb["LoadBalancerArn"],
            ).get("Listeners", [])
            listeners = []
            for l in listeners_raw:
                cert_arn = None
                certs = l.get("Certificates") or []
                if certs:
                    cert_arn = certs[0].get("CertificateArn")
                actions = l.get("DefaultActions") or [{}]
                listeners.append(AlbListener(
                    arn=l["ListenerArn"], port=l.get("Port", 0),
                    protocol=l.get("Protocol", ""),
                    default_action_type=actions[0].get("Type", ""),
                    certificate_arn=cert_arn,
                ))
            try:
                tgs_raw = elbv2.describe_target_groups(
                    LoadBalancerArn=lb["LoadBalancerArn"],
                ).get("TargetGroups", [])
            except Exception:
                tgs_raw = []
            tgs = [
                AlbTargetGroup(
                    name=tg["TargetGroupName"], arn=tg["TargetGroupArn"],
                    port=tg.get("Port", 0),
                    health_check_path=tg.get("HealthCheckPath", ""),
                )
                for tg in tgs_raw
            ]
            alb = Alb(
                name=lb.get("LoadBalancerName", ""),
                arn=lb["LoadBalancerArn"],
                dns_name=lb.get("DNSName", ""),
                scheme=lb.get("Scheme", ""),
                listeners=listeners, target_groups=tgs,
            )

        # ACM — first ISSUED cert
        acm_client = session.client("acm")
        acm_cert = None
        certs = acm_client.list_certificates().get("CertificateSummaryList", [])
        if certs:
            arn = certs[0]["CertificateArn"]
            try:
                detail = acm_client.describe_certificate(CertificateArn=arn).get("Certificate", {})
                acm_cert = AcmCert(
                    arn=arn,
                    domain_name=detail.get("DomainName", certs[0].get("DomainName", "")),
                    status=detail.get("Status", "ISSUED"),
                )
            except Exception:
                acm_cert = AcmCert(
                    arn=arn,
                    domain_name=certs[0].get("DomainName", ""),
                    status="ISSUED",
                )

        # SM secrets — list all that match project_name prefix
        sm = session.client("secretsmanager")
        secrets: list[SmSecret] = []
        try:
            paginator = sm.get_paginator("list_secrets")
            for page in paginator.paginate():
                for s in page.get("SecretList", []):
                    secrets.append(SmSecret(name=s["Name"], arn=s["ARN"]))
        except Exception:
            pass

        # IAM — well-known role names
        iam = IamConfig(
            task_execution_role_arn=f"arn:aws:iam::{account_id}:role/ecsTaskExecutionRole",
            task_role_arn=f"arn:aws:iam::{account_id}:role/ecsTaskRole",
            external=True,
        )

        # ECR
        ecr_client = session.client("ecr")
        ecr_repos = []
        try:
            for r in ecr_client.describe_repositories().get("repositories", []):
                ecr_repos.append(EcrRepo(
                    name=r["repositoryName"], uri=r["repositoryUri"],
                ))
        except Exception:
            pass

        return cls(
            region=region or "",
            account_id=account_id,
            ecs_cluster=ecs_cluster,
            ecs_services=[],
            efs=efs,
            alb=alb,
            acm_cert=acm_cert,
            route53_zone=None,
            secrets=secrets,
            vpc=vpc,
            iam=iam,
            ecr_repositories=ecr_repos,
        )


# ---------------------------------------------------------------------
# discover() composite
# ---------------------------------------------------------------------

def discover(
    rc_v1_yml_path: Path,
    aws_session: Any = None,
    inventory_snapshot: Path | None = None,
) -> tuple[V1Stack, ResourceInventory]:
    stack = V1Stack.from_yaml(rc_v1_yml_path)
    if inventory_snapshot is not None:
        inv = ResourceInventory.from_json(inventory_snapshot)
    elif aws_session is not None:
        inv = ResourceInventory.from_aws(aws_session, stack.cluster)
    else:
        raise DiscoveryError(
            "discover() requires either aws_session or inventory_snapshot"
        )
    return stack, inv
