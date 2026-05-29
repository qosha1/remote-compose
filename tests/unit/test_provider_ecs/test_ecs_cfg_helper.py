"""rc-tuc: _ecs_cfg() centralizes the bespoke
'(ctx.provider_config or {}).get("ecs") or {}' chain that was repeated
~8 times in provider.py. Adds explicit type checks + a `require` knob
so a missing key raises ProviderConfigError with the key name (instead
of a downstream TypeError on NoneType).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs.provider import _ecs_cfg


def _ctx(provider_config) -> DeployContext:
    return DeployContext(
        project="t",
        compose_path=Path("/tmp/x.yml"),
        rc_yml_v2={},
        provider_config=provider_config,
        tf_backend_config={"type": "local"},
        working_dir=Path("/tmp"),
        services={},
        secrets=[],
    )


class TestEcsCfgNormalization:
    def test_returns_ecs_dict_when_present(self):
        ctx = _ctx({"ecs": {"region": "us-west-2", "cluster": "x"}})
        assert _ecs_cfg(ctx) == {"region": "us-west-2", "cluster": "x"}

    def test_empty_dict_when_provider_config_none(self):
        ctx = _ctx(None)
        assert _ecs_cfg(ctx) == {}

    def test_empty_dict_when_ecs_key_missing(self):
        ctx = _ctx({"other": "stuff"})
        assert _ecs_cfg(ctx) == {}

    def test_empty_dict_when_ecs_value_none(self):
        ctx = _ctx({"ecs": None})
        assert _ecs_cfg(ctx) == {}


class TestEcsCfgTypeValidation:
    def test_raises_when_provider_config_not_dict(self):
        ctx = _ctx("not-a-dict")
        with pytest.raises(ProviderConfigError, match="provider_config must be a dict"):
            _ecs_cfg(ctx)

    def test_raises_when_ecs_not_dict(self):
        ctx = _ctx({"ecs": ["list-not-dict"]})
        with pytest.raises(
            ProviderConfigError, match="provider_config.ecs must be a dict"
        ):
            _ecs_cfg(ctx)


class TestEcsCfgRequireKnob:
    def test_passes_when_required_keys_present(self):
        ctx = _ctx({"ecs": {"region": "us-west-1", "cluster": "c"}})
        out = _ecs_cfg(ctx, require=("region", "cluster"))
        assert out["region"] == "us-west-1"

    def test_raises_when_required_key_missing(self):
        ctx = _ctx({"ecs": {"cluster": "c"}})
        with pytest.raises(
            ProviderConfigError, match="provider_config.ecs.region is required"
        ):
            _ecs_cfg(ctx, require=("region",))

    def test_raises_when_required_key_empty_string(self):
        # Falsy values count as missing — '' is not a valid region.
        ctx = _ctx({"ecs": {"region": ""}})
        with pytest.raises(
            ProviderConfigError, match="provider_config.ecs.region is required"
        ):
            _ecs_cfg(ctx, require=("region",))

    def test_raises_first_missing_key_name(self):
        ctx = _ctx({"ecs": {}})
        with pytest.raises(
            ProviderConfigError, match="provider_config.ecs.region is required"
        ):
            _ecs_cfg(ctx, require=("region", "cluster"))

    def test_no_require_returns_partial_config(self):
        # Without require=, missing keys are tolerated — caller has
        # downstream defaults / fallback paths.
        ctx = _ctx({"ecs": {"cluster": "c"}})
        assert _ecs_cfg(ctx) == {"cluster": "c"}
