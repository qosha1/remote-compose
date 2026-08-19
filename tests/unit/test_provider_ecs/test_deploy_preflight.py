"""rc-g3jy: report every missing deploy prerequisite at once.

Moving debuggai-api-prod off --no-state onto full terraform cost three failed
production deploys in a row -- no terraform binary, an S3 403 on state, an
unresolvable aws_profile -- each discovered one deploy at a time. Diffing the
deploy role against a working stack then turned up 36 MORE missing IAM
actions, every one of which would have been another serial failure.

The load-bearing property under test is completeness: preflight must run all
its checks and report everything, never stop at the first failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from remote_compose.provider.ecs import deploy_preflight as pf


class _Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeSession:
    """Minimal boto3.Session stand-in with per-service scripted behavior."""

    def __init__(self, **clients):
        self._clients = clients

    def client(self, name, region_name=None):
        if name not in self._clients:
            raise AssertionError(f"unexpected client: {name}")
        client = self._clients[name]
        if isinstance(client, Exception):
            raise client
        return client


class _FakeS3:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def get_object(self, Bucket, Key):
        if self.error:
            raise self.error
        return {"Body": _Body(self.payload)}


class _FakeDDB:
    def __init__(self, held=None, put_error=None, delete_error=None, get_error=None):
        self.held = held
        self.put_error = put_error
        self.delete_error = delete_error
        self.get_error = get_error
        self.calls: list[tuple] = []

    def get_item(self, TableName, Key):
        self.calls.append(("get", Key["LockID"]["S"]))
        if self.get_error:
            raise self.get_error
        return {"Item": self.held} if self.held else {}

    def put_item(self, TableName, Item):
        self.calls.append(("put", Item["LockID"]["S"]))
        if self.put_error:
            raise self.put_error

    def delete_item(self, TableName, Key):
        self.calls.append(("delete", Key["LockID"]["S"]))
        if self.delete_error:
            raise self.delete_error


class _FakeIAM:
    def __init__(self, denied=(), error=None):
        self.denied = set(denied)
        self.error = error
        self.simulated: list[str] = []

    def simulate_principal_policy(self, PolicySourceArn, ActionNames):
        if self.error:
            raise self.error
        self.simulated.extend(ActionNames)
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": a,
                    "EvalDecision": ("implicitDeny" if a in self.denied else "allowed"),
                }
                for a in ActionNames
            ]
        }


class _FakeSTS:
    def __init__(self, arn="arn:aws:sts::123456789012:assumed-role/rc-deploy/gh"):
        self.arn = arn

    def get_caller_identity(self):
        if isinstance(self.arn, Exception):
            raise self.arn
        return {"Arn": self.arn}


S3_BACKEND = {
    "type": "s3",
    "bucket": "tf-state",
    "key": "app/prod.tfstate",
    "region": "us-west-2",
    "dynamodb_table": "tf-locks",
}


class TestActionMapStaysInSyncWithTemplates:
    def test_every_emitted_resource_type_is_mapped(self):
        """A template that grows a resource type rc can't check silently
        reopens the exact gap this feature closes."""
        tpl_dir = Path(pf.__file__).parent / "templates"
        emitted: set[str] = set()
        data: set[str] = set()
        for f in sorted(tpl_dir.glob("*.j2")):
            text = f.read_text()
            emitted.update(re.findall(r'^resource\s+"([a-z0-9_]+)"', text, re.M))
            data.update(re.findall(r'^data\s+"([a-z0-9_]+)"', text, re.M))
        assert emitted - set(pf.RESOURCE_TYPE_ACTIONS) == set()
        assert data - set(pf.DATA_SOURCE_ACTIONS) == set()

    def test_every_mapped_action_is_service_qualified(self):
        for rtype, actions in pf.RESOURCE_TYPE_ACTIONS.items():
            for action in actions:
                assert ":" in action, f"{rtype}: {action}"


class TestDeriveRequiredActions:
    def test_covers_the_services_the_missing_role_lacked(self):
        """route53, servicediscovery, elasticloadbalancing, ec2 describes,
        iam role management, logs tagging -- the 36 that were found by hand."""
        actions, unmodeled = pf.derive_required_actions(
            pf.RESOURCE_TYPE_ACTIONS.keys(), pf.DATA_SOURCE_ACTIONS.keys()
        )
        assert unmodeled == []
        grouped = pf.group_by_service(actions)
        for service in (
            "route53",
            "servicediscovery",
            "elasticloadbalancing",
            "ec2",
            "iam",
            "logs",
        ):
            assert service in grouped, service
        assert "logs:TagResource" in grouped["logs"]

    def test_unmapped_type_is_reported_not_silently_passed(self):
        actions, unmodeled = pf.derive_required_actions(
            ["aws_ecs_cluster", "aws_totally_new_thing"]
        )
        assert unmodeled == ["aws_totally_new_thing"]
        assert "ecs:CreateCluster" in actions

    def test_baseline_actions_always_present(self):
        actions, _ = pf.derive_required_actions([])
        assert "sts:GetCallerIdentity" in actions

    def test_scan_reads_emitted_hcl(self, tmp_path):
        (tmp_path / "a.tf").write_text(
            'resource "aws_ecs_cluster" "main" {}\n'
            'data "aws_route53_zone" "z" {}\n'
            '  resource "not_top_level" "x" {}\n'
        )
        resources, data = pf.scan_terraform_dir(tmp_path)
        assert resources == {"aws_ecs_cluster"}
        assert data == {"aws_route53_zone"}


class TestCanonicalPrincipalArn:
    def test_assumed_role_session_becomes_the_role(self):
        """The OIDC-runner case: pass the session ARN through unchanged and
        the simulation fails on EVERY run."""
        assert (
            pf.canonical_principal_arn(
                "arn:aws:sts::123456789012:assumed-role/rc-deploy/GitHubActions"
            )
            == "arn:aws:iam::123456789012:role/rc-deploy"
        )

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:iam::123456789012:user/quinn",
            "arn:aws:iam::123456789012:role/rc-deploy",
        ],
    )
    def test_iam_entity_arns_pass_through(self, arn):
        assert pf.canonical_principal_arn(arn) == arn

    @pytest.mark.parametrize(
        "arn", ["arn:aws:iam::123456789012:root", "", "garbage", None]
    )
    def test_unsimulatable_principals_return_none(self, arn):
        assert pf.canonical_principal_arn(arn or "") is None


class TestStateBackendCheck:
    def test_403_is_reported_as_access_denied_not_missing(self):
        session = _FakeSession(s3=_FakeS3(error=_ClientError("AccessDenied")))
        check = pf.check_state_backend(session, S3_BACKEND, (1, 15, 5))
        assert check.status == pf.FAIL
        assert "access denied" in check.detail
        assert "s3:GetObject" in check.remedy

    def test_absent_state_is_a_first_apply_not_a_failure(self):
        session = _FakeSession(s3=_FakeS3(error=_ClientError("NoSuchKey")))
        check = pf.check_state_backend(session, S3_BACKEND, (1, 15, 5))
        assert check.status == pf.OK
        assert "does not exist yet" in check.detail

    def test_state_written_by_a_newer_terraform_is_blocking(self):
        """Copying another repo's pinned 1.9.8 onto state written by 1.15.5
        would have failed every deploy."""
        session = _FakeSession(
            s3=_FakeS3(payload=json.dumps({"terraform_version": "1.15.5"}).encode())
        )
        check = pf.check_state_backend(session, S3_BACKEND, (1, 9, 8))
        assert check.status == pf.FAIL
        assert "1.15.5" in check.detail and "1.9.8" in check.detail
        assert "newer than current" in check.remedy

    def test_matching_versions_pass(self):
        session = _FakeSession(
            s3=_FakeS3(payload=json.dumps({"terraform_version": "1.9.8"}).encode())
        )
        check = pf.check_state_backend(session, S3_BACKEND, (1, 15, 5))
        assert check.status == pf.OK

    def test_non_s3_backend_is_skipped(self):
        check = pf.check_state_backend(_FakeSession(), {"type": "local"}, (1, 15, 5))
        assert check.status == pf.SKIP


class TestStateLockCheck:
    def test_acquire_and_release_verified_without_touching_the_real_lock(self):
        ddb = _FakeDDB()
        check = pf.check_state_lock(_FakeSession(dynamodb=ddb), S3_BACKEND)
        assert check.status == pf.OK
        written = [lock for op, lock in ddb.calls if op in ("put", "delete")]
        assert all(lock.endswith("-rc-preflight-probe") for lock in written)
        # The real LockID is only ever READ.
        assert ("get", "tf-state/app/prod.tfstate") in ddb.calls

    def test_held_lock_is_reported_and_never_broken(self):
        ddb = _FakeDDB(held={"Info": {"S": "someone else's apply"}})
        check = pf.check_state_lock(_FakeSession(dynamodb=ddb), S3_BACKEND)
        assert check.status == pf.WARN
        assert "someone else's apply" in check.remedy
        assert not [op for op, _ in ddb.calls if op == "delete"]

    def test_denied_write_is_blocking(self):
        ddb = _FakeDDB(put_error=_ClientError("AccessDeniedException"))
        check = pf.check_state_lock(_FakeSession(dynamodb=ddb), S3_BACKEND)
        assert check.status == pf.FAIL
        assert "dynamodb:PutItem" in check.remedy

    def test_write_without_delete_is_flagged(self):
        ddb = _FakeDDB(delete_error=_ClientError("AccessDeniedException"))
        check = pf.check_state_lock(_FakeSession(dynamodb=ddb), S3_BACKEND)
        assert check.status == pf.WARN
        assert "never release" in check.remedy

    def test_no_lock_table_is_skipped(self):
        backend = dict(S3_BACKEND)
        backend.pop("dynamodb_table")
        check = pf.check_state_lock(_FakeSession(), backend)
        assert check.status == pf.SKIP


class TestIamCheck:
    def test_reports_every_denied_action_grouped_by_service(self):
        denied = ["route53:ChangeResourceRecordSets", "logs:TagResource"]
        iam = _FakeIAM(denied=denied)
        actions, _ = pf.derive_required_actions(pf.RESOURCE_TYPE_ACTIONS.keys())
        check, missing = pf.check_iam_actions(
            _FakeSession(iam=iam),
            "arn:aws:sts::123456789012:assumed-role/rc-deploy/gh",
            actions,
        )
        assert check.status == pf.FAIL
        assert set(missing) == set(denied)
        assert "logs (1)" in check.detail and "route53 (1)" in check.detail
        # Simulated against the ROLE, not the session ARN.
        assert len(iam.simulated) == len(actions)

    def test_missing_simulate_permission_is_could_not_check_not_a_pass(self):
        iam = _FakeIAM(error=_ClientError("AccessDenied"))
        check, missing = pf.check_iam_actions(
            _FakeSession(iam=iam),
            "arn:aws:iam::123456789012:role/rc-deploy",
            ["ecs:CreateCluster"],
        )
        assert check.status == pf.WARN
        assert "could not simulate" in check.detail
        assert "not evidence that anything is wrong" in check.remedy
        assert missing == []

    def test_all_allowed_passes(self):
        check, missing = pf.check_iam_actions(
            _FakeSession(iam=_FakeIAM()),
            "arn:aws:iam::123456789012:role/rc-deploy",
            ["ecs:CreateCluster", "ecs:DeleteCluster"],
        )
        assert check.status == pf.OK and missing == []


class TestRunPreflightCompleteness:
    def _session(self, **overrides):
        clients = {
            "sts": _FakeSTS(),
            "s3": _FakeS3(error=_ClientError("AccessDenied")),
            "dynamodb": _FakeDDB(put_error=_ClientError("AccessDeniedException")),
            "iam": _FakeIAM(denied=["route53:ChangeResourceRecordSets"]),
        }
        clients.update(overrides)
        return _FakeSession(**clients)

    def test_reports_all_failures_not_just_the_first(self, tmp_path):
        """The whole point: one round of fixes, not one per failed deploy."""
        (tmp_path / "main.tf").write_text('resource "aws_route53_record" "r" {}\n')
        report = pf.run_preflight(tmp_path, S3_BACKEND, self._session())

        failed = {c.name for c in report.checks if c.status == pf.FAIL}
        assert {"state backend", "state lock", "deploy principal IAM"} <= failed
        assert report.ok is False

    def test_emits_a_paste_ready_policy_fragment(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_route53_record" "r" {}\n')
        report = pf.run_preflight(tmp_path, S3_BACKEND, self._session())
        fragment = json.loads(report.policy_fragment())
        assert fragment["Effect"] == "Allow"
        assert "route53:ChangeResourceRecordSets" in fragment["Action"]

    def test_unmodeled_types_surface_as_a_warning(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_brand_new" "x" {}\n')
        report = pf.run_preflight(tmp_path, S3_BACKEND, self._session())
        assert report.unmodeled_resource_types == ["aws_brand_new"]
        assert any(c.name == "action coverage" for c in report.checks)

    def test_broken_credentials_fail_loudly(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        session = self._session(sts=_FakeSTS(arn=RuntimeError("no creds")))
        report = pf.run_preflight(tmp_path, S3_BACKEND, session)
        identity = next(c for c in report.checks if c.name == "aws identity")
        assert identity.status == pf.FAIL
        # And the IAM check does not pretend to have run.
        iam = next(c for c in report.checks if c.name == "deploy principal IAM")
        assert iam.status == pf.SKIP

    def test_clean_environment_passes(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        session = self._session(
            s3=_FakeS3(payload=json.dumps({"terraform_version": "1.0.0"}).encode()),
            dynamodb=_FakeDDB(),
            iam=_FakeIAM(),
        )
        report = pf.run_preflight(tmp_path, S3_BACKEND, session)
        assert report.ok is True
        assert report.policy_fragment() == ""

    def test_render_table_lists_every_check(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        report = pf.run_preflight(tmp_path, S3_BACKEND, self._session())
        table = report.render_table()
        for check in report.checks:
            assert check.name in table


class TestProviderWiring:
    """deploy_preflight() on the provider: when it runs, and what it does."""

    def _ctx(self, tmp_path, backend, **kw):
        from remote_compose.provider import DeployContext, ServiceSpec

        ctx = DeployContext(
            project="app",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={"ecs": {"region": "us-west-2", "cluster": "c"}},
            tf_backend_config=backend,
            working_dir=tmp_path,
            services={"api": ServiceSpec(name="api", cpu=256, memory=512)},
        )
        for key, value in kw.items():
            setattr(ctx, key, value)
        return ctx

    def _provider(self, session):
        from remote_compose.provider.ecs import ECSProvider

        return ECSProvider(session_factory=lambda _ctx: session)

    def test_local_backend_does_not_auto_run(self, tmp_path):
        """A local-backend stack has no state bucket or lock table to check."""
        provider = self._provider(_FakeSession())
        assert (
            provider.deploy_preflight(self._ctx(tmp_path, {"type": "local"}), tmp_path)
            is None
        )

    def test_no_state_deploy_does_not_run(self, tmp_path):
        provider = self._provider(_FakeSession())
        ctx = self._ctx(tmp_path, S3_BACKEND, skip_terraform=True)
        assert provider.deploy_preflight(ctx, tmp_path) is None

    def test_env_escape_hatch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RC_SKIP_PREFLIGHT", "1")
        provider = self._provider(_FakeSession())
        assert (
            provider.deploy_preflight(self._ctx(tmp_path, S3_BACKEND), tmp_path) is None
        )

    def test_blocking_findings_raise_with_the_full_report(self, tmp_path):
        from remote_compose.provider.base import ProviderConfigError

        (tmp_path / "main.tf").write_text('resource "aws_route53_record" "r" {}\n')
        session = _FakeSession(
            sts=_FakeSTS(),
            s3=_FakeS3(error=_ClientError("AccessDenied")),
            dynamodb=_FakeDDB(),
            iam=_FakeIAM(denied=["route53:ChangeResourceRecordSets"]),
        )
        with pytest.raises(ProviderConfigError) as exc:
            self._provider(session).deploy_preflight(
                self._ctx(tmp_path, S3_BACKEND), tmp_path, force=True
            )
        message = str(exc.value)
        # Both failures, not just the first.
        assert "state backend" in message
        assert "route53:ChangeResourceRecordSets" in message
        assert "RemoteComposeDeployMissingActions" in message
        assert "RC_SKIP_PREFLIGHT=1" in message

    def test_a_broken_checker_never_breaks_the_deploy(self, tmp_path):
        """A preflight that grounds a working deploy is worse than none."""
        from remote_compose.provider.ecs import ECSProvider

        def _boom(_ctx):
            raise RuntimeError("boto3 exploded")

        provider = ECSProvider(session_factory=_boom)
        assert (
            provider.deploy_preflight(self._ctx(tmp_path, S3_BACKEND), tmp_path) is None
        )
        assert any("could not run" in w for w in provider._warnings)

    def test_clean_report_returns_and_does_not_raise(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        session = _FakeSession(
            sts=_FakeSTS(),
            s3=_FakeS3(payload=json.dumps({"terraform_version": "1.0.0"}).encode()),
            dynamodb=_FakeDDB(),
            iam=_FakeIAM(),
        )
        report = self._provider(session).deploy_preflight(
            self._ctx(tmp_path, S3_BACKEND), tmp_path
        )
        assert report is not None and report.ok is True
