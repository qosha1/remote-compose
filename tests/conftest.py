"""
Pytest configuration and fixtures.
"""

import os
import pytest
import django


def pytest_configure():
    """Configure Django settings for tests."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
    django.setup()
    # rc-8zz: opt all tests out of the post-rollout ECS event watcher by
    # default. The watcher polls describe_services for up to 60s after
    # force-roll — fine in production, but every force-roll caller in
    # the unit tests would otherwise wait on it. Tests that specifically
    # exercise the watcher (test_post_rollout_watcher.py) override this
    # via monkeypatch.setenv.
    os.environ.setdefault("RC_POST_ROLLOUT_WATCH_S", "0")
    # Same for the post-roll steady-state wait (rc zero-downtime gate): the
    # waiter polls describe_services until services stabilize — opt unit
    # tests out by default; the wait's own tests override via setenv.
    os.environ.setdefault("RC_DEPLOY_WAIT_S", "0")


# ---------------------------------------------------------------------------
# rc-9r2u: process-env isolation — no test's os.environ mutation survives it.
# ---------------------------------------------------------------------------
# `monkeypatch.setenv`/`delenv` already auto-revert, so they're unaffected by
# this. It exists for code paths that mutate `os.environ` directly (e.g.
# `os.environ.setdefault(...)`, `os.environ[...] = ...`) without going through
# monkeypatch — those mutations otherwise survive the test and leak into
# whichever test pytest-randomly schedules next in the same process. Root
# cause example: cli_commands/db.py's `rc db psql` does
# `os.environ.setdefault("AWS_PROFILE", aws_profile)` and never unsets it;
# under pytest-randomly, a test exercising that path ahead of a moto-backed
# AWS test poisons the later test's credential resolution with
# `botocore.exceptions.ProfileNotFound`. Autouse + function-scoped so it
# applies to every test in the fast + integration tiers regardless of
# ordering; snapshot/restore of a ~dozen-to-few-hundred-key dict is
# microseconds, well inside the 8s per-test runtime budget below.
@pytest.fixture(autouse=True)
def _isolate_os_environ():
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


# ---------------------------------------------------------------------------
# Shared model fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cluster(db):
    """Shared ECSCluster fixture used across converter and infrastructure tests."""
    from remote_compose.models import ECSCluster

    return ECSCluster.objects.create(
        name="test-cluster",
        aws_cluster_name="test-cluster",
        aws_cluster_arn="arn:aws:ecs:us-east-1:123456789:cluster/test-cluster",
        aws_region="us-east-1",
        launch_type=ECSCluster.LaunchType.FARGATE,
        status=ECSCluster.ClusterStatus.ACTIVE,
        subnet_ids=["subnet-123", "subnet-456"],
        security_group_ids=["sg-123"],
    )


# ---------------------------------------------------------------------------
# Shared preprocessor helpers
# ---------------------------------------------------------------------------


def make_preprocessed_from_tuples(
    *services, named_volumes=None, warnings=None, errors=None
):
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
            env_vars=config.get("environment", {}),
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
        mocker.MagicMock(
            read=lambda: b"success",
            channel=mocker.MagicMock(recv_exit_status=lambda: 0),
        ),  # stdout
        mocker.MagicMock(read=lambda: b""),  # stderr
    )

    mocker.patch("paramiko.SSHClient", return_value=mock_client)
    return mock_client


# ---------------------------------------------------------------------------
# rc-0b3.3: runtime-budget guard — keep the fast loop fast.
# ---------------------------------------------------------------------------
# Any unit/contract test whose call phase exceeds the budget without an
# explicit @pytest.mark.slow is a regression in feedback speed: either speed
# it up, or mark it slow (so `-m "not slow"` deselects it from the fast loop).
# The threshold sits well above today's slowest fast-tier test (~3s) so normal
# runner variance doesn't trip it; override via RC_TEST_SLOW_BUDGET_S.
# Integration/e2e tiers are exempt — they're legitimately slow and run on
# their own gated workflows.

_FAST_TIER_SLOW_BUDGET_S = float(os.environ.get("RC_TEST_SLOW_BUDGET_S", "8.0"))
_call_durations: dict[str, float] = {}
_budget_offenders: list[tuple[str, float]] = []


def pytest_runtest_logreport(report):
    if report.when == "call":
        _call_durations[report.nodeid] = report.duration


def _is_fast_tier(nodeid: str) -> bool:
    return "/unit/" in nodeid or "/contract/" in nodeid


def pytest_sessionfinish(session, exitstatus):
    _budget_offenders.clear()
    for item in session.items:
        nodeid = item.nodeid
        if not _is_fast_tier(nodeid):
            continue
        if any(m.name == "slow" for m in item.iter_markers()):
            continue
        dur = _call_durations.get(nodeid)
        if dur is not None and dur > _FAST_TIER_SLOW_BUDGET_S:
            _budget_offenders.append((nodeid, dur))
    # Only escalate a passing run; never mask a real failure's exit code.
    if _budget_offenders and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _budget_offenders:
        return
    terminalreporter.write_sep("=", "RUNTIME BUDGET VIOLATION", red=True, bold=True)
    for nodeid, dur in sorted(_budget_offenders, key=lambda x: -x[1]):
        terminalreporter.write_line(
            f"  {dur:.1f}s  {nodeid}  — exceeds "
            f"{_FAST_TIER_SLOW_BUDGET_S:.0f}s budget; mark @pytest.mark.slow "
            f"or speed it up"
        )
