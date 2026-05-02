"""rc-44z: ECSProvider.deploy honors ctx.skip_build by skipping
_build_and_push_images entirely. Force-roll still runs so services
pick up any task-def changes terraform just applied. Without this,
'rc deploy --no-build' was silently ignored on the v2 path and
buildx ran every time.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.terraform.runner import RecordingTerraformRunner


def _ctx(tmp_path: Path, *, skip_build: bool = False) -> DeployContext:
    ctx = DeployContext(
        project="myapp",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {
            "region": "us-west-2",
            "cluster": "myapp-prod",
            "vpc_cidr": "10.0.0.0/16",
        }},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "django": ServiceSpec(
                name="django", cpu=512, memory=1024, type="application",
                image="x:latest",
            ),
            "postgres": ServiceSpec(
                name="postgres", cpu=256, memory=512, type="infrastructure",
                image="postgres:16",
            ),
        },
        secrets=[],
    )
    ctx.skip_build = skip_build
    return ctx


def _provider(runner_holder, mock_session):
    def factory(out_dir: Path):
        if runner_holder.get("runner") is None:
            runner_holder["runner"] = RecordingTerraformRunner(out_dir)
        return runner_holder["runner"]

    return ECSProvider(
        runner_factory=factory,
        session_factory=lambda ctx: mock_session,
    )


@pytest.fixture
def mock_session():
    sess = mock.MagicMock()
    sess.client.return_value = mock.MagicMock()
    return sess


class TestSkipBuild:
    def test_skip_build_omits_buildx_invocation(self, tmp_path, mock_session):
        """ctx.skip_build=True must NOT call _build_and_push_images.
        Spy on the method to confirm zero invocations."""
        holder = {"runner": None}
        provider = _provider(holder, mock_session)
        with mock.patch.object(
            ECSProvider, "_build_and_push_images",
        ) as build_mock:
            provider.deploy(_ctx(tmp_path, skip_build=True))
        build_mock.assert_not_called()

    def test_skip_build_still_force_rolls(self, tmp_path, mock_session):
        """Force-roll still happens — that's the point: terraform changes
        a task-def field, force-roll picks up the new revision."""
        holder = {"runner": None}
        provider = _provider(holder, mock_session)
        with mock.patch.object(
            ECSProvider, "_force_new_deployments",
        ) as roll_mock:
            provider.deploy(_ctx(tmp_path, skip_build=True))
        roll_mock.assert_called_once()
        # Force-roll target list = all services, in sorted order.
        targets = roll_mock.call_args.args[1]
        assert sorted(targets) == ["django", "postgres"]

    def test_skip_build_with_services_filter_rolls_only_filtered(
        self, tmp_path, mock_session,
    ):
        holder = {"runner": None}
        provider = _provider(holder, mock_session)
        with mock.patch.object(
            ECSProvider, "_force_new_deployments",
        ) as roll_mock:
            provider.deploy(
                _ctx(tmp_path, skip_build=True),
                services_filter=["django"],
            )
        roll_mock.assert_called_once()
        assert roll_mock.call_args.args[1] == ["django"]

    def test_skip_build_returns_deploy_result(self, tmp_path, mock_session):
        holder = {"runner": None}
        provider = _provider(holder, mock_session)
        result = provider.deploy(_ctx(tmp_path, skip_build=True))
        assert sorted(result.services) == ["django", "postgres"]

    def test_default_path_still_builds(self, tmp_path, mock_session):
        """Backwards-compat: when skip_build=False (default), the build
        path still runs."""
        holder = {"runner": None}
        provider = _provider(holder, mock_session)
        with mock.patch.object(
            ECSProvider, "_build_and_push_images", return_value=[],
        ) as build_mock:
            provider.deploy(_ctx(tmp_path, skip_build=False))
        build_mock.assert_called_once()
