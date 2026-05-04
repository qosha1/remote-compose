"""End-to-end integration tests for DevHostService with moto-mocked AWS.

TDD red phase for [rc dev 2.2] (rc-srl). Tests assert the DevHostService
wires correctly through real (mocked) boto3 + a fake terraform runner so
that 'rc dev up alice' produces a valid in-memory state machine.

These do NOT invoke the real terraform binary (that's covered in
test_dev_host_terraform.py); they test the orchestration glue.

Phase 4.1 (rc-z7p) makes these green.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest


pytestmark = pytest.mark.integration


@pytest.fixture
def moto_aws():
    """Activate moto for all AWS APIs used by DevHostService."""
    from moto import mock_aws

    with mock_aws():
        yield


@pytest.fixture
def terraform_runner_stub():
    """Stand-in TerraformRunner that records calls and returns canned outputs."""
    runner = MagicMock()
    runner.apply.return_value = {
        "instance_id": "i-deadbeef00000001",
        "public_ip": "203.0.113.10",
        "public_dns": "ec2-203-0-113-10.compute.amazonaws.com",
    }
    return runner


@pytest.fixture
def credential_service_stub():
    cs = MagicMock()
    stored = MagicMock(id=99, name="dev-host-key")
    cs.store_ssh_keypair.return_value = stored
    cs.get_ssh_keypair.return_value = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n",
        "ssh-ed25519 AAAA",
    )
    return cs


@pytest.fixture
def aws_client_factory(moto_aws):
    """Factory that returns real (moto-intercepted) boto3 clients."""

    class _Factory:
        def get_client(self, service: str, region_name: str = "us-west-1"):
            return boto3.client(service, region_name=region_name)

    return _Factory()


@pytest.fixture
def service(
    credential_service_stub, terraform_runner_stub, aws_client_factory, tmp_path
):
    from remote_compose.dev_host.service import DevHostService

    return DevHostService(
        credential_service=credential_service_stub,
        terraform_runner=terraform_runner_stub,
        aws_client_factory=aws_client_factory,
        state_path=tmp_path / "dev-hosts.yml",
    )


@pytest.fixture
def git_source():
    from remote_compose.dev_host.bootstrap import GitSource

    return GitSource(url="https://github.com/owner/repo.git", ref="main")


class TestEndToEndLifecycle:
    def test_create_then_list_then_destroy(self, service, git_source):
        # create
        record = service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )
        assert record.name == "alice"

        # list reflects creation
        hosts = service.list_hosts()
        assert [h.name for h in hosts] == ["alice"]

        # destroy
        service.destroy_host("alice")
        assert service.list_hosts() == []

    def test_state_persists_across_service_instances(
        self,
        credential_service_stub,
        terraform_runner_stub,
        aws_client_factory,
        tmp_path,
        git_source,
    ):
        """A second DevHostService instance reads state from the same file."""
        from remote_compose.dev_host.service import DevHostService

        state_path = tmp_path / "dev-hosts.yml"
        svc1 = DevHostService(
            credential_service=credential_service_stub,
            terraform_runner=terraform_runner_stub,
            aws_client_factory=aws_client_factory,
            state_path=state_path,
        )
        svc1.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        svc2 = DevHostService(
            credential_service=credential_service_stub,
            terraform_runner=terraform_runner_stub,
            aws_client_factory=aws_client_factory,
            state_path=state_path,
        )
        hosts = svc2.list_hosts()
        assert [h.name for h in hosts] == ["alice"]

    def test_terraform_apply_receives_user_data(
        self, service, git_source, terraform_runner_stub
    ):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        call_kwargs = terraform_runner_stub.apply.call_args.kwargs
        # tf apply must receive rendered cloud-init as user_data variable
        variables = call_kwargs.get("variables", {})
        user_data = variables.get("user_data") or call_kwargs.get("user_data")
        assert user_data is not None
        assert user_data.startswith("#cloud-config")
        # must include the git url/ref so the bootstrap clones the right thing
        assert "https://github.com/owner/repo.git" in user_data

    def test_terraform_apply_receives_ssh_public_key(
        self, service, git_source, terraform_runner_stub
    ):
        service.create_host(
            name="alice",
            source=git_source,
            instance_type="t4g.medium",
            region="us-west-1",
        )

        call_kwargs = terraform_runner_stub.apply.call_args.kwargs
        variables = call_kwargs.get("variables", {})
        pubkey = variables.get("ssh_public_key") or call_kwargs.get("ssh_public_key")
        assert pubkey is not None
        assert pubkey.startswith("ssh-")


class TestSourceRoundTrip:
    """SourceSpec must serialize to the state file and deserialize on read."""

    def test_git_source_round_trips_through_state(
        self,
        credential_service_stub,
        terraform_runner_stub,
        aws_client_factory,
        tmp_path,
    ):
        from remote_compose.dev_host.bootstrap import GitSource
        from remote_compose.dev_host.service import DevHostService

        state_path = tmp_path / "dev-hosts.yml"
        svc = DevHostService(
            credential_service=credential_service_stub,
            terraform_runner=terraform_runner_stub,
            aws_client_factory=aws_client_factory,
            state_path=state_path,
        )

        original = GitSource(
            url="https://github.com/owner/repo.git", ref="alice/feat-x"
        )
        svc.create_host(
            name="alice", source=original, instance_type="t4g.medium", region="us-west-1"
        )

        # fresh service instance reads from disk
        svc2 = DevHostService(
            credential_service=credential_service_stub,
            terraform_runner=terraform_runner_stub,
            aws_client_factory=aws_client_factory,
            state_path=state_path,
        )
        record = svc2.get_host("alice")

        # source field must round-trip with the same shape
        assert record.source["type"] == "git"
        assert record.source["url"] == original.url
        assert record.source["ref"] == original.ref
