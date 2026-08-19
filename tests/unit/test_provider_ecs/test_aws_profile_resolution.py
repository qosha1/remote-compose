"""rc-rigk: aws_profile must not render into the terraform provider on CI.

`profile = "{{ aws_profile }}"` used to render whenever
provider_config.ecs.aws_profile was set. A named profile is a workstation
concept — an OIDC runner has credentials in the environment and no shared
config file at all — so the line killed every stateful CI deploy with
terraform's "failed to get shared config profile, default".

The decision lives in preflight() (which plan() and deploy() both call) so
emit_terraform() stays a pure renderer: called on its own it emits exactly
what rc.yml says and never depends on the host's ~/.aws.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs import provider as ecs_provider
from remote_compose.provider.ecs.provider import (
    PROFILE_ABSENT,
    PROFILE_PRESENT,
    PROFILE_UNKNOWN,
    PROFILE_UNSET,
    _AMBIENT_CREDENTIAL_ENV_VARS,
    _ambient_aws_credentials,
    _aws_profile_status,
    check_aws_profile_for_terraform,
)

ALL_AMBIENT = list(_AMBIENT_CREDENTIAL_ENV_VARS)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Every test states its own credential environment explicitly.

    Without this the result depends on whatever AWS_* the developer happens
    to have exported, which is exactly the host-dependence this feature is
    about.
    """
    for var in ALL_AMBIENT:
        monkeypatch.delenv(var, raising=False)


def _ctx(tmp_path: Path, aws_profile="default") -> DeployContext:
    ecs: dict = {"region": "us-west-2", "cluster": "c", "vpc_cidr": "10.0.0.0/16"}
    if aws_profile is not None:
        ecs["aws_profile"] = aws_profile
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={"api": ServiceSpec(name="api", cpu=256, memory=512)},
    )


def _pin_status(monkeypatch, status: str) -> None:
    monkeypatch.setattr(ecs_provider, "_aws_profile_status", lambda _p: status)


class TestProfileStatus:
    def test_unset_profile_is_unset(self):
        assert _aws_profile_status(None) == PROFILE_UNSET
        assert _aws_profile_status("") == PROFILE_UNSET

    def test_missing_profile_is_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nope-config"))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope-creds"))
        assert _aws_profile_status("rc-not-a-real-profile-xyz") == PROFILE_ABSENT

    def test_declared_profile_is_present(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text("[profile rc-test-prof]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope-creds"))
        assert _aws_profile_status("rc-test-prof") == PROFILE_PRESENT

    def test_failed_probe_is_unknown_not_absent(self, monkeypatch):
        """A probe that can't answer must not be read as 'profile missing'."""
        import botocore.session

        def _boom():
            raise RuntimeError("no botocore for you")

        monkeypatch.setattr(botocore.session, "Session", _boom)
        assert _aws_profile_status("anything") == PROFILE_UNKNOWN

    def test_profile_is_resolvable_collapses_unknown_to_false(self, monkeypatch):
        _pin_status(monkeypatch, PROFILE_UNKNOWN)
        # _profile_is_resolvable is module-local, so re-read it through the
        # module to pick up the patched status probe.
        assert ecs_provider._profile_is_resolvable("x") is False
        _pin_status(monkeypatch, PROFILE_PRESENT)
        assert ecs_provider._profile_is_resolvable("x") is True


class TestAmbientCredentialDetection:
    @pytest.mark.parametrize("var", ALL_AMBIENT)
    def test_each_ambient_var_counts(self, monkeypatch, var):
        monkeypatch.setenv(var, "set")
        assert _ambient_aws_credentials() == [var]

    def test_aws_profile_env_is_not_ambient_credentials(self, monkeypatch):
        """AWS_PROFILE names a profile; it does not supply credentials."""
        monkeypatch.setenv("AWS_PROFILE", "some-profile")
        assert _ambient_aws_credentials() == []

    def test_nothing_set(self):
        assert _ambient_aws_credentials() == []


class TestCheckAwsProfileForTerraform:
    def test_present_profile_is_rendered_even_with_ambient_creds(self, monkeypatch):
        """An explicit, working profile wins — it is what the user asked for."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
        _pin_status(monkeypatch, PROFILE_PRESENT)
        omit, msg = ecs_provider.check_aws_profile_for_terraform("prod")
        assert omit is False and msg == ""

    def test_absent_profile_with_ambient_creds_is_omitted_and_warned(self, monkeypatch):
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/t/tok")
        _pin_status(monkeypatch, PROFILE_ABSENT)
        omit, msg = ecs_provider.check_aws_profile_for_terraform("default")
        assert omit is True
        assert "AWS_WEB_IDENTITY_TOKEN_FILE" in msg
        # The warning names terraform's own error string so a user searching
        # for what they saw in CI lands on it.
        assert "failed to get shared config profile, default" in msg

    def test_absent_profile_without_ambient_creds_is_not_omitted(self, monkeypatch):
        """Nothing to fall back to — the caller raises instead."""
        _pin_status(monkeypatch, PROFILE_ABSENT)
        assert ecs_provider.check_aws_profile_for_terraform("default") == (False, "")

    def test_unknown_probe_never_drops_a_configured_profile(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
        _pin_status(monkeypatch, PROFILE_UNKNOWN)
        assert ecs_provider.check_aws_profile_for_terraform("prod") == (False, "")

    def test_unset_profile_is_a_no_op(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
        assert check_aws_profile_for_terraform(None) == (False, "")


class TestPreflightIntegration:
    def test_ci_shape_omits_profile_and_warns(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::1:role/deploy")
        _pin_status(monkeypatch, PROFILE_ABSENT)
        provider = ECSProvider()
        ctx = _ctx(tmp_path, aws_profile="default")

        provider.preflight(ctx)

        assert ctx.omit_aws_profile is True
        assert any("aws_profile" in w for w in provider._warnings)

        out = tmp_path / "tf"
        provider.emit_terraform(ctx, out)
        assert "profile =" not in (out / "providers.tf").read_text()

    def test_laptop_shape_with_no_creds_raises_naming_the_profile(
        self, monkeypatch, tmp_path
    ):
        _pin_status(monkeypatch, PROFILE_ABSENT)
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider().preflight(_ctx(tmp_path, aws_profile="prod-acct"))
        msg = str(exc.value)
        assert "prod-acct" in msg
        assert "aws configure --profile prod-acct" in msg

    def test_resolvable_profile_still_renders(self, monkeypatch, tmp_path):
        _pin_status(monkeypatch, PROFILE_PRESENT)
        provider = ECSProvider()
        ctx = _ctx(tmp_path, aws_profile="prod")
        provider.preflight(ctx)
        assert ctx.omit_aws_profile is False
        out = tmp_path / "tf"
        provider.emit_terraform(ctx, out)
        assert 'profile = "prod"' in (out / "providers.tf").read_text()

    def test_no_state_deploy_skips_the_check_entirely(self, monkeypatch, tmp_path):
        """--no-state renders no provider block; its boto3 session already
        falls back to the ambient chain."""
        _pin_status(monkeypatch, PROFILE_ABSENT)
        ctx = _ctx(tmp_path, aws_profile="default")
        ctx.skip_terraform = True
        ECSProvider().preflight(ctx)  # must not raise
        assert ctx.omit_aws_profile is False

    def test_emit_terraform_alone_never_probes_the_host(self, monkeypatch, tmp_path):
        """Rendering is deterministic: no ~/.aws lookup, no env dependence."""

        def _fail(_p):
            raise AssertionError("emit_terraform must not probe AWS config")

        monkeypatch.setattr(ecs_provider, "_aws_profile_status", _fail)
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path, aws_profile="whatever"), out)
        assert 'profile = "whatever"' in (out / "providers.tf").read_text()
