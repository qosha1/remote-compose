"""ECSProvider auto-imports the container-insights log group when AWS
already owns it from a pre-terraform deploy.

Container Insights auto-creates /aws/ecs/containerinsights/<cluster>/performance
on first task launch. Stacks deployed before terraform managed that log group
hit ResourceAlreadyExistsException on the upgrade apply. The provider detects
the orphan via boto3 between init and apply and runs `terraform import` to
reconcile state.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock


from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import RecordingTerraformRunner, TerraformError


def _ctx(tmp_path: Path, cluster: str = "myapp-prod") -> DeployContext:
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": cluster,
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(name="api", cpu=256, memory=512, type="application"),
        },
        secrets=[],
    )


def _logs_session(log_groups: list[dict]):
    """A mock session whose logs.describe_log_groups returns log_groups."""
    sess = mock.MagicMock()
    logs = mock.MagicMock()
    logs.describe_log_groups.return_value = {"logGroups": log_groups}
    sess.client.return_value = logs
    return sess


def _provider(runner_holder: dict, session):
    def factory(out_dir: Path):
        if runner_holder.get("runner") is None:
            runner_holder["runner"] = RecordingTerraformRunner(out_dir)
        return runner_holder["runner"]

    return ECSProvider(
        runner_factory=factory,
        session_factory=lambda ctx: session,
    )


class TestOrphanLogGroupImport:
    def test_imports_when_aws_has_orphan(self, tmp_path):
        """boto3 reports the log group exists → terraform import runs."""
        sess = _logs_session(
            [
                {"logGroupName": "/aws/ecs/containerinsights/myapp-prod/performance"},
            ]
        )
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        provider.deploy(_ctx(tmp_path, cluster="myapp-prod"))

        runner = holder["runner"]
        subcmds = [c.args[0] for c in runner.calls]
        assert subcmds == ["init", "import", "apply", "output"]

        import_call = next(c for c in runner.calls if c.args[0] == "import")
        # last two args are the resource address + the log group id
        assert import_call.args[-2] == "aws_cloudwatch_log_group.container_insights"
        assert import_call.args[-1] == (
            "/aws/ecs/containerinsights/myapp-prod/performance"
        )

    def test_skips_import_when_aws_has_no_log_group(self, tmp_path):
        """No orphan in AWS → no import call, normal init→apply→output."""
        sess = _logs_session([])  # describe_log_groups returns no groups
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        provider.deploy(_ctx(tmp_path))

        subcmds = [c.args[0] for c in holder["runner"].calls]
        assert subcmds == ["init", "apply", "output"]

    def test_skips_import_when_describe_returns_unrelated_log_groups(self, tmp_path):
        """Prefix matches MUST be filtered to exact-name matches."""
        sess = _logs_session(
            [
                {
                    "logGroupName": "/aws/ecs/containerinsights/myapp-prod/performance-other"
                },
                {
                    "logGroupName": "/aws/ecs/containerinsights/myapp-prod/performance.bak"
                },
            ]
        )
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        provider.deploy(_ctx(tmp_path, cluster="myapp-prod"))

        subcmds = [c.args[0] for c in holder["runner"].calls]
        assert subcmds == ["init", "apply", "output"]

    def test_emits_recovered_followup_on_already_managed(self, tmp_path):
        """rc-b0d: when import fails because state already has the
        resource, emit a '✓ already in terraform state' follow-up so
        the user knows the prior raw 'Error: Resource already managed'
        stderr was handled cleanly."""
        sess = _logs_session(
            [
                {"logGroupName": "/aws/ecs/containerinsights/myapp-prod/performance"},
            ]
        )
        emitted: list[str] = []

        class _AlreadyManagedRunner(RecordingTerraformRunner):
            def import_resource(self, address, resource_id):
                self.calls.append(
                    type(self.calls[0])(args=["import", address, resource_id])
                    if self.calls
                    else None
                )
                raise TerraformError(
                    cmd=["terraform", "import", address, resource_id],
                    returncode=1,
                    stdout="",
                    stderr="Error: Resource already managed by Terraform",
                )

        runner = _AlreadyManagedRunner(tmp_path / "terraform")
        provider = ECSProvider(
            runner_factory=lambda out_dir: runner,
            session_factory=lambda ctx: sess,
            progress=emitted.append,
        )
        provider.deploy(_ctx(tmp_path, cluster="myapp-prod"))
        joined = "\n".join(emitted)
        assert "✓ orphan log group" in joined
        assert "already in terraform state" in joined
        assert "informational" in joined

    def test_swallows_already_managed_error(self, tmp_path):
        """If import fails because state already has it, do not crash deploy."""
        sess = _logs_session(
            [
                {"logGroupName": "/aws/ecs/containerinsights/myapp-prod/performance"},
            ]
        )
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        # Wrap factory so we can substitute a runner that errors on import.
        class _FailingImportRunner(RecordingTerraformRunner):
            def import_resource(self, address, resource_id):
                self.calls.append(  # record the attempt for assertion
                    type(self.calls[0])(args=["import", address, resource_id])
                    if self.calls
                    else None
                )
                raise TerraformError(
                    cmd=["terraform", "import", address, resource_id],
                    returncode=1,
                    stdout="",
                    stderr=(
                        "Error: Resource already managed by Terraform\n"
                        "Terraform is already managing a remote object for "
                        "aws_cloudwatch_log_group.container_insights"
                    ),
                )

        runner = _FailingImportRunner(tmp_path / "terraform")
        provider.runner_factory = lambda out_dir: runner
        # Should NOT raise.
        provider.deploy(_ctx(tmp_path, cluster="myapp-prod"))

    def test_swallows_is_already_managing_variant(self, tmp_path):
        """rc-e5u.37.5: terraform may emit 'is already managing' (verb form)
        WITHOUT the 'already managed' phrase if the user's tf version
        prints only the second sentence. Make sure that case is also
        recognized as 'already in state'."""
        sess = _logs_session(
            [
                {"logGroupName": "/aws/ecs/containerinsights/myapp-prod/performance"},
            ]
        )
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        class _VerbFormFailingRunner(RecordingTerraformRunner):
            def import_resource(self, address, resource_id):
                self.calls.append(
                    type(self.calls[0])(args=["import", address, resource_id])
                    if self.calls
                    else None
                )
                raise TerraformError(
                    cmd=["terraform", "import", address, resource_id],
                    returncode=1,
                    stdout="",
                    stderr=(
                        "Terraform is already managing a remote object for "
                        "aws_cloudwatch_log_group.container_insights. To "
                        "import to this address you must first remove the "
                        "existing object from the state."
                    ),
                )

        runner = _VerbFormFailingRunner(tmp_path / "terraform")
        provider.runner_factory = lambda out_dir: runner
        # Must NOT raise — already-in-state must be recognized via either phrase.
        provider.deploy(_ctx(tmp_path, cluster="myapp-prod"))

    def test_boto3_failure_does_not_crash_deploy(self, tmp_path):
        """If boto3 raises (creds missing, region unreachable), continue
        with normal apply — the user gets the original error path if AWS
        truly has an orphan."""
        sess = mock.MagicMock()
        logs = mock.MagicMock()
        logs.describe_log_groups.side_effect = RuntimeError("no creds")
        sess.client.return_value = logs

        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        # Deploy completes; sequence omits import.
        provider.deploy(_ctx(tmp_path))
        subcmds = [c.args[0] for c in holder["runner"].calls]
        assert subcmds == ["init", "apply", "output"]

    def test_default_cluster_name_used_when_unset(self, tmp_path):
        """If provider_config.ecs.cluster is unset, defaults to {project}-cluster."""
        sess = _logs_session(
            [
                {
                    "logGroupName": "/aws/ecs/containerinsights/myapp-cluster/performance"
                },
            ]
        )
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        ctx = _ctx(tmp_path)
        # Drop cluster so default kicks in.
        ctx.provider_config["ecs"].pop("cluster")

        provider.deploy(ctx)

        # Verify boto3 lookup used the defaulted name and import got it.
        describe = sess.client.return_value.describe_log_groups
        call_kwargs = describe.call_args.kwargs
        assert call_kwargs["logGroupNamePrefix"] == (
            "/aws/ecs/containerinsights/myapp-cluster/performance"
        )
        import_call = next(c for c in holder["runner"].calls if c.args[0] == "import")
        assert import_call.args[-1] == (
            "/aws/ecs/containerinsights/myapp-cluster/performance"
        )
