"""
Pytest configuration and fixtures.
"""

import os
import pytest
import django
from django.conf import settings


def pytest_configure():
    """Configure Django settings for tests."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tests.settings')
    django.setup()
    # rc-8zz: opt all tests out of the post-rollout ECS event watcher by
    # default. The watcher polls describe_services for up to 60s after
    # force-roll — fine in production, but every force-roll caller in
    # the unit tests would otherwise wait on it. Tests that specifically
    # exercise the watcher (test_post_rollout_watcher.py) override this
    # via monkeypatch.setenv.
    os.environ.setdefault("RC_POST_ROLLOUT_WATCH_S", "0")


# ---------------------------------------------------------------------------
# Shared model fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cluster(db):
    """Shared ECSCluster fixture used across converter and infrastructure tests."""
    from remote_compose.models import ECSCluster
    return ECSCluster.objects.create(
        name='test-cluster',
        aws_cluster_name='test-cluster',
        aws_cluster_arn='arn:aws:ecs:us-east-1:123456789:cluster/test-cluster',
        aws_region='us-east-1',
        launch_type=ECSCluster.LaunchType.FARGATE,
        status=ECSCluster.ClusterStatus.ACTIVE,
        subnet_ids=['subnet-123', 'subnet-456'],
        security_group_ids=['sg-123'],
    )


# ---------------------------------------------------------------------------
# Shared preprocessor helpers
# ---------------------------------------------------------------------------

def make_preprocessed_from_tuples(*services, named_volumes=None, warnings=None, errors=None):
    """
    Build a PreprocessedCompose from a variable number of service tuples.

    Each element in *services* is a tuple of:
        (name, config, image_name, build_info)

    Optional keyword arguments set top-level fields on PreprocessedCompose.
    """
    from remote_compose.services.compose_preprocessor import (
        PreprocessedCompose,
        PreprocessedService,
    )
    svc_dict = {}
    for name, config, image, build_info in services:
        requires_build = build_info is not None
        svc_dict[name] = PreprocessedService(
            name=name,
            config=config,
            image_name=image,
            build_info=build_info,
            requires_build=requires_build,
            env_vars=config.get('environment', {}),
        )
    return PreprocessedCompose(
        services=svc_dict,
        named_volumes=named_volumes or {},
        warnings=warnings or [],
        errors=errors or [],
    )


def make_preprocessed_from_services(*services):
    """
    Build a PreprocessedCompose from a variable number of PreprocessedService
    instances.
    """
    from remote_compose.services.compose_preprocessor import PreprocessedCompose
    svc_dict = {svc.name: svc for svc in services}
    return PreprocessedCompose(services=svc_dict)


@pytest.fixture
def sample_compose_content():
    """Sample docker-compose.yml content."""
    return """
version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "8080:80"
  redis:
    image: redis:alpine
"""


@pytest.fixture
def sample_ssh_key():
    """Sample SSH private key (fake, for testing only)."""
    return """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AKk8KnME0iFLHFEP0mXn
FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake
FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake
FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake
-----END RSA PRIVATE KEY-----"""


@pytest.fixture
def mock_ssh_success(mocker):
    """Mock successful SSH connection."""
    mock_client = mocker.MagicMock()
    mock_client.connect.return_value = None
    mock_client.exec_command.return_value = (
        mocker.MagicMock(),  # stdin
        mocker.MagicMock(read=lambda: b'success', channel=mocker.MagicMock(recv_exit_status=lambda: 0)),  # stdout
        mocker.MagicMock(read=lambda: b''),  # stderr
    )

    mocker.patch('paramiko.SSHClient', return_value=mock_client)
    return mock_client
