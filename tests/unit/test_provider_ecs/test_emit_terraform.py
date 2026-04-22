"""Unit tests for ECSProvider.emit_terraform (Phase 6b).

These tests assert on the rendered HCL without invoking terraform. The
``terraform init && terraform validate`` truth test runs in
tests/integration/test_provider_ecs_terraform.py and skips cleanly when
terraform is not usable in the current environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, SecretRef, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.base import ProviderConfigError


def _ctx(tmp_path: Path, **overrides) -> DeployContext:
    services = overrides.pop("services", None) or {
        "web": ServiceSpec(
            name="web", cpu=256, memory=512, replicas=1, type="proxy",
            public=True, port=80, health_check_path="/health",
        ),
        "api": ServiceSpec(
            name="api", cpu=512, memory=1024, replicas=2, type="application",
        ),
        "cache": ServiceSpec(
            name="cache", cpu=256, memory=512, type="infrastructure",
        ),
    }
    return DeployContext(
        project=overrides.pop("project", "myapp"),
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": overrides.pop("region", "us-west-2"),
                "cluster": overrides.pop("cluster", "myapp-prod"),
                "aws_profile": overrides.pop("aws_profile", "default"),
                "vpc_cidr": overrides.pop("vpc_cidr", "10.0.0.0/16"),
            }
        },
        tf_backend_config=overrides.pop(
            "tf_backend", {"type": "s3", "bucket": "tf", "key": "myapp.tfstate", "region": "us-west-2"}
        ),
        working_dir=tmp_path,
        services=services,
        secrets=overrides.pop("secrets", []),
    )


class TestEmitTerraformStructural:
    def test_writes_expected_files(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        expected = {
            "backend.tf", "providers.tf", "variables.tf", "network.tf",
            "security_groups.tf", "alb.tf", "iam.tf", "cluster.tf",
            "services.tf", "outputs.tf", "README.md",
        }
        actual = {p.name for p in out.iterdir()}
        assert expected.issubset(actual), f"missing: {expected - actual}"

    def test_region_injected_into_variables(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, region="eu-central-1"), out)
        assert '"eu-central-1"' in (out / "variables.tf").read_text()

    def test_cluster_name_injected(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, cluster="my-cluster"), out)
        assert '"my-cluster"' in (out / "variables.tf").read_text()

    def test_aws_profile_in_providers(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, aws_profile="prod"), out)
        assert 'profile = "prod"' in (out / "providers.tf").read_text()

    def test_missing_region_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.provider_config["ecs"].pop("region")
        with pytest.raises(ProviderConfigError, match="region"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")

    def test_vpc_cidr_defaults_when_absent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.provider_config["ecs"].pop("vpc_cidr")
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert '"10.0.0.0/16"' in (out / "variables.tf").read_text()


class TestServices:
    def test_each_service_has_ecr_task_def_service(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        services_tf = (out / "services.tf").read_text()
        for svc in ("web", "api", "cache"):
            assert f'aws_ecr_repository" "{svc}"' in services_tf
            assert f'aws_ecs_task_definition" "{svc}"' in services_tf
            assert f'aws_ecs_service" "{svc}"' in services_tf

    def test_public_service_attached_to_alb(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        services_tf = (out / "services.tf").read_text()
        assert "load_balancer {" in services_tf
        assert "aws_lb_target_group.default.arn" in services_tf

    def test_replicas_flow_to_desired_count(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        services_tf = (out / "services.tf").read_text()
        import re
        counts = re.findall(r"desired_count\s*=\s*(\d+)", services_tf)
        assert "2" in counts, f"expected replicas=2 to appear; got {counts}"
        assert counts.count("1") >= 1

    def test_service_name_with_dash_sanitized_for_terraform(self, tmp_path):
        ctx = _ctx(tmp_path, services={
            "celery-worker": ServiceSpec(
                name="celery-worker", cpu=1024, memory=2048, type="worker",
            ),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        assert 'aws_ecr_repository" "celery_worker"' in services_tf
        assert '"${var.project}/celery-worker"' in services_tf

    def test_invalid_launch_type_rejected(self, tmp_path):
        ctx = _ctx(tmp_path, services={
            "web": ServiceSpec(name="web", cpu=256, memory=512,
                               type="application", launch_type="BOGUS"),
        })
        from remote_compose.provider.base import ProviderConfigError
        with pytest.raises(ProviderConfigError, match="launch_type"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")


class TestAlb:
    def test_alb_rendered_when_public_service_present(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        alb_tf = (out / "alb.tf").read_text()
        assert "aws_lb" in alb_tf
        assert "aws_lb_target_group" in alb_tf

    def test_alb_empty_when_no_public_service(self, tmp_path):
        ctx = _ctx(tmp_path, services={
            "worker": ServiceSpec(name="worker", cpu=256, memory=512, type="worker"),
        })
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        alb_tf = (out / "alb.tf").read_text()
        assert "aws_lb" not in alb_tf

    def test_target_group_uses_service_health_check(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        alb_tf = (out / "alb.tf").read_text()
        assert 'path                = "/health"' in alb_tf


class TestBackendIntegration:
    def test_s3_backend_rendered(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        backend_tf = (out / "backend.tf").read_text()
        assert 'backend "s3"' in backend_tf
        assert '"tf"' in backend_tf  # bucket

    def test_local_backend_when_requested(self, tmp_path):
        ctx = _ctx(tmp_path, tf_backend={"type": "local"})
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert 'backend "local"' in (out / "backend.tf").read_text()


class TestDeterminism:
    def test_byte_identical_across_runs(self, tmp_path):
        ctx = _ctx(tmp_path)
        a = tmp_path / "a"
        b = tmp_path / "b"
        ECSProvider().emit_terraform(ctx, a)
        ECSProvider().emit_terraform(ctx, b)
        for name in sorted(p.name for p in a.iterdir()):
            assert (a / name).read_bytes() == (b / name).read_bytes(), (
                f"mismatch in {name}"
            )


class TestSecretsLeakage:
    def test_secret_values_never_in_emitted_hcl(self, tmp_path):
        sentinel = "SECRET_SENTINEL_abc123"
        ctx = _ctx(tmp_path, secrets=[
            SecretRef(name="app", source="file", path=f"/tmp/{sentinel}"),
        ])
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        for tf in out.glob("*.tf"):
            assert sentinel not in tf.read_text(), f"sentinel leaked into {tf.name}"


class TestRollbackLocalBackendRejected:
    def test_local_backend_rollback_raises(self, tmp_path):
        from remote_compose.provider.base import ProviderError
        ctx = _ctx(tmp_path, tf_backend={"type": "local"})
        with pytest.raises(ProviderError, match="local terraform backend"):
            ECSProvider().rollback(ctx)
