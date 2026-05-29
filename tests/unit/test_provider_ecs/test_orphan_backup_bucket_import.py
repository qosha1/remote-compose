"""rc-nae: ECSProvider auto-imports the backup S3 bucket when AWS already
owns it (user added backup.bucket to rc.yml after the deploy was up).

Without this, terraform apply fails with BucketAlreadyOwnedByYou. The
provider probes via boto3 head_bucket between init and apply and runs
'terraform import' to reconcile state. NO delete fallback — S3 deletion
risks data loss; if import fails, the apply is allowed to crash with
a clear next-step message.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock


from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import (
    RecordingTerraformRunner,
    TerraformError,
)


def _ctx(
    tmp_path: Path,
    bucket: str = "myapp-db-dumps",
    managed: bool = True,
    include_backup: bool = True,
) -> DeployContext:
    rc_yml: dict = {}
    if include_backup:
        rc_yml["backup"] = {
            "bucket": bucket,
            "service": "postgres",
        }
        if not managed:
            rc_yml["backup"]["bucket_managed"] = False
    return DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2=rc_yml,
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "myapp-prod",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(name="api", cpu=256, memory=512, type="application"),
            "postgres": ServiceSpec(
                name="postgres",
                cpu=256,
                memory=512,
                type="infrastructure",
            ),
        },
        secrets=[],
    )


def _s3_session(*, head_response="ok"):
    """Mock session whose s3.head_bucket either succeeds or raises.

    head_response can be:
      - 'ok': returns 200
      - 'not_found': raises 404
      - any other string: raises an exception with that string in repr
    """
    sess = mock.MagicMock()
    s3 = mock.MagicMock()
    if head_response == "ok":
        s3.head_bucket.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    elif head_response == "not_found":
        s3.head_bucket.side_effect = RuntimeError(
            "An error occurred (404) when calling the HeadBucket operation: Not Found"
        )
    else:
        s3.head_bucket.side_effect = RuntimeError(head_response)
    sess.client.return_value = s3
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


class TestOrphanBackupBucketImport:
    def test_imports_when_bucket_exists(self, tmp_path):
        sess = _s3_session(head_response="ok")
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)
        provider.deploy(_ctx(tmp_path))

        runner = holder["runner"]
        subcmds = [c.args[0] for c in runner.calls]
        # init → import (log group? no, just S3) → apply → output. With
        # no log group orphan, the only import is the S3 bucket.
        assert "import" in subcmds
        # Find the import call and confirm address + id.
        import_call = next(c for c in runner.calls if c.args[0] == "import")
        assert import_call.args[-2] == "aws_s3_bucket.backups"
        assert import_call.args[-1] == "myapp-db-dumps"

    def test_skips_when_bucket_does_not_exist(self, tmp_path):
        sess = _s3_session(head_response="not_found")
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)
        provider.deploy(_ctx(tmp_path))

        subcmds = [c.args[0] for c in holder["runner"].calls]
        # No import — terraform will create the bucket fresh.
        assert subcmds == ["init", "apply", "output"]

    def test_skips_when_no_backup_block_in_rc_yml(self, tmp_path):
        sess = _s3_session(head_response="ok")
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)
        provider.deploy(_ctx(tmp_path, include_backup=False))

        subcmds = [c.args[0] for c in holder["runner"].calls]
        assert subcmds == ["init", "apply", "output"]

    def test_skips_when_bucket_managed_false(self, tmp_path):
        # User opted out of terraform managing the bucket — don't import.
        sess = _s3_session(head_response="ok")
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)
        provider.deploy(_ctx(tmp_path, managed=False))

        subcmds = [c.args[0] for c in holder["runner"].calls]
        assert subcmds == ["init", "apply", "output"]

    def test_emits_recovered_followup_on_already_managed(self, tmp_path):
        """rc-b0d: surface a '✓ already in state' follow-up when the
        import recovery fires, so the prior raw stderr isn't alarming."""
        sess = _s3_session(head_response="ok")
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
        provider.deploy(_ctx(tmp_path))
        joined = "\n".join(emitted)
        assert "✓ backup bucket" in joined
        assert "already in terraform state" in joined
        assert "informational" in joined

    def test_swallows_already_managed_error(self, tmp_path):
        """If import fails because state already has it, do not crash."""
        sess = _s3_session(head_response="ok")
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)

        class _FailingImportRunner(RecordingTerraformRunner):
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
                    stderr=("Error: Resource already managed by Terraform"),
                )

        runner = _FailingImportRunner(tmp_path / "terraform")
        provider.runner_factory = lambda out_dir: runner
        provider.deploy(_ctx(tmp_path))  # Must not raise.

    def test_boto3_failure_does_not_crash_deploy(self, tmp_path):
        """If boto3 raises a non-404 (creds, perms, throttling), continue
        with the apply — user gets a warning."""
        sess = _s3_session(head_response="permission denied")
        holder: dict = {"runner": None}
        provider = _provider(holder, sess)
        provider.deploy(_ctx(tmp_path))
        # Deploy completes; sequence omits import.
        subcmds = [c.args[0] for c in holder["runner"].calls]
        assert subcmds == ["init", "apply", "output"]
