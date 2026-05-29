"""Fixtures shared by provider contract tests.

Each test in this directory is parameterized across every Provider implementation
discovered in the registry. FakeProvider is the baseline; real providers (ECS,
k8s) opt in by being importable and self-registering.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Iterable

import pytest

from remote_compose.provider import DeployContext, Provider, ServiceSpec, available, get

_PROVIDER_MODULES = [
    "remote_compose.provider.fake",
    "remote_compose.provider.ecs",
    "remote_compose.provider.k8s",
]


def _load_providers() -> None:
    for mod in _PROVIDER_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            # Real providers are optional extras; FakeProvider import is a hard
            # requirement for contract tests to have anything to run against.
            continue


_load_providers()


@pytest.fixture(autouse=True)
def _reset_fake_state() -> None:
    """Per-test reset of FakeProvider class-level state to prevent leakage."""
    try:
        from remote_compose.provider.fake import FakeProvider

        FakeProvider.reset()
    except ImportError:
        pass


def _provider_ids() -> Iterable[str]:
    return available()


def _provider_selected() -> list[str]:
    """Which providers to run the contract suite against in this process.

    Default: FakeProvider only. Real-cloud providers require backing infra
    (LocalStack / kind / real AWS) and are opt-in via env var:

        RC_CONTRACT_PROVIDERS=ecs          → ECS only
        RC_CONTRACT_PROVIDERS=fake,ecs     → both
        RC_CONTRACT_PROVIDERS=all          → every registered provider
    """
    env = os.environ.get("RC_CONTRACT_PROVIDERS")
    if not env:
        return ["fake"]
    if env.strip() == "all":
        names = available()
        return names or ["fake"]
    return [p.strip() for p in env.split(",") if p.strip()]


@pytest.fixture(params=_provider_selected())
def provider(request) -> Provider:
    name = request.param
    if name not in available():
        pytest.skip(f"provider '{name}' is not registered (missing extras or impl)")
    cls = get(name)
    return cls()


@pytest.fixture
def compose_samples_dir() -> Path:
    return Path(__file__).parent.parent / "compose_samples"


@pytest.fixture
def minimal_compose_path(compose_samples_dir: Path) -> Path:
    return compose_samples_dir / "minimal_3_service.yml"


@pytest.fixture
def working_dir(tmp_path: Path) -> Path:
    return tmp_path


_PROVIDER_DEFAULT_CONFIG: dict[str, dict] = {
    "fake": {},
    "ecs": {
        "ecs": {
            "region": "us-west-2",
            "cluster": "contract-test",
            "vpc_cidr": "10.0.0.0/16",
        }
    },
    # k8s defaults land when the k8s provider ships (rc-e5u.8).
}


def _default_provider_config(provider: Provider) -> dict:
    return _PROVIDER_DEFAULT_CONFIG.get(provider.name, {})


@pytest.fixture
def minimal_ctx(
    provider: Provider,
    minimal_compose_path: Path,
    working_dir: Path,
) -> DeployContext:
    """A minimal DeployContext against the 3-service sample compose file.

    provider_config is set to the smallest valid config for the provider
    under test so contract tests don't need provider-specific knowledge.
    """
    services = {
        "web": ServiceSpec(
            name="web",
            cpu=256,
            memory=512,
            replicas=1,
            type="proxy",
            public=True,
            port=80,
        ),
        "api": ServiceSpec(
            name="api",
            cpu=512,
            memory=1024,
            replicas=1,
            type="application",
            health_check_path="/",
        ),
        "cache": ServiceSpec(
            name="cache",
            cpu=256,
            memory=512,
            replicas=1,
            type="infrastructure",
        ),
    }
    return DeployContext(
        project="contract-test",
        compose_path=minimal_compose_path,
        rc_yml_v2={"version": 2, "project": "contract-test"},
        provider_config=_default_provider_config(provider),
        tf_backend_config={"type": "local"},
        working_dir=working_dir,
        services=services,
        secrets=[],
    )
