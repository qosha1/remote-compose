"""rc-2v8 (extended): pre-flight refuses docker build when the build
context is multi-GB. Sentinal pattern (backend/ was 6.8GB) would
otherwise hang buildkit for 25+ min on context upload before the user
sees any feedback.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.base import ProviderConfigError


def _make_ctx_with_large_context(tmp_path: Path, size_bytes: int) -> DeployContext:
    """Lay out a build context with a single sparse file of the requested size."""
    ctx_path = tmp_path / "huge_context"
    ctx_path.mkdir()
    (ctx_path / "Dockerfile").write_text("FROM alpine\n")
    big = ctx_path / "media" / "uploads.bin"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as fh:
        if size_bytes > 0:
            fh.seek(size_bytes - 1)
            fh.write(b"\0")
    return DeployContext(
        project="ctx-size-test",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-east-1", "cluster": "x", "vpc_cidr": "10.0.0.0/16"}},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "api": ServiceSpec(
                name="api", cpu=256, memory=512, type="application",
                build_context=ctx_path,
            ),
        },
        secrets=[],
    )


class TestBuildContextPreflight:
    def test_blocks_when_context_over_5gb(self, tmp_path):
        # 6GB context — should hard-error.
        ctx = _make_ctx_with_large_context(tmp_path, 6 * 1024 * 1024 * 1024)
        provider = ECSProvider()
        with pytest.raises(ProviderConfigError) as exc_info:
            provider._preflight_build_context_sizes(list(ctx.services.values()))
        assert "rc-2v8" in str(exc_info.value)
        assert "multi-GB context" in str(exc_info.value)
        assert ".dockerignore" in str(exc_info.value)
        assert "RC_FORCE_LARGE_CONTEXT" in str(exc_info.value)

    def test_under_warn_threshold_silent(self, tmp_path):
        # 100MB — under the 1GB warn threshold.
        ctx = _make_ctx_with_large_context(tmp_path, 100 * 1024 * 1024)
        events: list[str] = []
        provider = ECSProvider(progress=events.append)
        provider._preflight_build_context_sizes(list(ctx.services.values()))
        assert events == []

    def test_warn_at_1_5gb(self, tmp_path):
        # 1.5GB — over WARN, under BLOCK → warn but don't raise.
        ctx = _make_ctx_with_large_context(tmp_path, int(1.5 * 1024 * 1024 * 1024))
        events: list[str] = []
        provider = ECSProvider(progress=events.append)
        provider._preflight_build_context_sizes(list(ctx.services.values()))
        assert any("WARN" in e and "1.5GB" in e for e in events), (
            f"expected WARN line for 1.5GB context. events={events}"
        )

    def test_force_override_allows_huge_context(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RC_FORCE_LARGE_CONTEXT", "1")
        ctx = _make_ctx_with_large_context(tmp_path, 6 * 1024 * 1024 * 1024)
        events: list[str] = []
        provider = ECSProvider(progress=events.append)
        # No raise; should warn instead.
        provider._preflight_build_context_sizes(list(ctx.services.values()))
        assert any("WARN" in e for e in events)

    def test_threshold_env_overrides(self, tmp_path, monkeypatch):
        # Lower BLOCK threshold to 1GB so a 100MB context still passes
        # but a 1.5GB context errors.
        monkeypatch.setenv("RC_BUILD_CONTEXT_BLOCK_GB", "1")
        monkeypatch.setenv("RC_BUILD_CONTEXT_WARN_GB", "0.05")
        ctx = _make_ctx_with_large_context(tmp_path, int(1.5 * 1024 * 1024 * 1024))
        provider = ECSProvider()
        with pytest.raises(ProviderConfigError):
            provider._preflight_build_context_sizes(list(ctx.services.values()))
