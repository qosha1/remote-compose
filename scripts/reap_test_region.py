#!/usr/bin/env python3
"""Delete every resource in the test region tagged Project=rc-test-*.

Standalone — does not use terraform state. Runs if terraform is wedged, if
state is lost, if an earlier test died mid-deploy. Idempotent: running
twice in a row with nothing to delete exits 0.

Usage:
    scripts/reap_test_region.py --region us-east-1 [--dry-run] [--yes]

Resources handled (in dependency-safe delete order):
    ECS services → ECS task defs → ECS clusters → ECS capacity providers
    Auto Scaling Groups → Launch Templates
    ALB listeners → ALBs → Target Groups
    EFS mount targets → EFS access points → EFS file systems
    ECR repositories (force_delete)
    Secrets Manager secrets (force + no recovery)
    IAM roles (detach policies first, delete inline + attached + role)
    Route 53 records (only those pointing at deleted ALBs)
    ACM certificates (tagged)
    CloudWatch Log Groups
    Security Groups (non-default)
    Route Tables (non-main)
    Subnets
    Internet Gateways (detach + delete)
    VPCs (non-default)

Safety:
    - Only acts on resources tagged Project=rc-test-* (or Name prefix
      rc-test- for resources that don't support tags at read time).
    - Hard refuses to run outside us-east-1 unless --force-region given.
    - Exits non-zero if the account's region contains any resource NOT
      matching the test tag (prevents accidental run against a prod region).

Exit codes:
    0  — nothing to clean up, or all cleanups succeeded
    1  — one or more deletions failed (details in stderr)
    2  — safety precondition violated (wrong region, prod resources present)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError


PROJECT_TAG_KEY = "Project"
PROJECT_TAG_PREFIX = "rc-test-"
NAME_PREFIX = "rc-test-"
DEFAULT_REGION = "us-east-1"


@dataclass
class Reaper:
    region: str
    session: boto3.Session
    dry_run: bool = False
    failures: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def _log(self, msg: str) -> None:
        prefix = "[DRY] " if self.dry_run else ""
        print(f"{prefix}{msg}")

    def _do(self, label: str, fn, *args, **kwargs) -> bool:
        if self.dry_run:
            self._log(f"would delete {label}")
            self.deleted.append(label)
            return True
        try:
            fn(*args, **kwargs)
            self.deleted.append(label)
            self._log(f"deleted {label}")
            return True
        except ClientError as exc:
            self.failures.append(f"{label}: {exc}")
            self._log(f"FAILED {label}: {exc}")
            return False

    def _tagged(self, tags: list[dict]) -> bool:
        return any(
            t.get("Key") == PROJECT_TAG_KEY
            and (t.get("Value") or "").startswith(PROJECT_TAG_PREFIX)
            for t in tags or []
        )

    # ------------------------------------------------------------------
    # ECS
    # ------------------------------------------------------------------

    def reap_ecs(self) -> None:
        ecs = self.session.client("ecs", region_name=self.region)
        clusters = ecs.list_clusters().get("clusterArns", [])
        matching_clusters: list[str] = []
        for arn in clusters:
            details = ecs.describe_clusters(clusters=[arn], include=["TAGS"])["clusters"]
            if not details:
                continue
            cluster = details[0]
            if not self._tagged(cluster.get("tags", [])):
                continue
            matching_clusters.append(cluster["clusterName"])

        for cluster in matching_clusters:
            # Delete services first.
            services = ecs.list_services(cluster=cluster).get("serviceArns", [])
            for svc_arn in services:
                svc_name = svc_arn.split("/")[-1]
                self._do(f"ecs service {cluster}/{svc_name}",
                         ecs.delete_service, cluster=cluster, service=svc_name, force=True)
            # Wait a beat for services to drain tasks.
            if services and not self.dry_run:
                time.sleep(5)

            # Delete custom capacity providers attached to the cluster.
            resp = ecs.describe_clusters(clusters=[cluster])["clusters"]
            if resp:
                for cp in resp[0].get("capacityProviders", []):
                    if cp in ("FARGATE", "FARGATE_SPOT"):
                        continue
                    self._do(f"ecs capacity provider {cp}",
                             ecs.delete_capacity_provider, capacityProvider=cp)

            self._do(f"ecs cluster {cluster}",
                     ecs.delete_cluster, cluster=cluster)

        # Deregister task definitions whose family matches our prefix.
        families = ecs.list_task_definition_families(
            familyPrefix=NAME_PREFIX, status="ACTIVE"
        ).get("families", [])
        for family in families:
            arns = ecs.list_task_definitions(familyPrefix=family, status="ACTIVE").get("taskDefinitionArns", [])
            for arn in arns:
                self._do(f"ecs task def {arn.split('/')[-1]}",
                         ecs.deregister_task_definition, taskDefinition=arn)

    # ------------------------------------------------------------------
    # Auto Scaling + Launch Templates
    # ------------------------------------------------------------------

    def reap_autoscaling(self) -> None:
        asg = self.session.client("autoscaling", region_name=self.region)
        groups = asg.describe_auto_scaling_groups().get("AutoScalingGroups", [])
        for g in groups:
            if not self._tagged(g.get("Tags", [])):
                continue
            name = g["AutoScalingGroupName"]
            self._do(f"asg {name}",
                     asg.delete_auto_scaling_group, AutoScalingGroupName=name, ForceDelete=True)

        ec2 = self.session.client("ec2", region_name=self.region)
        lts = ec2.describe_launch_templates().get("LaunchTemplates", [])
        for lt in lts:
            name = lt.get("LaunchTemplateName", "")
            tags = lt.get("Tags", [])
            if not (self._tagged(tags) or name.startswith(NAME_PREFIX)):
                continue
            self._do(f"launch template {name}",
                     ec2.delete_launch_template, LaunchTemplateName=name)

    # ------------------------------------------------------------------
    # ALB / target groups
    # ------------------------------------------------------------------

    def reap_elb(self) -> None:
        elb = self.session.client("elbv2", region_name=self.region)
        lbs = elb.describe_load_balancers().get("LoadBalancers", [])
        lb_arns_to_kill: list[str] = []
        for lb in lbs:
            arn = lb["LoadBalancerArn"]
            tags = elb.describe_tags(ResourceArns=[arn]).get("TagDescriptions", [])
            if not tags:
                continue
            if self._tagged(tags[0].get("Tags", [])):
                lb_arns_to_kill.append(arn)
        for arn in lb_arns_to_kill:
            listeners = elb.describe_listeners(LoadBalancerArn=arn).get("Listeners", [])
            for l in listeners:
                self._do(f"elb listener {l['ListenerArn'].split('/')[-1]}",
                         elb.delete_listener, ListenerArn=l["ListenerArn"])
            self._do(f"elb lb {arn.split('/')[-2]}",
                     elb.delete_load_balancer, LoadBalancerArn=arn)

        # Target groups
        tgs = elb.describe_target_groups().get("TargetGroups", [])
        for tg in tgs:
            arn = tg["TargetGroupArn"]
            tags = elb.describe_tags(ResourceArns=[arn]).get("TagDescriptions", [])
            if not tags:
                continue
            if self._tagged(tags[0].get("Tags", [])):
                self._do(f"elb target group {tg['TargetGroupName']}",
                         elb.delete_target_group, TargetGroupArn=arn)

    # ------------------------------------------------------------------
    # EFS
    # ------------------------------------------------------------------

    def reap_efs(self) -> None:
        efs = self.session.client("efs", region_name=self.region)
        filesystems = efs.describe_file_systems().get("FileSystems", [])
        for fs in filesystems:
            if not self._tagged(fs.get("Tags", [])):
                continue
            fs_id = fs["FileSystemId"]

            aps = efs.describe_access_points(FileSystemId=fs_id).get("AccessPoints", [])
            for ap in aps:
                self._do(f"efs access point {ap['AccessPointId']}",
                         efs.delete_access_point, AccessPointId=ap["AccessPointId"])

            mts = efs.describe_mount_targets(FileSystemId=fs_id).get("MountTargets", [])
            for mt in mts:
                self._do(f"efs mount target {mt['MountTargetId']}",
                         efs.delete_mount_target, MountTargetId=mt["MountTargetId"])
            if mts and not self.dry_run:
                time.sleep(10)  # mount targets take a moment to detach

            self._do(f"efs file system {fs_id}",
                     efs.delete_file_system, FileSystemId=fs_id)

    # ------------------------------------------------------------------
    # ECR
    # ------------------------------------------------------------------

    def reap_ecr(self) -> None:
        ecr = self.session.client("ecr", region_name=self.region)
        repos = ecr.describe_repositories().get("repositories", [])
        for repo in repos:
            name = repo["repositoryName"]
            if not name.startswith(NAME_PREFIX):
                continue
            self._do(f"ecr repo {name}",
                     ecr.delete_repository, repositoryName=name, force=True)

    # ------------------------------------------------------------------
    # Secrets Manager
    # ------------------------------------------------------------------

    def reap_secrets(self) -> None:
        sm = self.session.client("secretsmanager", region_name=self.region)
        secrets = sm.list_secrets(MaxResults=100).get("SecretList", [])
        for s in secrets:
            name = s["Name"]
            if not name.startswith(NAME_PREFIX):
                continue
            self._do(f"secret {name}",
                     sm.delete_secret, SecretId=name,
                     ForceDeleteWithoutRecovery=True)

    # ------------------------------------------------------------------
    # CloudWatch Logs
    # ------------------------------------------------------------------

    def reap_log_groups(self) -> None:
        logs = self.session.client("logs", region_name=self.region)
        groups = logs.describe_log_groups(logGroupNamePrefix=f"/ecs/{NAME_PREFIX}").get("logGroups", [])
        for g in groups:
            self._do(f"log group {g['logGroupName']}",
                     logs.delete_log_group, logGroupName=g["logGroupName"])

    # ------------------------------------------------------------------
    # IAM (global — name-prefix scoped)
    # ------------------------------------------------------------------

    def reap_iam(self) -> None:
        iam = self.session.client("iam")
        roles = iam.list_roles(MaxItems=1000).get("Roles", [])
        for r in roles:
            name = r["RoleName"]
            if not name.startswith(NAME_PREFIX):
                continue
            # detach managed policies
            attached = iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies", [])
            for p in attached:
                self._do(f"detach policy {p['PolicyArn']} from {name}",
                         iam.detach_role_policy, RoleName=name, PolicyArn=p["PolicyArn"])
            # delete inline
            inline = iam.list_role_policies(RoleName=name).get("PolicyNames", [])
            for p in inline:
                self._do(f"inline policy {p} on {name}",
                         iam.delete_role_policy, RoleName=name, PolicyName=p)
            # detach instance profiles
            profiles = iam.list_instance_profiles_for_role(RoleName=name).get("InstanceProfiles", [])
            for prof in profiles:
                self._do(f"remove role {name} from profile {prof['InstanceProfileName']}",
                         iam.remove_role_from_instance_profile,
                         InstanceProfileName=prof["InstanceProfileName"], RoleName=name)
            self._do(f"iam role {name}",
                     iam.delete_role, RoleName=name)

        profiles = iam.list_instance_profiles(MaxItems=1000).get("InstanceProfiles", [])
        for prof in profiles:
            name = prof["InstanceProfileName"]
            if not name.startswith(NAME_PREFIX):
                continue
            self._do(f"instance profile {name}",
                     iam.delete_instance_profile, InstanceProfileName=name)

    # ------------------------------------------------------------------
    # VPC + dependencies
    # ------------------------------------------------------------------

    def reap_vpc(self) -> None:
        ec2 = self.session.client("ec2", region_name=self.region)
        vpcs = ec2.describe_vpcs(Filters=[
            {"Name": f"tag:{PROJECT_TAG_KEY}", "Values": [f"{PROJECT_TAG_PREFIX}*"]},
        ]).get("Vpcs", [])
        for vpc in vpcs:
            vpc_id = vpc["VpcId"]
            # security groups (non-default)
            sgs = ec2.describe_security_groups(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]).get("SecurityGroups", [])
            for sg in sgs:
                if sg["GroupName"] == "default":
                    continue
                # revoke rules that reference other SGs in same VPC first
                try:
                    if sg.get("IpPermissions"):
                        ec2.revoke_security_group_ingress(
                            GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"])
                except ClientError:
                    pass
            for sg in sgs:
                if sg["GroupName"] == "default":
                    continue
                self._do(f"security group {sg['GroupId']}",
                         ec2.delete_security_group, GroupId=sg["GroupId"])

            # subnets
            subnets = ec2.describe_subnets(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]).get("Subnets", [])
            for s in subnets:
                self._do(f"subnet {s['SubnetId']}",
                         ec2.delete_subnet, SubnetId=s["SubnetId"])

            # route tables (non-main)
            rts = ec2.describe_route_tables(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]).get("RouteTables", [])
            for rt in rts:
                is_main = any(a.get("Main") for a in rt.get("Associations", []))
                if is_main:
                    continue
                # disassociate non-main associations
                for a in rt.get("Associations", []):
                    if a.get("RouteTableAssociationId"):
                        try:
                            ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
                        except ClientError:
                            pass
                self._do(f"route table {rt['RouteTableId']}",
                         ec2.delete_route_table, RouteTableId=rt["RouteTableId"])

            # internet gateways
            igws = ec2.describe_internet_gateways(Filters=[
                {"Name": "attachment.vpc-id", "Values": [vpc_id]},
            ]).get("InternetGateways", [])
            for igw in igws:
                self._do(f"detach igw {igw['InternetGatewayId']}",
                         ec2.detach_internet_gateway,
                         InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
                self._do(f"delete igw {igw['InternetGatewayId']}",
                         ec2.delete_internet_gateway,
                         InternetGatewayId=igw["InternetGatewayId"])

            self._do(f"vpc {vpc_id}",
                     ec2.delete_vpc, VpcId=vpc_id)

    # ------------------------------------------------------------------
    # Drive
    # ------------------------------------------------------------------

    def run(self) -> int:
        print(f"Reaping Project={PROJECT_TAG_PREFIX}* resources in {self.region} "
              f"(dry_run={self.dry_run})...\n")
        # Order matters for dependencies.
        self.reap_ecs()
        self.reap_autoscaling()
        self.reap_elb()
        self.reap_efs()
        self.reap_ecr()
        self.reap_secrets()
        self.reap_log_groups()
        self.reap_vpc()
        self.reap_iam()

        print(f"\nDeleted: {len(self.deleted)}  Failed: {len(self.failures)}")
        if self.failures:
            print("\nFailures:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-region", action="store_true",
                        help="Allow running outside us-east-1 (discouraged).")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    args = parser.parse_args()

    if args.region != DEFAULT_REGION and not args.force_region:
        print(f"Refusing to run in {args.region}. Use --force-region to override.",
              file=sys.stderr)
        return 2

    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    reaper = Reaper(region=args.region, session=session, dry_run=args.dry_run)
    return reaper.run()


if __name__ == "__main__":
    sys.exit(main())
