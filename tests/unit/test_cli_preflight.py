"""`rc preflight` — one command, every missing prerequisite (rc-g3jy).

The command renders the terraform (so the IAM action set is derived from what
rc is actually about to apply), then runs the full check suite and reports
everything at once. AWS is stubbed end to end; nothing here touches a real
account.
"""

from __future__ import annotations

import json

import pytest
import yaml
from click.testing import CliRunner

from remote_compose.cli import cli
from remote_compose.provider.ecs import provider as ecs_provider

_RC = {
    "version": 2,
    "project": "preflight-test",
    "compose_file": "docker-compose.yml",
    "provider": "ecs",
    "provider_config": {"ecs": {"region": "us-west-2", "cluster": "preflight-cluster"}},
    "services": {
        "web": {"cpu": 256, "memory": 512, "type": "proxy", "public": True, "port": 80}
    },
    "terraform": {
        "backend": {
            "type": "s3",
            "bucket": "acct-tfstate",
            "key": "preflight-test/ecs.tfstate",
            "region": "us-west-2",
            "dynamodb_table": "tf-locks",
        }
    },
}


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _S3:
    def __init__(self, error=None, version="1.0.0"):
        self.error = error
        self.version = version

    def get_object(self, Bucket, Key):
        if self.error:
            raise self.error
        return {"Body": _Body(json.dumps({"terraform_version": self.version}).encode())}


class _DDB:
    def get_item(self, TableName, Key):
        return {}

    def put_item(self, TableName, Item):
        pass

    def delete_item(self, TableName, Key):
        pass


class _IAM:
    def __init__(self, denied=()):
        self.denied = set(denied)

    def simulate_principal_policy(self, PolicySourceArn, ActionNames, **kwargs):
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": a,
                    "EvalDecision": "implicitDeny" if a in self.denied else "allowed",
                }
                for a in ActionNames
            ]
        }


class _STS:
    def get_caller_identity(self):
        return {"Arn": "arn:aws:sts::123456789012:assumed-role/rc-deploy/gh"}


class _Session:
    def __init__(self, s3=None, iam=None):
        self._clients = {
            "sts": _STS(),
            "s3": s3 or _S3(),
            "dynamodb": _DDB(),
            "iam": iam or _IAM(),
            # emit_terraform's own preflight for adopted VPCs never runs here
            # (no vpc_id), but keep ec2 available so an unexpected call is a
            # clear failure rather than a KeyError deep in a traceback.
            "ec2": object(),
        }

    def client(self, name, region_name=None):
        return self._clients[name]


@pytest.fixture
def rc_yml(tmp_path):
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(_RC))
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx\n"
    )
    return p


@pytest.fixture
def stub_aws(monkeypatch):
    """Point the provider's session factory at a scripted fake."""

    def _install(session):
        monkeypatch.setattr(
            ecs_provider, "_default_session_factory", lambda _ctx: session
        )

    return _install


def _invoke(rc_yml, *args):
    return CliRunner().invoke(cli, ["-c", str(rc_yml), "preflight", *args])


class TestPreflightCommand:
    def test_clean_environment_exits_zero_and_lists_every_check(self, rc_yml, stub_aws):
        stub_aws(_Session())
        res = _invoke(rc_yml)
        assert res.exit_code == 0, res.output
        for name in ("terraform binary", "aws identity", "state backend", "state lock"):
            assert name in res.output

    def test_green_run_against_the_wrong_principal_does_not_claim_all_clear(
        self, rc_yml, stub_aws
    ):
        """rc-zu1x: 'All prerequisites satisfied' from an admin laptop is true
        and irrelevant — the deploy runs as a CI role this never touched, and
        an operator reasonably reads that line as 'the deploy will work'."""
        stub_aws(_Session())
        res = _invoke(rc_yml)
        assert res.exit_code == 0, res.output
        assert "All prerequisites satisfied" not in res.output
        assert "probably not the principal that deploys" in res.output
        assert "deploy_role_arn" in res.output

    def test_configured_deploy_role_is_checked_and_reported_as_such(
        self, rc_yml, stub_aws
    ):
        import yaml as _yaml

        cfg = dict(_RC)
        cfg["provider_config"] = {
            "ecs": dict(
                _RC["provider_config"]["ecs"],
                deploy_role_arn="arn:aws:iam::033937118837:role/app-github-deploy",
            )
        }
        rc_yml.write_text(_yaml.safe_dump(cfg))
        stub_aws(_Session())
        res = _invoke(rc_yml)
        assert res.exit_code == 0, res.output
        assert "All prerequisites satisfied" in res.output
        assert "app-github-deploy" in res.output
        assert "the configured deploy principal" in res.output

    def test_principal_flag_overrides_the_caller(self, rc_yml, stub_aws):
        seen: list[str] = []

        class _RecordingIAM(_IAM):
            def simulate_principal_policy(self, PolicySourceArn, ActionNames, **kw):
                seen.append(PolicySourceArn)
                return super().simulate_principal_policy(
                    PolicySourceArn, ActionNames, **kw
                )

        stub_aws(_Session(iam=_RecordingIAM()))
        res = _invoke(rc_yml, "--principal", "arn:aws:iam::1:role/ci-role")
        assert res.exit_code == 0, res.output
        assert set(seen) == {"arn:aws:iam::1:role/ci-role"}

    def test_reports_every_problem_at_once_and_exits_nonzero(self, rc_yml, stub_aws):
        """Three failed deploys became one report."""
        stub_aws(
            _Session(
                s3=_S3(error=_ClientError("AccessDenied")),
                iam=_IAM(denied=["logs:TagResource", "ecs:RegisterTaskDefinition"]),
            )
        )
        res = _invoke(rc_yml)
        assert res.exit_code == 1
        # Both the state failure AND the IAM failures, in one report.
        assert "access denied reading state" in res.output
        assert "logs:TagResource" in res.output
        assert "ecs:RegisterTaskDefinition" in res.output
        # Grouped by service so the reader can act service by service.
        assert "ecs (1)" in res.output and "logs (1)" in res.output
        # And a fragment the user can paste straight into the deploy role.
        assert "RemoteComposeDeployMissingActions" in res.output

    def test_only_checks_actions_the_rendered_module_actually_needs(
        self, rc_yml, stub_aws
    ):
        """This stack declares no domain, so no aws_route53_record is
        rendered and route53 is not in the checked set. Deriving from the
        emitted terraform rather than from a fixed list is what keeps the
        report about THIS deploy."""
        seen: list[str] = []

        class _RecordingIAM(_IAM):
            def simulate_principal_policy(self, PolicySourceArn, ActionNames, **kw):
                seen.extend(ActionNames)
                return super().simulate_principal_policy(
                    PolicySourceArn, ActionNames, **kw
                )

        stub_aws(_Session(iam=_RecordingIAM()))
        assert _invoke(rc_yml).exit_code == 0
        assert not [a for a in seen if a.startswith("route53:")]

        # Declare a domain and the ACM + route53 actions appear.
        import yaml as _yaml

        with_domain = dict(_RC, domain="api.example.com", tls={"mode": "acm"})
        rc_yml.write_text(_yaml.safe_dump(with_domain))
        seen.clear()
        assert _invoke(rc_yml).exit_code == 0
        assert "route53:ChangeResourceRecordSets" in seen
        assert "acm:RequestCertificate" in seen

    def test_json_output_is_machine_readable(self, rc_yml, stub_aws):
        stub_aws(_Session())
        res = _invoke(rc_yml, "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["ok"] is True
        assert {c["name"] for c in payload["checks"]} >= {
            "terraform binary",
            "state backend",
            "state lock",
            "deploy principal IAM",
        }
        assert payload["unmodeled_resource_types"] == []

    def test_derives_actions_from_the_rendered_module(self, rc_yml, stub_aws):
        """A public web service means an ALB, so elasticloadbalancing actions
        must be in the checked set — derived from the emitted .tf, not from a
        plan (a plan needs the state access preflight is checking)."""
        seen: list[str] = []

        class _RecordingIAM(_IAM):
            def simulate_principal_policy(self, PolicySourceArn, ActionNames, **kw):
                seen.extend(ActionNames)
                return super().simulate_principal_policy(
                    PolicySourceArn, ActionNames, **kw
                )

        stub_aws(_Session(iam=_RecordingIAM()))
        res = _invoke(rc_yml)
        assert res.exit_code == 0, res.output
        assert any(a.startswith("elasticloadbalancing:") for a in seen)
        assert "ecs:RegisterTaskDefinition" in seen

    def test_missing_rc_yml_is_a_clear_error(self, tmp_path):
        res = CliRunner().invoke(cli, ["-c", str(tmp_path / "nope.yml"), "preflight"])
        assert res.exit_code == 1
        assert "rc.yml" in res.output
