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

    def test_aws_profile_omitted_when_unset(self, tmp_path):
        out = tmp_path / "tf"
        ctx = _ctx(tmp_path)
        ctx.provider_config["ecs"].pop("aws_profile", None)
        ECSProvider().emit_terraform(ctx, out)
        assert "profile =" not in (out / "providers.tf").read_text()

    def test_aws_profile_passed_to_boto3_session(self, tmp_path):
        """A configured aws_profile is forwarded to boto3.Session."""
        from unittest import mock

        from remote_compose.provider.ecs.provider import _default_session_factory
        ctx = _ctx(tmp_path, aws_profile="debuggai", region="us-west-1")
        with mock.patch("boto3.Session") as MockSession:
            _default_session_factory(ctx)
        MockSession.assert_called_once_with(
            region_name="us-west-1", profile_name="debuggai"
        )

    def test_session_falls_back_when_profile_missing(self, tmp_path):
        """A configured profile that doesn't resolve (CI/OIDC, no shared AWS
        config) falls back to the default credential chain instead of raising."""
        from unittest import mock

        from botocore.exceptions import ProfileNotFound

        from remote_compose.provider.ecs.provider import _default_session_factory
        ctx = _ctx(tmp_path, aws_profile="ghost", region="us-west-1")
        calls = []

        def fake_session(**kwargs):
            calls.append(kwargs)
            if kwargs.get("profile_name"):
                raise ProfileNotFound(profile=kwargs["profile_name"])
            return mock.sentinel.session

        with mock.patch("boto3.Session", side_effect=fake_session):
            result = _default_session_factory(ctx)

        assert result is mock.sentinel.session
        # tried the named profile first, then fell back without it
        assert calls == [
            {"region_name": "us-west-1", "profile_name": "ghost"},
            {"region_name": "us-west-1"},
        ]

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
        env = tmp_path / ".app"
        env.write_text(f"SECRET_KEY={sentinel}\nOTHER=y\n")
        ctx = _ctx(tmp_path, secrets=[
            SecretRef(name="app", source="file", path=str(env)),
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


class TestManagedBackupBucket:
    """rc.yml v2 backup.bucket auto-creates an S3 bucket via terraform
    so users don't have to `aws s3api create-bucket` before rc db push.
    Opt-out via backup.bucket_managed=false for externally-owned buckets."""

    def _ctx_with_backup(self, tmp_path, **backup):
        return DeployContext(
            project=backup.pop("project", "myproj"),
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={"backup": backup or {"bucket": "myproj-backups"}},
            provider_config={"ecs": {
                "region": "us-west-2", "cluster": "test", "vpc_cidr": "10.0.0.0/16",
            }},
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                "api": ServiceSpec(name="api", cpu=512, memory=1024, type="application"),
            },
            secrets=[],
        )

    def test_backup_bucket_emitted_when_declared(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            self._ctx_with_backup(tmp_path, bucket="myproj-backups"), out,
        )
        backup_tf = (out / "backup.tf").read_text()
        assert 'aws_s3_bucket" "backups"' in backup_tf
        assert 'bucket = "myproj-backups"' in backup_tf
        assert "aws_s3_bucket_public_access_block" in backup_tf
        assert "AES256" in backup_tf

    def test_no_backup_block_means_no_bucket(self, tmp_path):
        # Same context but no backup.
        ctx = self._ctx_with_backup(tmp_path, bucket="x")
        ctx.rc_yml_v2 = {}
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        assert (out / "backup.tf").read_text().strip() == ""

    def test_bucket_managed_false_skips_creation(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            self._ctx_with_backup(
                tmp_path, bucket="external-team-bucket", bucket_managed=False,
            ),
            out,
        )
        # No aws_s3_bucket resource since the user owns it elsewhere.
        backup_tf = (out / "backup.tf").read_text()
        assert "aws_s3_bucket" not in backup_tf

    def test_rc_test_project_force_destroy_true(self, tmp_path):
        out = tmp_path / "tf"
        ctx = self._ctx_with_backup(
            tmp_path, project="rc-test-foo", bucket="rc-test-foo-backups",
        )
        ECSProvider().emit_terraform(ctx, out)
        assert "force_destroy = true" in (out / "backup.tf").read_text()

    def test_non_test_project_no_force_destroy(self, tmp_path):
        out = tmp_path / "tf"
        ctx = self._ctx_with_backup(
            tmp_path, project="prod-app", bucket="prod-app-backups",
        )
        ECSProvider().emit_terraform(ctx, out)
        assert "force_destroy" not in (out / "backup.tf").read_text()

    def test_lifecycle_rule_applied_when_retention_days(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            self._ctx_with_backup(
                tmp_path, bucket="x", retention_days=7,
            ),
            out,
        )
        backup_tf = (out / "backup.tf").read_text()
        assert "aws_s3_bucket_lifecycle_configuration" in backup_tf
        assert "days = 7" in backup_tf

    def test_lifecycle_omitted_when_retention_never(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            self._ctx_with_backup(
                tmp_path, bucket="x", retention_days="never",
            ),
            out,
        )
        backup_tf = (out / "backup.tf").read_text()
        assert "aws_s3_bucket_lifecycle_configuration" not in backup_tf


class TestContainerInsightsLogGroup:
    """ECS auto-creates /aws/ecs/containerinsights/<cluster>/performance
    when Container Insights is enabled; if terraform doesn't manage it,
    rc destroy leaks the log group every cycle. We declare it explicitly
    so destroy is truly clean."""

    def test_container_insights_log_group_emitted(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, cluster="my-cluster"), out)
        cluster = (out / "cluster.tf").read_text()
        assert 'aws_cloudwatch_log_group" "container_insights"' in cluster
        assert "/aws/ecs/containerinsights/" in cluster
        assert "${var.cluster_name}/performance" in cluster


class TestExecuteCommand:
    def test_services_enable_execute_command(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        svc_tf = (out / "services.tf").read_text()
        # Should appear once per aws_ecs_service resource.
        assert svc_tf.count("enable_execute_command = true") >= 3

    def test_task_role_has_ssmmessages_policy(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        iam_tf = (out / "iam.tf").read_text()
        assert "task_execute_command" in iam_tf
        assert "ssmmessages:CreateControlChannel" in iam_tf
        assert "ssmmessages:OpenDataChannel" in iam_tf


class TestEphemeralStorage:
    def test_emits_ephemeral_storage_block_on_fargate(self, tmp_path):
        out = tmp_path / "tf"
        services = {
            "api": ServiceSpec(
                name="api", cpu=1024, memory=4096, type="application",
                ephemeral_storage=40,
            ),
        }
        ECSProvider().emit_terraform(_ctx(tmp_path, services=services), out)
        svc_tf = (out / "services.tf").read_text()
        assert "ephemeral_storage {" in svc_tf
        assert "size_in_gib = 40" in svc_tf

    def test_unset_ephemeral_storage_omits_block(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        assert "ephemeral_storage {" not in (out / "services.tf").read_text()

    def test_ephemeral_storage_on_ec2_raises(self, tmp_path):
        services = {
            "api": ServiceSpec(
                name="api", cpu=1024, memory=4096, type="application",
                launch_type="EC2", ephemeral_storage=40,
            ),
        }
        with pytest.raises(ProviderConfigError, match="ephemeral_storage"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, services=services), tmp_path / "tf"
            )

    @pytest.mark.parametrize("bad", [0, 20, 201, 500])
    def test_ephemeral_storage_out_of_range_raises(self, tmp_path, bad):
        services = {
            "api": ServiceSpec(
                name="api", cpu=1024, memory=4096, type="application",
                ephemeral_storage=bad,
            ),
        }
        with pytest.raises(ProviderConfigError, match="between 21 and 200"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, services=services), tmp_path / "tf"
            )
