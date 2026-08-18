"""adopt_owned ALB support (rc-v4c).

rc's ECS provider has exactly two ALB modes today: create-and-own (a fresh
``aws_lb``), and ``existing_alb`` read-only reference (a ``data "aws_lb"``
rc never creates, updates, or destroys). Neither lets rc actually retire a
foreign stack's grip on a live, shared ALB — the concrete forcing case is
browser-mgr's AWS Copilot CloudFormation env stack, which is the sole owner
of the ALB serving browser-mgr.debugg.ai today.

``provider_config.ecs.adopt_owned.alb`` adds a third mode: rc emits a real
``aws_lb``/``aws_lb_listener`` *resource* (not a data source) for the given
foreign ARNs, so a one-time ``terraform import`` brings it under rc's state
and rc holds delete/update authority going forward — while a blanket
``lifecycle { ignore_changes = all }`` means rc never tries to reconcile the
adopted resource's live attributes against what it would render from
scratch (which would show up as a destructive diff: Copilot's ALB is named
``browse-Publi-...``, not ``${project}-alb``).

GENERAL + opt-in + strictly ADDITIVE: with no ``adopt_owned`` the emitted
terraform is byte-identical (guarded by test_golden.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import TerraformError

_ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:"
    "loadbalancer/app/browse-Publi-zc7tlO4ZlkmK/e3c84967632dbde1"
)
_HTTP_LISTENER_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:"
    "listener/app/browse-Publi-zc7tlO4ZlkmK/e3c84967632dbde1/1111111"
)
_HTTPS_LISTENER_ARN = (
    "arn:aws:elasticloadbalancing:us-east-2:033937118837:"
    "listener/app/browse-Publi-zc7tlO4ZlkmK/e3c84967632dbde1/2222222"
)
_SG_IDS = ["sg-0aaa1111", "sg-0bbb2222"]

ADOPT_OWNED_ALB = {
    "arn": _ALB_ARN,
    "http_listener_arn": _HTTP_LISTENER_ARN,
    "https_listener_arn": _HTTPS_LISTENER_ARN,
    "security_group_ids": _SG_IDS,
}


def _ctx(tmp_path: Path, ecs_overrides: dict | None = None, **over) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "browser-mgr-prod",
        "vpc_id": "vpc-0b6967",
        "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
        "security_group_ids": ["sg-013b"],
        "route53_zone": "debugg.ai",
    }
    ecs_cfg.update(ecs_overrides or {})
    services = over.pop("services", None) or {
        "django": ServiceSpec(
            name="django",
            cpu=512,
            memory=1024,
            type="application",
            public=True,
            port=5000,
            domain="browser-mgr.debugg.ai",
            health_check_path="/api/health/",
        ),
    }
    return DeployContext(
        project="browser-mgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={
            "tls": {"mode": "manual", "certificate_arn": "arn:aws:acm:x:y:cert/z"}
        },
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _emit(tmp_path, **over):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(
        _ctx(tmp_path, {"adopt_owned": {"alb": ADOPT_OWNED_ALB}}, **over), out
    )
    return out


def _resource_block(tf_text: str, header: str) -> str:
    """Extract a single `resource "type" "name" { ... }` block by brace
    matching, so assertions don't depend on incidental blank-line placement.
    """
    start = tf_text.index(header)
    brace_start = tf_text.index("{", start)
    depth = 0
    for i in range(brace_start, len(tf_text)):
        if tf_text[i] == "{":
            depth += 1
        elif tf_text[i] == "}":
            depth -= 1
            if depth == 0:
                return tf_text[brace_start : i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


class TestAdoptOwnedAlbEmission:
    def test_resource_emitted_not_data_source(self, tmp_path):
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        assert 'resource "aws_lb" "main"' in alb
        assert 'data "aws_lb" "main"' not in alb
        assert 'data "aws_lb_listener"' not in alb

    def test_alb_has_ignore_changes_all(self, tmp_path):
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        lb_block = _resource_block(alb, 'resource "aws_lb" "main"')
        assert "lifecycle" in lb_block
        assert "ignore_changes = all" in lb_block

    def test_alb_security_groups_are_literal_ids(self, tmp_path):
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        assert 'security_groups    = ["sg-0aaa1111", "sg-0bbb2222"]' in alb

    def test_no_rc_managed_alb_security_group_resource(self, tmp_path):
        sg = (_emit(tmp_path) / "security_groups.tf").read_text()
        assert 'resource "aws_security_group" "alb"' not in sg

    def test_listeners_are_owned_resources_with_ignore_changes(self, tmp_path):
        alb = (_emit(tmp_path) / "alb.tf").read_text()
        assert 'resource "aws_lb_listener" "http"' in alb
        assert 'resource "aws_lb_listener" "https"' in alb
        https_block = _resource_block(alb, 'resource "aws_lb_listener" "https"')
        assert "ignore_changes = all" in https_block
        http_block = _resource_block(alb, 'resource "aws_lb_listener" "http"')
        assert "ignore_changes = all" in http_block

    def test_default_target_group_still_created_fresh(self, tmp_path):
        # Copilot's own DefaultHTTPTargetGroup is dead weight rc doesn't
        # inherit; rc creates its own, same as plain create-mode — but only
        # when the default (first public, alphabetically) service has no
        # domain of its own (otherwise ITS per-service TG acts as default,
        # same rule as plain create-mode — unrelated to adopt_owned).
        services = {
            "api": ServiceSpec(
                name="api",
                cpu=256,
                memory=512,
                type="application",
                public=True,
                port=8000,
                health_check_path="/",
            ),
            "django": ServiceSpec(
                name="django",
                cpu=512,
                memory=1024,
                type="application",
                public=True,
                port=5000,
                domain="browser-mgr.debugg.ai",
                health_check_path="/api/health/",
            ),
        }
        alb = (_emit(tmp_path, services=services) / "alb.tf").read_text()
        assert 'resource "aws_lb_target_group" "default"' in alb

    def test_outputs_and_r53_use_resource_not_data_source(self, tmp_path):
        out = _emit(tmp_path)
        assert "value = aws_lb.main.dns_name" in (out / "outputs.tf").read_text()
        domain = (out / "domain.tf").read_text()
        assert "name                   = aws_lb.main.dns_name" in domain
        assert "zone_id                = aws_lb.main.zone_id" in domain


class TestAdoptOwnedAlbValidation:
    def test_mutually_exclusive_with_existing_alb(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="mutually exclusive"):
            ECSProvider().emit_terraform(
                _ctx(
                    tmp_path,
                    {
                        "adopt_owned": {"alb": ADOPT_OWNED_ALB},
                        "existing_alb": {
                            "arn": _ALB_ARN,
                            "https_listener_arn": _HTTPS_LISTENER_ARN,
                        },
                    },
                ),
                tmp_path / "tf",
            )

    def test_requires_arn_listener_and_security_groups(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="adopt_owned.alb requires"):
            ECSProvider().emit_terraform(
                _ctx(
                    tmp_path,
                    {"adopt_owned": {"alb": {"arn": _ALB_ARN}}},
                ),
                tmp_path / "tf",
            )

    def test_https_listener_required_when_domain_set(self, tmp_path):
        cfg = dict(ADOPT_OWNED_ALB)
        del cfg["https_listener_arn"]
        with pytest.raises(ProviderConfigError, match="https_listener_arn"):
            ECSProvider().emit_terraform(
                _ctx(tmp_path, {"adopt_owned": {"alb": cfg}}), tmp_path / "tf"
            )

    def test_ec2_launch_type_admits_adopted_alb_security_groups(self, tmp_path):
        services = {
            "django": ServiceSpec(
                name="django",
                cpu=512,
                memory=1024,
                type="application",
                public=True,
                port=5000,
                domain="browser-mgr.debugg.ai",
                health_check_path="/api/health/",
                launch_type="EC2",
            ),
        }
        out = ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                {"adopt_owned": {"alb": ADOPT_OWNED_ALB}},
                services=services,
            ),
            tmp_path / "tf",
        )
        capacity = (out / "capacity.tf").read_text()
        # ec2_instances SG ingress admits the adopted ALB's own (literal,
        # foreign) security groups -- there is no rc-created
        # aws_security_group.alb to reference in adopt_owned mode.
        assert f'security_groups = ["{_SG_IDS[0]}", "{_SG_IDS[1]}"]' in capacity
        assert "aws_security_group.alb.id" not in capacity


# ---------------------------------------------------------------------------
# Reconcile: import the adopted ALB + listeners into terraform state before
# apply. Mirrors _reconcile_orphan_backup_bucket / _reconcile_orphan_log_groups
# (boto3-stub-style mocks; real boto3/terraform never invoked).
# ---------------------------------------------------------------------------


@pytest.fixture
def adopt_ctx(tmp_path: Path) -> DeployContext:
    return _ctx(tmp_path, {"adopt_owned": {"alb": ADOPT_OWNED_ALB}})


class TestAdoptOwnedAlbReconcileHappyPath:
    def test_imports_alb_and_both_listeners(self, adopt_ctx):
        elbv2 = MagicMock()
        elbv2.describe_load_balancers.return_value = {
            "LoadBalancers": [{"LoadBalancerArn": _ALB_ARN}]
        }
        elbv2.describe_listeners.return_value = {
            "Listeners": [
                {"ListenerArn": _HTTP_LISTENER_ARN},
                {"ListenerArn": _HTTPS_LISTENER_ARN},
            ]
        }
        session = MagicMock()
        session.client.return_value = elbv2

        progress: list[str] = []
        provider = ECSProvider(
            session_factory=lambda c: session, progress=progress.append
        )
        runner = MagicMock()
        runner.import_resource.return_value = None

        provider._reconcile_adopt_owned_alb(adopt_ctx, runner)

        runner.import_resource.assert_any_call("aws_lb.main", _ALB_ARN)
        runner.import_resource.assert_any_call(
            "aws_lb_listener.http", _HTTP_LISTENER_ARN
        )
        runner.import_resource.assert_any_call(
            "aws_lb_listener.https", _HTTPS_LISTENER_ARN
        )
        assert runner.import_resource.call_count == 3
        assert any("imported adopt_owned ALB" in m for m in progress)

    def test_noop_when_adopt_owned_alb_not_configured(self, tmp_path):
        ctx = _ctx(tmp_path)  # no adopt_owned config at all
        runner = MagicMock()
        provider = ECSProvider(session_factory=lambda c: MagicMock())

        provider._reconcile_adopt_owned_alb(ctx, runner)

        runner.import_resource.assert_not_called()

    def test_already_imported_is_idempotent(self, adopt_ctx):
        elbv2 = MagicMock()
        elbv2.describe_load_balancers.return_value = {
            "LoadBalancers": [{"LoadBalancerArn": _ALB_ARN}]
        }
        elbv2.describe_listeners.return_value = {
            "Listeners": [
                {"ListenerArn": _HTTP_LISTENER_ARN},
                {"ListenerArn": _HTTPS_LISTENER_ARN},
            ]
        }
        session = MagicMock()
        session.client.return_value = elbv2

        provider = ECSProvider(session_factory=lambda c: session)
        runner = MagicMock()
        runner.import_resource.side_effect = TerraformError(
            cmd=["terraform", "import"],
            returncode=1,
            stdout="",
            stderr="Error: Resource already managed by Terraform",
        )

        # Must not raise — already-managed is a no-op, not a failure.
        provider._reconcile_adopt_owned_alb(adopt_ctx, runner)


class TestAdoptOwnedAlbReconcileMisconfiguration:
    """The foreign IDs in rc.yml are user-supplied literals, unlike the
    orphan reconcilers' self-derived IDs. If the ALB the user named isn't
    actually live, that's misconfiguration, not "nothing to import yet" —
    proceeding to apply would have terraform CREATE A BRAND NEW ALB while
    the real one keeps serving traffic. This must be a hard error.
    """

    def test_missing_alb_is_a_hard_error_not_silent_skip(self, adopt_ctx):
        elbv2 = MagicMock()
        elbv2.describe_load_balancers.return_value = {"LoadBalancers": []}
        session = MagicMock()
        session.client.return_value = elbv2

        provider = ECSProvider(session_factory=lambda c: session)
        runner = MagicMock()

        with pytest.raises(ProviderConfigError, match=_ALB_ARN):
            provider._reconcile_adopt_owned_alb(adopt_ctx, runner)

        runner.import_resource.assert_not_called()

    def test_import_failure_for_other_reason_is_not_swallowed(self, adopt_ctx):
        elbv2 = MagicMock()
        elbv2.describe_load_balancers.return_value = {
            "LoadBalancers": [{"LoadBalancerArn": _ALB_ARN}]
        }
        elbv2.describe_listeners.return_value = {
            "Listeners": [
                {"ListenerArn": _HTTP_LISTENER_ARN},
                {"ListenerArn": _HTTPS_LISTENER_ARN},
            ]
        }
        session = MagicMock()
        session.client.return_value = elbv2

        provider = ECSProvider(session_factory=lambda c: session)
        runner = MagicMock()
        runner.import_resource.side_effect = TerraformError(
            cmd=["terraform", "import"],
            returncode=1,
            stdout="",
            stderr="some other terraform error",
        )

        # Unlike the orphan reconcilers, there is NO delete-and-recreate
        # fallback here — this is live foreign prod infra, not something
        # rc can safely nuke. Failure must propagate.
        with pytest.raises(TerraformError):
            provider._reconcile_adopt_owned_alb(adopt_ctx, runner)
