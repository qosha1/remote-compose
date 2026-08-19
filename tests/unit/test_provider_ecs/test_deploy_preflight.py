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
        self.resource_arns: list = []

    def simulate_principal_policy(self, PolicySourceArn, ActionNames, **kwargs):
        if self.error:
            raise self.error
        self.simulated.extend(ActionNames)
        self.resource_arns.append(kwargs.get("ResourceArns"))
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


class TestActionMapStaysInSyncWithRenderedTerraform:
    """The map must cover what rc actually EMITS, not what its templates
    literally spell.

    The first version of this test scanned the .j2 files with the same regex
    the runtime scanner uses. That was wrong in a way that passed: one
    template writes `resource "aws_vpc_security_group_{{ rule.direction }}_rule"`,
    so the type name only exists after rendering, and the two SG-rule types
    never entered the compared set at all. The assertion held vacuously while
    a real, commonly-hit path went unchecked -- found in the field, not here
    (rc-zu1x). The runtime scanner was always correct; only the test was not.

    Rendering across the feature paths and scanning the .tf is the honest
    version.
    """

    def _render_all_paths(self, tmp_path):
        from remote_compose.provider import DeployContext, SecretRef, ServiceSpec
        from remote_compose.provider.ecs import ECSProvider

        env_dir = tmp_path / ".envs"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / ".prod").write_text("SECRET_KEY=x\n")
        ctx = DeployContext(
            project="cover",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={
                "version": 2,
                "project": "cover",
                "domain": "api.example.com",
                "tls": {"mode": "acm"},
                # Declared network exercises the interpolated SG-rule types,
                # NAT/route/endpoint resources and the declared-subnet path.
                "network": {
                    # egress: nat also pulls in the NAT gateway, EIP,
                    # route table and route resources.
                    "subnets": {"priv": {"public": False, "egress": "nat"}},
                    "security_groups": {
                        "mesh": {
                            "description": "Coverage fixture.",
                            "ingress": [{"from": "alb", "ports": [5432]}],
                            "egress": [{"to": "cidr:0.0.0.0/0"}],
                        }
                    },
                },
                "backup": {"bucket": "cover-backups"},
            },
            provider_config={
                "ecs": {
                    "region": "us-east-2",
                    "cluster": "cover",
                    "vpc_cidr": "10.0.0.0/16",
                    "default_launch_type": "EC2",
                    "ec2_capacity": {
                        "instance_type": "m5.xlarge",
                        "desired": 2,
                        "max": 4,
                    },
                }
            },
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services={
                "web": ServiceSpec(
                    name="web", cpu=256, memory=512, public=True, port=80
                ),
                "db": ServiceSpec(
                    name="db",
                    cpu=256,
                    memory=512,
                    volumes=[{"name": "data", "mount": "/var/lib/x"}],
                ),
            },
            secrets=[SecretRef(name="app", source="file", path=str(env_dir / ".prod"))],
        )
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        return pf.scan_terraform_dir(out)

    def test_every_rendered_resource_type_is_mapped(self, tmp_path):
        resources, data_sources = self._render_all_paths(tmp_path)
        assert resources - set(pf.RESOURCE_TYPE_ACTIONS) == set()
        assert data_sources - set(pf.DATA_SOURCE_ACTIONS) == set()

    def test_the_interpolated_sg_rule_types_are_actually_exercised(self, tmp_path):
        """Guard the guard: if this fixture stops rendering them, the test
        above goes vacuous again exactly as before."""
        resources, _ = self._render_all_paths(tmp_path)
        assert "aws_vpc_security_group_ingress_rule" in resources
        assert "aws_vpc_security_group_egress_rule" in resources

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
        # rc-hguq: without this rc cannot read awsvpcTrunking, silently sizes
        # as though trunking were off, and rejects a valid EC2 config for a
        # reason unrelated to the config.
        assert "ecs:ListAccountSettings" in actions

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
            is_deploy_principal=True,
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

    def test_all_allowed_against_the_deploy_principal_passes(self):
        check, missing = pf.check_iam_actions(
            _FakeSession(iam=_FakeIAM()),
            "arn:aws:iam::123456789012:role/rc-deploy",
            ["ecs:CreateCluster", "ecs:DeleteCluster"],
            principal_arn="arn:aws:iam::123456789012:role/rc-deploy",
            is_deploy_principal=True,
        )
        assert check.status == pf.OK and missing == []
        assert "the configured deploy principal" in check.detail

    def test_all_allowed_against_the_WRONG_principal_is_only_a_warning(self):
        """rc-zu1x: an admin laptop user passing everything says nothing
        about whether CI can deploy, and must not read as if it did."""
        check, missing = pf.check_iam_actions(
            _FakeSession(iam=_FakeIAM()),
            "arn:aws:iam::123456789012:user/qosha",
            ["ecs:CreateCluster"],
        )
        assert check.status == pf.WARN and missing == []
        assert "NOT a configured deploy role" in check.detail
        assert "says nothing about whether CI can deploy" in check.remedy


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
        assert "RemoteComposeDeploy" in message
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


class TestResourceScopedSimulation:
    """rc-zu1x third finding: simulating a resource-scoped policy against "*"
    reports denials that are not real.

    Verified live against a least-privileged deploy role (2026-08-19):
    iam:CreateRole, iam:CreateInstanceProfile, iam:AddRoleToInstanceProfile,
    iam:DeleteInstanceProfile, s3:PutObject and dynamodb:PutItem all returned
    implicitDeny against "*" and allowed against the concrete ARNs. The
    natural fix an operator reaches for on seeing those is widening the
    statements to Resource: "*" -- so a tool that reports them would actively
    push people from scoped policies toward admin-shaped ones.
    """

    BACKEND = dict(S3_BACKEND)

    def _arns(self):
        return pf.project_resource_arns(
            account_id="033937118837",
            region="us-west-2",
            project="debuggai-api",
            backend_cfg=self.BACKEND,
        )

    @pytest.mark.parametrize(
        "action,expected",
        [
            ("iam:CreateRole", "iam_role"),
            ("iam:PassRole", "iam_role"),
            ("iam:CreateInstanceProfile", "iam_instance_profile"),
            ("iam:AddRoleToInstanceProfile", "iam_instance_profile"),
            ("s3:PutObject", "s3_state"),
            ("dynamodb:PutItem", "dynamodb_lock"),
            # Wildcard-only actions must NOT be scoped: supplying a resource
            # makes them evaluate against a nonsense ARN and falsely deny.
            ("ecs:RegisterTaskDefinition", "wildcard"),
            ("ecr:GetAuthorizationToken", "wildcard"),
            ("ec2:DescribeVpcs", "wildcard"),
            ("iam:CreateServiceLinkedRole", "wildcard"),
            # Backup-bucket actions must not be scoped to the STATE bucket.
            ("s3:CreateBucket", "wildcard"),
            ("s3:PutBucketVersioning", "wildcard"),
        ],
    )
    def test_action_classification(self, action, expected):
        assert pf.classify_action(action) == expected

    def test_backend_actions_are_in_the_derived_set(self):
        """They come from no resource type -- the state bucket is not
        something the module creates -- so the first version omitted them
        even though every stateful deploy needs them."""
        actions, _ = pf.derive_required_actions(["aws_ecs_cluster"], [], self.BACKEND)
        assert "s3:PutObject" in actions
        assert "dynamodb:PutItem" in actions

    def test_no_backend_no_backend_actions(self):
        actions, _ = pf.derive_required_actions(
            ["aws_ecs_cluster"], [], {"type": "local"}
        )
        assert "s3:PutObject" not in actions

    def test_groups_carry_the_concrete_arns_rc_renders(self):
        arns = self._arns()
        assert arns["iam_instance_profile"] == [
            "arn:aws:iam::033937118837:instance-profile/debuggai-api-ec2-instance"
        ]
        assert "arn:aws:iam::033937118837:role/debuggai-api-task" in arns["iam_role"]
        assert "arn:aws:s3:::tf-state/app/prod.tfstate" in arns["s3_state"]
        assert arns["dynamodb_lock"] == [
            "arn:aws:dynamodb:us-west-2:033937118837:table/tf-locks"
        ]

    def test_each_group_is_simulated_against_its_own_resources(self):
        """One call per group -- ResourceArns applies to EVERY action in a
        call, so a mixed batch poisons the wildcard-only actions."""
        recorded: list[tuple] = []

        class _Recording:
            def simulate_principal_policy(self, PolicySourceArn, ActionNames, **kw):
                recorded.append((tuple(ActionNames), kw.get("ResourceArns")))
                return {
                    "EvaluationResults": [
                        {"EvalActionName": a, "EvalDecision": "allowed"}
                        for a in ActionNames
                    ]
                }

        actions = [
            "iam:CreateInstanceProfile",
            "ecs:RegisterTaskDefinition",
            "s3:PutObject",
        ]
        groups = pf.build_simulation_groups(actions, self._arns())
        pf.simulate_groups(_Recording(), "arn:aws:iam::1:role/r", groups)

        by_action = {a: res for batch, res in recorded for a in batch}
        assert by_action["ecs:RegisterTaskDefinition"] is None
        assert "instance-profile/debuggai-api-ec2-instance" in "".join(
            by_action["iam:CreateInstanceProfile"]
        )
        assert any("tfstate" in x for x in by_action["s3:PutObject"])

    def test_unscoped_denials_are_flagged_as_possible_false_negatives(self):
        """The report must not push an operator toward Resource: "*"."""
        iam = _FakeIAM(denied=["ec2:CreateVpc"])
        check, missing = pf.check_iam_actions(
            _FakeSession(iam=iam),
            "arn:aws:iam::033937118837:role/deploy",
            ["ec2:CreateVpc"],
            "us-west-2",
            principal_arn="arn:aws:iam::033937118837:role/deploy",
            is_deploy_principal=True,
            resource_arns=self._arns(),
        )
        assert check.status == pf.FAIL
        assert "ec2:CreateVpc" in missing
        assert 'checked against "*"' in check.remedy
        assert "before widening any statement" in check.remedy

    def test_scoped_denials_carry_no_false_negative_caveat(self):
        iam = _FakeIAM(denied=["iam:CreateInstanceProfile"])
        check, _ = pf.check_iam_actions(
            _FakeSession(iam=iam),
            "arn:aws:iam::033937118837:role/deploy",
            ["iam:CreateInstanceProfile"],
            "us-west-2",
            principal_arn="arn:aws:iam::033937118837:role/deploy",
            is_deploy_principal=True,
            resource_arns=self._arns(),
        )
        assert check.status == pf.FAIL
        assert 'checked against "*"' not in check.remedy


class TestPrincipalSelection:
    """rc-zu1x first finding: a green local run said nothing about CI."""

    def _session(self, iam=None):
        return _FakeSession(
            sts=_FakeSTS("arn:aws:iam::033937118837:user/qosha"),
            s3=_FakeS3(payload=json.dumps({"terraform_version": "1.0.0"}).encode()),
            dynamodb=_FakeDDB(),
            iam=iam or _FakeIAM(),
        )

    def test_defaults_to_the_caller_and_says_so_loudly(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        report = pf.run_preflight(
            tmp_path, S3_BACKEND, self._session(), project="debuggai-api"
        )
        check = next(c for c in report.checks if c.name == "deploy principal IAM")
        assert check.status == pf.WARN
        assert "NOT a configured deploy role" in check.detail
        assert report.checked_deploy_principal is False

    def test_simulates_the_configured_deploy_principal(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        iam = _FakeIAM()
        report = pf.run_preflight(
            tmp_path,
            S3_BACKEND,
            self._session(iam),
            region="us-west-2",
            project="debuggai-api",
            deploy_principal_arn=(
                "arn:aws:iam::033937118837:role/debuggai-api-prod-github-deploy"
            ),
        )
        check = next(c for c in report.checks if c.name == "deploy principal IAM")
        assert check.status == pf.OK
        assert "the configured deploy principal" in check.detail
        assert report.checked_deploy_principal is True
        assert report.checked_principal.endswith("debuggai-api-prod-github-deploy")

    def test_identity_line_names_both_identities(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        report = pf.run_preflight(
            tmp_path,
            S3_BACKEND,
            self._session(),
            project="p",
            deploy_principal_arn="arn:aws:iam::033937118837:role/ci",
        )
        identity = next(c for c in report.checks if c.name == "aws identity")
        assert "user/qosha" in identity.detail
        assert "simulating against" in identity.detail

    def test_unsimulatable_deploy_role_degrades_to_warn_not_fail(self, tmp_path):
        """A laptop that cannot simulate the CI role must not turn a working
        preflight red -- same degradation rc-g3jy already relies on."""
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        iam = _FakeIAM(error=_ClientError("AccessDenied"))
        report = pf.run_preflight(
            tmp_path,
            S3_BACKEND,
            self._session(iam),
            project="p",
            deploy_principal_arn="arn:aws:iam::033937118837:role/ci",
        )
        check = next(c for c in report.checks if c.name == "deploy principal IAM")
        assert check.status == pf.WARN
        assert report.ok is True


class TestFalseDenialsFromTheField:
    """rc-u0wr: preflight reported 7 FALSE denials on a COMPLETE deploy role.

    Since preflight blocks, a false positive is a hard blocker -- and the
    only workaround disabled the feature wholesale on the stack that most
    needed it, costing the 22 true findings too. Each case verified by hand
    with simulate-principal-policy before being fixed here.
    """

    BACKEND_CROSS_REGION = {
        "type": "s3",
        "bucket": "rc-tfstate",
        "key": "debuggai-api/ecs.tfstate",
        # The backend lives in a DIFFERENT region from the infrastructure.
        "region": "us-west-2",
        "dynamodb_table": "rc-tfstate-locks",
    }

    def test_lock_table_arn_uses_the_backend_region_not_the_infra_region(self):
        """#1: 4 dynamodb actions falsely denied. The report contradicted
        itself -- check_state_lock passed in the same run because it reads
        the backend region correctly."""
        arns = pf.project_resource_arns(
            account_id="033937118837",
            region="us-east-2",  # infra region
            project="debuggai-api",
            backend_cfg=self.BACKEND_CROSS_REGION,
        )
        assert arns["dynamodb_lock"] == [
            "arn:aws:dynamodb:us-west-2:033937118837:table/rc-tfstate-locks"
        ]

    def test_lock_arn_falls_back_to_infra_region_when_backend_omits_one(self):
        arns = pf.project_resource_arns(
            account_id="1",
            region="us-east-2",
            project="p",
            backend_cfg={"type": "s3", "bucket": "b", "dynamodb_table": "t"},
        )
        assert arns["dynamodb_lock"] == ["arn:aws:dynamodb:us-east-2:1:table/t"]

    def test_list_instance_profiles_for_role_acts_on_a_role(self):
        """#2: name contains 'InstanceProfile' but the resource is a ROLE.
        Verified live: allowed against role/..., implicitDeny against
        instance-profile/... . Inference got this wrong; the table doesn't."""
        assert pf.classify_action("iam:ListInstanceProfilesForRole") == "iam_role"

    @pytest.mark.parametrize(
        "action",
        [
            "iam:AddRoleToInstanceProfile",
            "iam:CreateInstanceProfile",
            "iam:DeleteInstanceProfile",
            "iam:GetInstanceProfile",
            "iam:RemoveRoleFromInstanceProfile",
            "iam:TagInstanceProfile",
        ],
    )
    def test_genuine_instance_profile_actions_stay_scoped_to_the_profile(self, action):
        assert pf.classify_action(action) == "iam_instance_profile"

    def test_every_scoped_iam_action_is_explicitly_classified(self):
        """Guard against the inference trap returning: any iam action rc
        scopes must appear in the explicit table or be a plain role action.

        The general rule this encodes: an action's resource type is NOT
        always the resource that made rc emit it.
        """
        actions, _ = pf.derive_required_actions(pf.RESOURCE_TYPE_ACTIONS.keys())
        for action in actions:
            if not action.startswith("iam:"):
                continue
            klass = pf.classify_action(action)
            if "InstanceProfile" in action:
                # Must have been decided explicitly, never by substring.
                assert action in pf._ACTION_RESOURCE_CLASS, action
            assert klass in ("iam_role", "iam_instance_profile", "wildcard")

    def test_destroy_only_actions_are_split_out_of_the_remedy(self):
        """#3: the paste-ready fragment granted route53:DeleteHostedZone on
        "*" to a CI role, to converge a stack that never deletes a zone --
        the exact blast-radius widening the resource-ARN work prevents."""
        report = pf.PreflightReport(
            missing_actions=[
                "ecs:CreateService",
                "route53:DeleteHostedZone",
                "ec2:DeleteVpc",
            ]
        )
        fragment = report.policy_fragment()
        converge, destroy = fragment.split("RemoteComposeDestroy")
        assert "ecs:CreateService" in converge
        assert "route53:DeleteHostedZone" not in converge
        assert "REVIEW BEFORE GRANTING" in fragment
        assert "route53:DeleteHostedZone" in destroy and "ec2:DeleteVpc" in destroy

    @pytest.mark.parametrize(
        "action",
        [
            # Ordinary update verbs -- terraform calls these to converge, so
            # withholding them would break a normal deploy.
            "ec2:RevokeSecurityGroupIngress",
            "iam:DetachRolePolicy",
            "ecs:DeregisterTaskDefinition",
            "ec2:DisassociateRouteTable",
            # Backend operations are never "destroy only".
            "s3:DeleteObject",
            "dynamodb:DeleteItem",
        ],
    )
    def test_update_verbs_are_not_treated_as_destructive(self, action):
        assert pf.is_destroy_only(action) is False

    def test_no_destructive_actions_means_a_single_statement(self):
        report = pf.PreflightReport(missing_actions=["ecs:CreateService"])
        assert "RemoteComposeDestroy" not in report.policy_fragment()


class TestReportProvenance:
    """The suggestion attached to rc-u0wr: naming the ARN per group and the
    origin per action would have made all three bugs self-evident from the
    output instead of requiring manual simulation to find."""

    def test_report_names_the_arn_used_for_each_group(self):
        iam = _FakeIAM(denied=["dynamodb:PutItem"])
        check, _ = pf.check_iam_actions(
            _FakeSession(iam=iam),
            "arn:aws:iam::1:role/deploy",
            ["dynamodb:PutItem"],
            "us-east-2",
            principal_arn="arn:aws:iam::1:role/deploy",
            is_deploy_principal=True,
            resource_arns=pf.project_resource_arns(
                account_id="1",
                region="us-east-2",
                project="p",
                backend_cfg=TestFalseDenialsFromTheField.BACKEND_CROSS_REGION,
            ),
        )
        # The wrong-region bug would be visible right here.
        assert "arn:aws:dynamodb:us-west-2:1:table/rc-tfstate-locks" in check.remedy

    def test_report_names_what_pulled_each_action_in(self):
        iam = _FakeIAM(denied=["route53:DeleteHostedZone"])
        check, _ = pf.check_iam_actions(
            _FakeSession(iam=iam),
            "arn:aws:iam::1:role/deploy",
            ["route53:DeleteHostedZone"],
            "us-east-2",
            principal_arn="arn:aws:iam::1:role/deploy",
            is_deploy_principal=True,
            action_provenance=pf.action_sources(
                ["aws_service_discovery_private_dns_namespace"]
            ),
        )
        assert "aws_service_discovery_private_dns_namespace" in check.remedy
        assert "[destroy/replace only]" in check.remedy

    def test_data_sources_contribute_read_actions_only(self):
        """A data source must never pull in a managed-resource action set."""
        for dtype, actions in pf.DATA_SOURCE_ACTIONS.items():
            for action in actions:
                assert not pf.is_destroy_only(action), f"{dtype}: {action}"
                verb = action.split(":", 1)[-1]
                assert verb.startswith(
                    ("Describe", "Get", "List")
                ), f"{dtype}: {action}"

    def test_data_source_origins_are_marked_as_data(self):
        sources = pf.action_sources([], ["aws_route53_zone"])
        assert sources["route53:GetHostedZone"] == ["data.aws_route53_zone"]

    def test_backend_and_preflight_origins_are_named(self):
        sources = pf.action_sources(
            [], [], TestFalseDenialsFromTheField.BACKEND_CROSS_REGION
        )
        assert sources["s3:PutObject"] == ["terraform s3 backend"]
        assert sources["dynamodb:PutItem"] == ["terraform state lock"]
        assert sources["ecs:ListAccountSettings"] == ["rc preflight"]


class TestAdvisoryMode:
    """RC_SKIP_PREFLIGHT=1 was the only way past a blocking finding, which
    turns the feature off on the stack that most needs it -- a false positive
    then costs the true findings too."""

    def _ctx(self, tmp_path):
        from remote_compose.provider import DeployContext, ServiceSpec

        return DeployContext(
            project="app",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={"ecs": {"region": "us-west-2", "cluster": "c"}},
            tf_backend_config=S3_BACKEND,
            working_dir=tmp_path,
            services={"api": ServiceSpec(name="api", cpu=256, memory=512)},
        )

    def _provider(self, session):
        from remote_compose.provider.ecs import ECSProvider

        return ECSProvider(session_factory=lambda _c: session)

    def _failing_session(self):
        return _FakeSession(
            sts=_FakeSTS(),
            s3=_FakeS3(error=_ClientError("AccessDenied")),
            dynamodb=_FakeDDB(),
            iam=_FakeIAM(),
        )

    def test_advisory_reports_everything_but_does_not_block(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RC_PREFLIGHT_ADVISORY", "1")
        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        emitted: list[str] = []
        from remote_compose.provider.ecs import ECSProvider

        provider = ECSProvider(
            session_factory=lambda _c: self._failing_session(),
            progress=emitted.append,
        )
        report = provider.deploy_preflight(self._ctx(tmp_path), tmp_path, force=True)
        assert report is not None and report.ok is False
        assert any("state backend" in line for line in emitted)
        assert any("RC_PREFLIGHT_ADVISORY" in line for line in emitted)

    def test_without_advisory_it_still_blocks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RC_PREFLIGHT_ADVISORY", raising=False)
        from remote_compose.provider.base import ProviderConfigError

        (tmp_path / "main.tf").write_text('resource "aws_ecs_cluster" "c" {}\n')
        with pytest.raises(ProviderConfigError) as exc:
            self._provider(self._failing_session()).deploy_preflight(
                self._ctx(tmp_path), tmp_path, force=True
            )
        assert "RC_PREFLIGHT_ADVISORY=1" in str(exc.value)
