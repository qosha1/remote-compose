"""Discover the live v1 stack: parse rc v1 yaml + snapshot live AWS state.

These tests run against tests/fixtures/v1_migrate/* — a frozen copy of
production state at 2026-04-27 — so the parser is proven against real
prod-shaped input, not toy examples.

The fixture set covers every resource type the migration tooling has to
reason about: ECS cluster + 7 services, EFS with the live postgres
mount (133 GB on fsap-004097e867c7bb755), ALB + listeners + target
groups, ACM cert, Route53 zone (registrar-managed apex), 32 SM
secrets, VPC + subnets + SGs, IAM roles (external/account-wide), ECR
repos.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.v1_migrate.discover import (
    DiscoveryError,
    ResourceInventory,
    V1Stack,
    discover,
)

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "v1_migrate"
V1_RC_YML = FIXTURES / "ss-debuggai-prod.rc.yml"
INVENTORY_JSON = FIXTURES / "inventory.json"


# ---------------------------------------------------------------------
# V1Stack: parsed from rc v1 yaml
# ---------------------------------------------------------------------


class TestV1StackParse:
    def test_basic_shape(self):
        stack = V1Stack.from_yaml(V1_RC_YML)
        assert stack.cluster == "ss-debuggai-prod"
        assert stack.region == "us-west-2"
        assert stack.aws_profile == "debuggai"
        assert stack.project_name == "ss-debuggai"
        assert stack.compose_file.endswith("docker-compose.ecs.yml")

    def test_services_extracted(self):
        stack = V1Stack.from_yaml(V1_RC_YML)
        names = sorted(stack.services.keys())
        assert "django" in names
        assert "postgres" in names
        assert "redis" in names
        assert "nginx" in names
        assert "celery-worker" in names
        assert "celery-beat" in names
        assert "celery-worker-linkedin" in names

    def test_django_service_resources_carried(self):
        stack = V1Stack.from_yaml(V1_RC_YML)
        django = stack.services["django"]
        assert django.cpu == 1024
        assert django.memory == 4096
        assert django.health_check_path == "/api/health/"

    def test_nginx_is_public_default_target(self):
        stack = V1Stack.from_yaml(V1_RC_YML)
        nginx = stack.services["nginx"]
        assert nginx.public is True
        assert nginx.default_target is True
        assert nginx.port == 80

    def test_domain_extracted(self):
        stack = V1Stack.from_yaml(V1_RC_YML)
        assert stack.domain == "api.startsimpli.com"


# ---------------------------------------------------------------------
# ResourceInventory: parsed from boto3 snapshot
# ---------------------------------------------------------------------


class TestResourceInventoryParse:
    def test_basic_shape(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        assert inv.region == "us-west-2"
        assert inv.account_id == "033937118837"

    def test_ecs_cluster(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        assert inv.ecs_cluster.name == "ss-debuggai-prod"
        assert inv.ecs_cluster.arn.startswith("arn:aws:ecs:us-west-2:")
        assert inv.ecs_cluster.active_services_count == 7

    def test_efs_carries_size_and_live_mount(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        assert inv.efs.file_system_id == "fs-0e8a2f9d1e006af95"
        # 133 GB — must survive cutover; failing this test means we lost
        # track of the prod data volume.
        assert inv.efs.size_bytes > 100_000_000_000
        live = inv.efs.live_postgres_access_point()
        assert live.ap_id == "fsap-004097e867c7bb755"
        assert live.path == "/ss-debuggai/postgres_data"

    def test_alb_listeners(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        ports = sorted(lst.port for lst in inv.alb.listeners)
        assert ports == [80, 443]

    def test_acm_cert_for_domain(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        assert inv.acm_cert.domain_name == "api.startsimpli.com"
        assert inv.acm_cert.status == "ISSUED"

    def test_route53_is_subzone_only(self):
        # api.startsimpli.com lives at the registrar; the AWS subzone
        # has only NS+SOA. Migration must not touch DNS.
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        types = sorted(r.type for r in inv.route53_zone.records)
        assert types == ["NS", "SOA"]

    def test_secrets_indexed_by_short_name(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        # rc.yml references secrets by short name; ARN is the full
        # path. The inventory must let translators look up either.
        secret = inv.secret("POSTGRES_PASSWORD")
        assert secret is not None
        assert secret.arn.startswith(
            "arn:aws:secretsmanager:us-west-2:033937118837:secret:"
            "ss-debuggai-prod/POSTGRES_PASSWORD"
        )

    def test_vpc_already_rc_managed(self):
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        assert inv.vpc.tags.get("remote-compose:managed") == "true"
        assert inv.vpc.cidr_block == "10.0.0.0/16"

    def test_iam_roles_marked_external(self):
        # Task exec + task role are account-wide, NOT managed by this
        # project. v2 must reference by ARN, never recreate.
        inv = ResourceInventory.from_json(INVENTORY_JSON)
        assert inv.iam.external is True
        assert inv.iam.task_execution_role_arn.endswith("/ecsTaskExecutionRole")


# ---------------------------------------------------------------------
# discover() composes V1Stack + ResourceInventory from real inputs
# ---------------------------------------------------------------------


class TestDiscoverComposite:
    def test_returns_pair(self, tmp_path):
        # Mocked AWS session — discover() must accept a snapshot path
        # (test mode) instead of hitting real AWS.
        stack, inv = discover(
            rc_v1_yml_path=V1_RC_YML,
            inventory_snapshot=INVENTORY_JSON,
        )
        assert isinstance(stack, V1Stack)
        assert isinstance(inv, ResourceInventory)
        # Cross-check: stack.cluster matches inventory.cluster
        assert stack.cluster == inv.ecs_cluster.name

    def test_missing_rc_yml_raises(self, tmp_path):
        with pytest.raises(DiscoveryError, match="not found"):
            discover(
                rc_v1_yml_path=tmp_path / "nope.yml",
                inventory_snapshot=INVENTORY_JSON,
            )

    def test_malformed_rc_yml_raises(self, tmp_path):
        bad = tmp_path / "bad.yml"
        bad.write_text("not: valid: yaml: at all")
        with pytest.raises(DiscoveryError, match="parse"):
            discover(
                rc_v1_yml_path=bad,
                inventory_snapshot=INVENTORY_JSON,
            )

    def test_v1_v2_schema_mismatch_raises(self, tmp_path):
        # Passing a v2 rc.yml in here is a user error — must fail
        # loudly, not silently misinterpret it as v1.
        v2_yml = tmp_path / "v2.yml"
        v2_yml.write_text("version: 2\nproject: foo\nprovider: ecs\n")
        with pytest.raises(DiscoveryError, match="v1"):
            discover(
                rc_v1_yml_path=v2_yml,
                inventory_snapshot=INVENTORY_JSON,
            )
