"""Packing several projects onto ONE ECS cluster and its EC2 instances.

An ECS container instance registers to exactly ONE cluster. So "put all the
tenants on one or two boxes" is not a sizing question at all -- it requires the
tenants to SHARE A CLUSTER. Today rc always creates its own, which puts a hard
floor of one instance per tenant underneath the whole estate.

Measured on foundry-tenant-obwbqa, the first EC2 tenant (2026-08-19): 6 tasks
declaring 2304 MiB on an m6i.large that registers 7817 MiB -- 29% of memory but
60% of ENI, because every awsvpc task takes one branch ENI and an m6i.large
carries 10 of them. ENI slots, not memory, are what a box runs out of. ECS
places tasks individually, so what an instance holds is 10 tasks from any mix of
projects; today each project gets its own cluster and therefore its own
instance. The per-tenant migration cannot pay until this exists.

Two things are needed, and neither is useful without the other:

  existing_cluster     adopt a cluster (and its EC2 capacity provider) that a
                       shared stack owns, instead of creating one. Mirrors
                       existing_alb, which already works this way.
  service_name_prefix  ECS service names are unique per CLUSTER. Every tenant
                       has a service called `django`; without a prefix the
                       second tenant into the cluster collides with the first.
"""

from __future__ import annotations

from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider

SHARED = {"name": "foundry-tenants", "capacity_provider": "foundry-tenants-ec2-cp"}


def _ctx(tmp_path, services=None, **ecs_over):
    ecs = {
        "region": "us-west-2",
        "cluster": "foundry-tenants",
        "vpc_id": "vpc-shared",
        "public_subnet_ids": ["subnet-a", "subnet-b"],
        "private_subnet_ids": ["subnet-c", "subnet-d"],
        "default_launch_type": "EC2",
    }
    ecs.update(ecs_over)
    return DeployContext(
        project="foundry-tenant-obwbqa",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services
        or {
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application"
            )
        },
        secrets=[],
    )


def _emit(tmp_path, name="tf", **ecs_over):
    out = tmp_path / name
    ECSProvider().emit_terraform(_ctx(tmp_path, **ecs_over), out)
    return out


class TestAdoptSharedCluster:
    def test_default_still_creates_its_own_cluster(self, tmp_path):
        """Every existing stack must be untouched."""
        tf = (_emit(tmp_path) / "cluster.tf").read_text()
        assert 'resource "aws_ecs_cluster" "main"' in tf
        assert 'data "aws_ecs_cluster"' not in tf

    def test_adopted_cluster_is_a_data_source(self, tmp_path):
        tf = (
            _emit(tmp_path, "tf2", existing_cluster=SHARED) / "cluster.tf"
        ).read_text()
        assert 'data "aws_ecs_cluster" "main"' in tf
        assert 'resource "aws_ecs_cluster" "main"' not in tf

    def test_adopting_does_not_claim_the_clusters_capacity_providers(self, tmp_path):
        """aws_ecs_cluster_capacity_providers is CLUSTER-scoped. If every tenant
        emitted it they would fight over one association, each apply reverting
        the last."""
        tf = (
            _emit(tmp_path, "tf3", existing_cluster=SHARED) / "cluster.tf"
        ).read_text()
        assert "aws_ecs_cluster_capacity_providers" not in tf

    def test_adopting_creates_no_asg_or_instance_role(self, tmp_path):
        """The shared stack owns the instances. A tenant that also created an ASG
        would defeat the entire point by adding its own box back."""
        out = _emit(tmp_path, "tf4", existing_cluster=SHARED)
        cap = out / "capacity.tf"
        body = cap.read_text() if cap.exists() else ""
        for r in (
            "aws_autoscaling_group",
            "aws_launch_template",
            "aws_ecs_capacity_provider",
            "aws_iam_instance_profile",
        ):
            assert r not in body, f"tenant emitted its own {r}"

    def test_services_use_the_shared_capacity_provider_by_name(self, tmp_path):
        tf = (
            _emit(tmp_path, "tf5", existing_cluster=SHARED) / "services.tf"
        ).read_text()
        assert 'capacity_provider = "foundry-tenants-ec2-cp"' in tf
        assert "aws_ecs_capacity_provider.ec2.name" not in tf

    def test_requires_a_capacity_provider_when_services_are_ec2(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="capacity_provider"):
            _emit(tmp_path, "tf6", existing_cluster={"name": "foundry-tenants"})


class TestServiceNamePrefix:
    def test_default_is_unprefixed(self, tmp_path):
        tf = (_emit(tmp_path) / "services.tf").read_text()
        assert 'name            = "django"' in tf

    def test_prefix_is_applied_to_the_ecs_service_name(self, tmp_path):
        tf = (
            _emit(tmp_path, "tf7", service_name_prefix="obwbqa-") / "services.tf"
        ).read_text()
        assert 'name            = "obwbqa-django"' in tf

    def test_two_projects_in_one_cluster_do_not_collide(self, tmp_path):
        """The actual invariant. Both tenants have a `django`; distinct prefixes
        must produce distinct ECS service names."""
        a = (
            _emit(
                tmp_path, "tfa", existing_cluster=SHARED, service_name_prefix="obwbqa-"
            )
            / "services.tf"
        ).read_text()
        b = (
            _emit(tmp_path, "tfb", existing_cluster=SHARED, service_name_prefix="mcr-")
            / "services.tf"
        ).read_text()
        import re

        def ecs_service_names(tf):
            """Only aws_ecs_service names. ECR repos and task-def families are
            project-scoped (`${var.project}/django`), so they render as the same
            literal here and resolve differently per project at apply — matching
            them would be a false collision."""
            out = set()
            for block in re.split(r'^resource "', tf, flags=re.M):
                if block.startswith('aws_ecs_service"'):
                    m = re.search(r'^\s+name\s+=\s+"([^"]+)"', block, re.M)
                    if m:
                        out.add(m.group(1))
            return out

        na, nb = ecs_service_names(a), ecs_service_names(b)
        assert na and nb, (na, nb)
        assert not (na & nb), f"collision: {na & nb}"


class TestPrefixIsAppliedAtRUNTIME:
    """The prefix has to reach the ECS API, not just the terraform (rc-py32).

    rc renders a service named "<prefix><name>" but keys ctx.services by the bare
    compose name, so every runtime path has to add the prefix back. It did not:
    the value was read once and handed to the template, and nothing else. rc
    wrote `acme-django` and then asked ECS about `django`.

    The original nine tests for this feature all asserted on rendered terraform,
    which is exactly why the gap survived them -- they checked what rc writes,
    never what rc does. These drive the provider instead.
    """

    @pytest.fixture
    def mock_session(self):
        sess = mock.MagicMock()
        sess.client.return_value = mock.MagicMock()
        return sess

    @pytest.fixture
    def provider(self, mock_session):
        return ECSProvider(session_factory=lambda ctx: mock_session)

    def test_redeploy_targets_the_prefixed_service(
        self, provider, mock_session, tmp_path
    ):
        ctx = _ctx(tmp_path, existing_cluster=SHARED, service_name_prefix="obwbqa-")
        provider.redeploy(ctx)
        kwargs = mock_session.client.return_value.update_service.call_args.kwargs
        assert kwargs["service"] == "obwbqa-django"
        assert kwargs["cluster"] == "foundry-tenants"

    def test_status_asks_for_the_prefixed_name(self, provider, mock_session, tmp_path):
        ecs = mock_session.client.return_value
        ecs.describe_services.return_value = {"services": []}
        ctx = _ctx(tmp_path, existing_cluster=SHARED, service_name_prefix="obwbqa-")
        provider.status(ctx)
        assert ecs.describe_services.call_args.kwargs["services"] == ["obwbqa-django"]

    def test_status_reports_under_the_COMPOSE_name(
        self, provider, mock_session, tmp_path
    ):
        """The user asked about `django`; they must not be told about
        `obwbqa-django`, which appears nowhere in their rc.yml."""
        ecs = mock_session.client.return_value
        ecs.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "obwbqa-django",
                    "runningCount": 1,
                    "desiredCount": 1,
                    "events": [{"message": "steady state"}],
                }
            ]
        }
        ctx = _ctx(tmp_path, existing_cluster=SHARED, service_name_prefix="obwbqa-")
        report = provider.status(ctx)
        names = {s.name for s in report.services}
        assert names == {"django"}
        assert next(iter(report.services)).health == "healthy"

    def test_unprefixed_project_is_untouched(self, provider, mock_session, tmp_path):
        """The default path must issue byte-identical calls to before."""
        ctx = _ctx(tmp_path)
        provider.redeploy(ctx)
        kwargs = mock_session.client.return_value.update_service.call_args.kwargs
        assert kwargs["service"] == "django"

    def test_a_service_named_like_the_prefix_round_trips(
        self, provider, mock_session, tmp_path
    ):
        """Stripping must not chew into a name that merely starts with the
        prefix text -- only the prefix rc actually rendered comes off."""
        ecs = mock_session.client.return_value
        ecs.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "obwbqa-obwbqa-thing",
                    "runningCount": 1,
                    "desiredCount": 1,
                    "events": [],
                }
            ]
        }
        ctx = _ctx(
            tmp_path,
            services={
                "obwbqa-thing": ServiceSpec(
                    name="obwbqa-thing", cpu=256, memory=512, type="application"
                )
            },
            existing_cluster=SHARED,
            service_name_prefix="obwbqa-",
        )
        report = provider.status(ctx)
        assert {s.name for s in report.services} == {"obwbqa-thing"}
