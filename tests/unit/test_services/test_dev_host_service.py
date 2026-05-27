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

from pathlib import Path
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
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )

        call_kwargs = mock_terraform_runner.apply.call_args.kwargs
        # the tf apply must include tags identifying the dev host
        tags = call_kwargs.get("tags") or call_kwargs.get("variables", {}).get("tags", {})
        assert tags.get("DevHost") == "alice"
        assert tags.get("ManagedBy") == "rc-dev"

    def test_create_host_generates_and_stores_ssh_keypair(
        self, service, git_source, mock_credential_service
    ):
        service.create_host(
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )

        mock_credential_service.store_ssh_keypair.assert_called_once()

    def test_create_host_writes_to_state_file(self, service, git_source, tmp_path):
        import yaml

        service.create_host(
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )

        state_file = tmp_path / "dev-hosts.yml"
        assert state_file.exists()
        loaded = yaml.safe_load(state_file.read_text())
        assert "alice" in loaded["hosts"]

    def test_create_host_rejects_duplicate_name(self, service, git_source):
        from remote_compose.exceptions import ValidationError

        service.create_host(
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
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
                name="alice", source=git_source,
                instance_type="t4g.medium", region="us-west-1",
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
            cmd=["terraform", "apply"], returncode=1,
            stdout="", stderr="apply fail",
        )
        mock_terraform_runner.destroy.side_effect = TerraformError(
            cmd=["terraform", "destroy"], returncode=1,
            stdout="", stderr="destroy fail too",
        )

        with pytest.raises(TerraformError):
            service.create_host(
                name="alice", source=git_source,
                instance_type="t4g.medium", region="us-west-1",
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
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )
        service.create_host(
            name="bob", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )

        hosts = service.list_hosts()
        names = sorted(h.name for h in hosts)
        assert names == ["alice", "bob"]


class TestGetHost:
    def test_get_existing(self, service, git_source):
        created = service.create_host(
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
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
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )
        service.destroy_host("alice")

        mock_terraform_runner.destroy.assert_called_once()

    def test_destroy_removes_from_state(self, service, git_source, tmp_path):
        import yaml

        service.create_host(
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
        )
        service.destroy_host("alice")

        loaded = yaml.safe_load((tmp_path / "dev-hosts.yml").read_text())
        assert "alice" not in (loaded.get("hosts") or {})

    def test_destroy_missing_no_force_raises(self, service):
        from remote_compose.exceptions import ValidationError

        with pytest.raises(ValidationError):
            service.destroy_host("ghost")

    def test_destroy_missing_with_force_noops(self, service):
        # idempotent destroy with --force does not raise on missing host
        service.destroy_host("ghost", force=True)


class TestSshCommand:
    def test_get_ssh_command_uses_stored_key(self, service, git_source):
        service.create_host(
            name="alice", source=git_source, instance_type="t4g.medium", region="us-west-1"
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
