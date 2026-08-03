"""
Unit tests for DevHostService — orchestrates EC2 dev-host lifecycle.

TDD red phase for [rc dev 2.1] (rc-ejl). Tests assert the contract that
implementation in [rc dev 4.1] (rc-z7p) must satisfy.

DevHostService is responsible for:
  - create / list / get / stop / start / destroy of dev hosts
  - state file management (.rc/dev-hosts.yml)
  - SSH keypair generation and storage via CredentialService
  - Terraform invocation for AWS resource lifecycle
  - Tagging all AWS resources for orphan detection
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_credential_service():
    svc = MagicMock()
    svc.store_ssh_keypair.return_value = MagicMock(id=42, name="dev-host-alice-key")
    svc.get_ssh_keypair.return_value = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n",
        "ssh-ed25519 AAAA fake",
    )
    return svc


@pytest.fixture
def mock_terraform_runner():
    runner = MagicMock()
    runner.apply.return_value = {
        "instance_id": "i-0123456789abcdef0",
        "public_ip": "203.0.113.42",
        "public_dns": "ec2-203-0-113-42.compute.amazonaws.com",
    }
    runner.destroy.return_value = None
    return runner


@pytest.fixture
def mock_aws_factory():
    factory = MagicMock()
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-0123456789abcdef0",
                        "State": {"Name": "running"},
                        "PublicIpAddress": "203.0.113.42",
                        "Tags": [
                            {"Key": "DevHost", "Value": "alice"},
                            {"Key": "ManagedBy", "Value": "rc-dev"},
                        ],
                    }
                ]
            }
        ]
    }
    factory.get_client.return_value = ec2
    return factory


@pytest.fixture
def service(mock_credential_service, mock_terraform_runner, mock_aws_factory, tmp_path):
    from remote_compose.dev_host.service import DevHostService

    return DevHostService(
        credential_service=mock_credential_service,
        terraform_runner=mock_terraform_runner,
        aws_client_factory=mock_aws_factory,
        state_path=tmp_path / "dev-hosts.yml",
    )


@pytest.fixture
def git_source():
    from remote_compose.dev_host.bootstrap import GitSource

    return GitSource(url="https://github.com/owner/repo.git", ref="main")


class TestCreateHost:
    def test_create_host_returns_record_with_aws_outputs(
        self, service, git_source, mock_terraform_runner
    ):
        record = service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        assert record.name == "alice"
        assert record.instance_type == "t4g.medium"
        assert record.region == "us-west-1"
        assert record.instance_id == "i-0123456789abcdef0"
        assert record.public_ip == "203.0.113.42"
        assert record.status in ("running", "pending")
        mock_terraform_runner.apply.assert_called_once()

    def test_create_host_passes_tags_to_terraform(
        self, service, git_source, mock_terraform_runner
    ):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        call_kwargs = mock_terraform_runner.apply.call_args.kwargs
        # the tf apply must include tags identifying the dev host
        tags = call_kwargs.get("tags") or call_kwargs.get("variables", {}).get(
            "tags", {}
        )
        assert tags.get("DevHost") == "alice"
        assert tags.get("ManagedBy") == "rc-dev"

    def test_create_host_generates_and_stores_ssh_keypair(
        self, service, git_source, mock_credential_service
    ):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        mock_credential_service.store_ssh_keypair.assert_called_once()

    def test_create_host_writes_to_state_file(self, service, git_source, tmp_path):
        import yaml

        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        state_file = tmp_path / "dev-hosts.yml"
        assert state_file.exists()
        loaded = yaml.safe_load(state_file.read_text())
        assert "alice" in loaded["hosts"]

    def test_create_host_rejects_duplicate_name(self, service, git_source):
        from remote_compose.exceptions import ValidationError

        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        with pytest.raises(ValidationError):
            service.create_host(
                name="alice",
                source=git_source,
                instance_type="t4g.medium",
                region="us-west-1",
            )

    def test_create_host_rejects_unknown_instance_type(self, service, git_source):
        from remote_compose.exceptions import ValidationError

        with pytest.raises(ValidationError):
            service.create_host(
                name="alice",
                source=git_source,
                instance_type="bogus.99xlarge",
                region="us-west-1",
            )

    def test_create_host_rolls_back_on_terraform_apply_failure(
        self, service, git_source, mock_terraform_runner, tmp_path
    ):
        """rc-xiq: partial terraform failure (e.g. EIP quota) MUST trigger
        terraform destroy in the same working dir so we don't leak AWS
        resources. Failed creates must NOT pollute the registry."""
        import yaml
        from remote_compose.terraform.runner import TerraformError

        mock_terraform_runner.apply.side_effect = TerraformError(
            cmd=["terraform", "apply"],
            returncode=1,
            stdout="",
            stderr="Error: AddressLimitExceeded",
        )

        with pytest.raises(TerraformError) as exc_info:
            service.create_host(
                name="alice",
                source=git_source,
                instance_type="t4g.medium",
                region="us-west-1",
            )

        # Original exception is re-raised, not wrapped
        assert "AddressLimitExceeded" in str(exc_info.value)
        # Rollback was attempted
        mock_terraform_runner.destroy.assert_called_once()
        # Registry NOT polluted by the failed attempt
        state_file = tmp_path / "dev-hosts.yml"
        if state_file.exists():
            loaded = yaml.safe_load(state_file.read_text()) or {}
            assert "alice" not in (loaded.get("hosts") or {})

    def test_create_host_when_rollback_fails_writes_failed_marker(
        self, service, git_source, mock_terraform_runner, tmp_path
    ):
        """If terraform destroy also fails after a failed apply, write a
        'failed' registry entry so the user can retry rc dev destroy later
        instead of having an invisible orphan."""
        import yaml
        from remote_compose.terraform.runner import TerraformError

        mock_terraform_runner.apply.side_effect = TerraformError(
            cmd=["terraform", "apply"],
            returncode=1,
            stdout="",
            stderr="apply fail",
        )
        mock_terraform_runner.destroy.side_effect = TerraformError(
            cmd=["terraform", "destroy"],
            returncode=1,
            stdout="",
            stderr="destroy fail too",
        )

        with pytest.raises(TerraformError):
            service.create_host(
                name="alice",
                source=git_source,
                instance_type="t4g.medium",
                region="us-west-1",
            )

        state_file = tmp_path / "dev-hosts.yml"
        assert state_file.exists()
        loaded = yaml.safe_load(state_file.read_text()) or {}
        entry = (loaded.get("hosts") or {}).get("alice")
        assert entry is not None
        assert entry["status"] == "failed"
        assert "apply fail" in entry.get("failure_reason", "")


class TestListHosts:
    def test_list_empty(self, service):
        assert service.list_hosts() == []

    def test_list_after_create(self, service, git_source):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        service.create_host(
            name="bob",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        hosts = service.list_hosts()
        names = sorted(h.name for h in hosts)
        assert names == ["alice", "bob"]


class TestReconcileLive:
    """rc-n14: rc dev list must reflect live AWS (tag ManagedBy=rc-dev),
    independent of the cwd-relative state file, and flag drift."""

    def test_untracked_aws_box_surfaced(self, service):
        # State file is empty, but AWS has a tagged box (the mock_aws_factory
        # returns 'alice' running). It MUST show up — this is the billing leak
        # case `rc dev list` silently hid.
        records = service.reconcile_live(["us-west-1"])
        by_name = {r.name: r for r in records}
        assert "alice" in by_name
        assert by_name["alice"].status == "running"
        assert by_name["alice"].public_ip == "203.0.113.42"
        assert by_name["alice"].tracked is False  # in AWS, not in local state

    def test_tracked_box_marked_and_live_status(self, service, git_source):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        records = service.reconcile_live(["us-west-1"])
        alice = {r.name: r for r in records}["alice"]
        assert alice.tracked is True  # present in local state
        assert alice.status == "running"  # live AWS status, not frozen

    def test_stale_state_box_flagged_gone(self, service, git_source):
        # A host in the state file that AWS no longer has (destroyed out-of-band)
        # must be flagged, not silently shown as running.
        service.create_host(
            name="ghost",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        # mock AWS returns only 'alice', never 'ghost'
        records = service.reconcile_live(["us-west-1"])
        ghost = {r.name: r for r in records}.get("ghost")
        assert ghost is not None
        assert ghost.status in ("gone", "missing", "terminated")


class TestGetHost:
    def test_get_existing(self, service, git_source):
        created = service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        fetched = service.get_host("alice")
        assert fetched.name == created.name
        assert fetched.instance_id == created.instance_id

    def test_get_missing_raises(self, service):
        from remote_compose.exceptions import ValidationError

        with pytest.raises(ValidationError):
            service.get_host("ghost")


class TestDestroyHost:
    def test_destroy_calls_terraform_destroy(
        self, service, git_source, mock_terraform_runner
    ):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        service.destroy_host("alice")

        mock_terraform_runner.destroy.assert_called_once()

    def test_destroy_removes_from_state(self, service, git_source, tmp_path):
        import yaml

        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        service.destroy_host("alice")

        loaded = yaml.safe_load((tmp_path / "dev-hosts.yml").read_text())
        assert "alice" not in (loaded.get("hosts") or {})

    def test_destroy_missing_no_force_raises(self, service):
        from remote_compose.exceptions import ValidationError

        with pytest.raises(ValidationError):
            service.destroy_host("ghost")

    def test_destroy_missing_with_force_still_tears_down(
        self, service, mock_terraform_runner
    ):
        """--force is documented as "tear down even if not in state".

        It used to `return` on exactly that path, doing NOTHING while the CLI
        printed "✓ destroyed". Because dev-host state is per-directory, running
        destroy from anywhere but the dir that ran `up` hit this every time, and
        the host's Elastic IP was abandoned to bill indefinitely. It must still
        not raise, but it must actually attempt the teardown.
        """
        service.destroy_host("ghost", force=True)

        mock_terraform_runner.destroy.assert_called_once()


class TestConcurrentCreateHostDoesNotLoseEntries:
    """create_host used to load `hosts` once at the top of the method,
    before the (multi-minute) terraform apply, then save that same
    by-then-stale snapshot at the end — every save overwrote the WHOLE
    file with whatever was in memory. A second create_host finishing its
    entire lifecycle while the first's apply was still in flight got
    silently dropped by the first's eventual save. Hit for real:
    provisioning 3 boxes in parallel lost 2 of them from local state (they
    were fine in AWS; only local bookkeeping broke, but rc dev
    attach/ssh/stop/start/destroy all key off this file).
    """

    def test_second_create_finishing_mid_apply_is_not_lost(
        self, mock_credential_service, mock_aws_factory, tmp_path
    ):
        from remote_compose.dev_host.bootstrap import GitSource
        from remote_compose.dev_host.service import DevHostService

        service = DevHostService(
            credential_service=mock_credential_service,
            terraform_runner=None,
            aws_client_factory=mock_aws_factory,
            state_path=tmp_path / "dev-hosts.yml",
        )

        alice_runner = MagicMock()
        bob_runner = MagicMock()
        bob_runner.apply.return_value = {
            "instance_id": "i-bob",
            "public_ip": "203.0.113.20",
            "public_dns": "ec2-203-0-113-20.compute.amazonaws.com",
        }

        def alice_apply_side_effect(*_a, **_k):
            # Simulate "bob" finishing its ENTIRE create_host lifecycle
            # while alice's apply is still in flight — exactly what running
            # two `rc dev up`s in parallel produces.
            service.terraform_runner = bob_runner
            service.create_host(
                name="bob",
                source=GitSource(url="https://github.com/owner/bob.git", ref="main"),
                instance_type="t4g.large",
                region="us-west-1",
            )
            service.terraform_runner = alice_runner
            return {
                "instance_id": "i-alice",
                "public_ip": "203.0.113.10",
                "public_dns": "ec2-203-0-113-10.compute.amazonaws.com",
            }

        alice_runner.apply.side_effect = alice_apply_side_effect
        service.terraform_runner = alice_runner

        service.create_host(
            name="alice",
            source=GitSource(url="https://github.com/owner/alice.git", ref="main"),
            instance_type="t4g.large",
            region="us-west-1",
        )

        hosts = service._load_state()
        assert "alice" in hosts, "the first (outer) create_host lost its own entry"
        assert "bob" in hosts, "the second (inner/concurrent) create_host got dropped"


class TestDestroyRefreshesTfvarsForSchemaDrift:
    """terraform.tfvars.json reflects whatever variable schema was current
    when the box was created. If the module's variables change later (e.g.
    user_data -> user_data_base64, added for the user-data-gzip fix), destroy
    used to fail outright on any box old enough to predate the change — "No
    value for required variable" — even though nothing about tearing it down
    depends on that variable's value. Hit for real destroying a box created
    before that change. destroy must regenerate tfvars from the stored
    record first.
    """

    def test_destroy_rewrites_stale_tfvars_before_destroying(
        self, mock_credential_service, mock_aws_factory, tmp_path
    ):
        import json

        from remote_compose.dev_host.bootstrap import GitSource
        from remote_compose.dev_host.service import DevHostService

        tf_dir = tmp_path / "tf"
        tf_dir.mkdir()
        runner = MagicMock()
        runner.working_dir = tf_dir
        runner.apply.return_value = {
            "instance_id": "i-stale",
            "public_ip": "203.0.113.9",
            "public_dns": "ec2-203-0-113-9.compute.amazonaws.com",
        }

        service = DevHostService(
            credential_service=mock_credential_service,
            terraform_runner=runner,
            aws_client_factory=mock_aws_factory,
            state_path=tmp_path / "dev-hosts.yml",
        )
        service.create_host(
            name="alice",
            source=GitSource(url="https://github.com/owner/repo.git", ref="main"),
            instance_type="t4g.large",
            region="us-west-1",
        )

        # Simulate schema drift: an old-format tfvars.json missing a variable
        # the module now requires, as if the box predated the module change.
        (tf_dir / "terraform.tfvars.json").write_text(
            json.dumps({"name": "alice", "user_data": "stale-old-format"})
        )

        service.destroy_host("alice")

        rewritten = json.loads((tf_dir / "terraform.tfvars.json").read_text())
        assert "user_data_base64" in rewritten
        assert rewritten["instance_type"] == "t4g.large"
        runner.destroy.assert_called_once()

    def test_destroy_still_tears_down_if_tfvars_refresh_fails(
        self, mock_credential_service, mock_terraform_runner, mock_aws_factory, tmp_path
    ):
        """An unreconstructable stored source must not block the teardown
        itself — refreshing tfvars is a best-effort improvement, not a new
        way for destroy to fail."""
        from remote_compose.dev_host.service import DevHostService

        service = DevHostService(
            credential_service=mock_credential_service,
            terraform_runner=mock_terraform_runner,
            aws_client_factory=mock_aws_factory,
            state_path=tmp_path / "dev-hosts.yml",
        )
        service._save_state(
            {"alice": {"name": "alice", "source": {"type": "bogus-unknown-type"}}}
        )

        service.destroy_host("alice")  # must not raise

        mock_terraform_runner.destroy.assert_called_once()


class TestSshCommand:
    def test_get_ssh_command_uses_stored_key(self, service, git_source):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        cmd = service.get_ssh_command("alice")

        assert "ssh" in cmd
        assert "203.0.113.42" in cmd
        assert "ec2-user" in cmd or "-l" in cmd


class TestInstanceTypeAllowlist:
    def test_known_arm_types(self):
        from remote_compose.utils.ec2_instance_types import get_arch

        assert get_arch("t4g.medium") == "arm64"
        assert get_arch("t4g.large") == "arm64"
        assert get_arch("m6g.large") == "arm64"

    def test_known_x86_types(self):
        from remote_compose.utils.ec2_instance_types import get_arch

        assert get_arch("t3.medium") == "x86_64"
        assert get_arch("m5.large") == "x86_64"

    def test_unknown_type_raises(self):
        from remote_compose.exceptions import ValidationError
        from remote_compose.utils.ec2_instance_types import get_arch

        with pytest.raises(ValidationError):
            get_arch("bogus.99xlarge")


class TestDefaultInstanceType:
    """The default must be able to actually build the stack.

    t4g.medium (the old default) OOMs during the docker image builds, so the
    documented default could never produce a working multi-repo box. And since
    provisioning is dominated by CPU-bound builds, cores buy wall-clock
    directly: measured 20m on t4g.large (2 vCPU) vs 10m43s on t4g.2xlarge
    (8 vCPU) for the same three-repo stack.
    """

    def test_cli_default_is_2xlarge(self):
        from remote_compose.cli_commands.dev import dev_up_cmd

        opt = next(p for p in dev_up_cmd.params if p.name == "instance_type")
        assert opt.default == "t4g.2xlarge", f"CLI default is {opt.default}"

    def test_service_default_matches_cli(self):
        import inspect

        from remote_compose.dev_host.service import DevHostService

        sig = inspect.signature(DevHostService.create_host)
        assert sig.parameters["instance_type"].default == "t4g.2xlarge"

    def test_default_is_a_known_arm_type(self):
        # An unknown type raises ValidationError at create_host and the AMI
        # lookup would pick the wrong architecture.
        from remote_compose.utils.ec2_instance_types import get_arch

        assert get_arch("t4g.2xlarge") == "arm64"


class TestDefaultEbsSize:
    """30GiB (the old default) left a multi-repo box at 94% full right after
    first boot: ~9GB images + ~7GB build cache + containers + volumes on top
    of the OS and three repo checkouts (sentinal, react-web-app, browser-mgr).
    Measured live on a running dev host. Heavy rebuild days (many agent
    stacks in flight) fill whatever headroom is left even faster.
    """

    def test_cli_default_is_100gb(self):
        from remote_compose.cli_commands.dev import dev_up_cmd

        opt = next(p for p in dev_up_cmd.params if p.name == "ebs_size_gb")
        assert opt.default == 100, f"CLI default is {opt.default}"

    def test_service_default_matches_cli(self):
        import inspect

        from remote_compose.dev_host.service import DevHostService

        sig = inspect.signature(DevHostService.create_host)
        assert sig.parameters["ebs_size_gb"].default == 100

    def test_terraform_default_matches(self):
        from pathlib import Path

        variables_tf = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "remote_compose"
            / "terraform"
            / "dev_host"
            / "variables.tf"
        )
        content = variables_tf.read_text()
        assert 'variable "ebs_size_gb"' in content
        block = content.split('variable "ebs_size_gb"', 1)[1].split("}", 1)[0]
        assert (
            "default     = 100" in block
        ), "terraform variable default drifted from the CLI/service default"


class TestFilesystemKeyStoreOverwritesStaleKeys:
    """destroy_host doesn't remove local key material, so re-provisioning a
    same-named box after a previous one was destroyed hits whatever mode that
    leftover file was in. A 0400 leftover used to crash store_ssh_keypair with
    a raw PermissionError instead of just being overwritten — hit for real
    provisioning a fresh 'crawlgraph4' after a chmod 400'd leftover from an
    earlier destroyed box of the same name.
    """

    def test_overwrites_a_readonly_leftover_private_key(self, tmp_path):
        from remote_compose.dev_host.service import FilesystemKeyStore

        store = FilesystemKeyStore(root=tmp_path)
        priv_path = tmp_path / "dev-host-alice-key.pem"
        priv_path.write_text("stale-old-key")
        priv_path.chmod(0o400)

        cred = store.store_ssh_keypair(
            name="dev-host-alice-key",
            private_pem="fresh-new-key",
            public_openssh="ssh-ed25519 AAAA fake",
        )

        assert priv_path.read_text() == "fresh-new-key"
        assert cred.id == str(priv_path)

    def test_overwrites_a_readonly_leftover_public_key(self, tmp_path):
        from remote_compose.dev_host.service import FilesystemKeyStore

        store = FilesystemKeyStore(root=tmp_path)
        pub_path = tmp_path / "dev-host-alice-key.pub"
        pub_path.write_text("stale-old-pub")
        pub_path.chmod(0o400)

        store.store_ssh_keypair(
            name="dev-host-alice-key",
            private_pem="fresh-new-key",
            public_openssh="ssh-ed25519 AAAA fresh",
        )

        assert pub_path.read_text() == "ssh-ed25519 AAAA fresh"

    def test_fresh_keypair_still_gets_correct_modes(self, tmp_path):
        from remote_compose.dev_host.service import FilesystemKeyStore

        store = FilesystemKeyStore(root=tmp_path)
        store.store_ssh_keypair(
            name="dev-host-bob-key",
            private_pem="key-material",
            public_openssh="ssh-ed25519 AAAA fake",
        )

        priv_path = tmp_path / "dev-host-bob-key.pem"
        pub_path = tmp_path / "dev-host-bob-key.pub"
        assert oct(priv_path.stat().st_mode)[-3:] == "600"
        assert oct(pub_path.stat().st_mode)[-3:] == "644"
