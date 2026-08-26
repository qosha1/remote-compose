"""containerDefinitions.restartPolicy (rc-ib01.4).

rc-m2sn assumed ``essential: false`` bought compose-like crash isolation. It
does not: ECS never restarts an individual container, so a non-essential
container that exits stays dead while the task runs on without it.

``restartPolicy`` is the mechanism that actually does. Verified against the AWS
docs and the live SSM parameter on 2026-08-26:

  * EC2 requires container agent 1.86.0+. rc's own AMI
    (``/aws/service/ecs/optimized-ami/amazon-linux-2/recommended``) ships
    **1.106.1**, so rc's default EC2 capacity supports it.
  * Works on both essential and non-essential containers.
  * ``restartAttemptPeriod`` default 300s, min 60, max 1800 — and a container
    that exits INSIDE that period is NOT restarted. So this fixes transient
    exits, not crash loops.

Opt-in, never auto-defaulted: rc emits offline by design, so it cannot
capability-check the agent on the operator's behalf.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_compose.config.v2_schema import ConfigError, parse
from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

pytestmark = pytest.mark.unit


def _cfg(**extra):
    base = {
        "version": 2,
        "project": "p",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
    }
    base.update(extra)
    return base


def _svc(name: str, **kw) -> ServiceSpec:
    kw.setdefault("cpu", 256)
    kw.setdefault("memory", 512)
    return ServiceSpec(name=name, **kw)


def _emit(tmp_path: Path, services) -> str:
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(
        DeployContext(
            project="app",
            compose_path=tmp_path / "docker-compose.yml",
            rc_yml_v2={},
            provider_config={
                "ecs": {
                    "region": "us-west-2",
                    "cluster": "c",
                    "vpc_cidr": "10.0.0.0/16",
                }
            },
            tf_backend_config={"type": "local"},
            working_dir=tmp_path,
            services=services,
            secrets=[],
        ),
        out,
    )
    return (out / "services.tf").read_text()


class TestSchema:
    def test_absent_defaults_to_none(self):
        cfg = parse(_cfg(services={"web": {"cpu": 256, "memory": 512}}))
        assert cfg.services["web"].restart_policy is None

    def test_declaring_the_block_is_the_opt_in(self):
        cfg = parse(
            _cfg(services={"web": {"cpu": 256, "memory": 512, "restart_policy": {}}})
        )
        assert cfg.services["web"].restart_policy == {"enabled": True}

    def test_full_block_round_trips(self):
        cfg = parse(
            _cfg(
                services={
                    "web": {
                        "cpu": 256,
                        "memory": 512,
                        "restart_policy": {
                            "enabled": True,
                            "ignored_exit_codes": [0, 143],
                            "attempt_period": 120,
                        },
                    }
                }
            )
        )
        assert cfg.services["web"].restart_policy == {
            "enabled": True,
            "ignored_exit_codes": [0, 143],
            "attempt_period": 120,
        }

    def test_enabled_false_is_honoured(self):
        cfg = parse(
            _cfg(
                services={
                    "web": {
                        "cpu": 256,
                        "memory": 512,
                        "restart_policy": {"enabled": False},
                    }
                }
            )
        )
        assert cfg.services["web"].restart_policy == {"enabled": False}

    def test_non_mapping_rejected(self):
        with pytest.raises(ConfigError, match="restart_policy must be a mapping"):
            parse(
                _cfg(
                    services={
                        "web": {"cpu": 256, "memory": 512, "restart_policy": True}
                    }
                )
            )

    def test_unknown_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown"):
            parse(
                _cfg(
                    services={
                        "web": {
                            "cpu": 256,
                            "memory": 512,
                            "restart_policy": {"restartAttemptPeriod": 120},
                        }
                    }
                )
            )

    @pytest.mark.parametrize("period", [59, 1801, 0, -1])
    def test_attempt_period_outside_the_aws_range_rejected(self, period):
        """AWS: min 60, max 1800."""
        with pytest.raises(ConfigError, match="between 60 and 1800"):
            parse(
                _cfg(
                    services={
                        "web": {
                            "cpu": 256,
                            "memory": 512,
                            "restart_policy": {"attempt_period": period},
                        }
                    }
                )
            )

    @pytest.mark.parametrize("period", [60, 300, 1800])
    def test_attempt_period_inside_the_range_accepted(self, period):
        cfg = parse(
            _cfg(
                services={
                    "web": {
                        "cpu": 256,
                        "memory": 512,
                        "restart_policy": {"attempt_period": period},
                    }
                }
            )
        )
        assert cfg.services["web"].restart_policy["attempt_period"] == period

    @pytest.mark.parametrize("codes", ["0", [0, "x"], {"0": 1}, [None]])
    def test_ignored_exit_codes_must_be_a_list_of_ints(self, codes):
        with pytest.raises(ConfigError, match="ignored_exit_codes"):
            parse(
                _cfg(
                    services={
                        "web": {
                            "cpu": 256,
                            "memory": 512,
                            "restart_policy": {"ignored_exit_codes": codes},
                        }
                    }
                )
            )


class TestRendering:
    def test_absent_emits_nothing(self, tmp_path):
        tf = _emit(tmp_path, {"web": _svc("web", image="w:1")})
        assert "restartPolicy" not in tf

    def test_enabled_emits_the_block(self, tmp_path):
        tf = _emit(
            tmp_path,
            {"web": _svc("web", image="w:1", restart_policy={"enabled": True})},
        )
        assert "restartPolicy = {" in tf
        assert re.search(r"restartPolicy = \{\s*\n\s*enabled\s+= true", tf)

    def test_optional_fields_are_omitted_when_unset(self, tmp_path):
        """AWS defaults restartAttemptPeriod to 300; don't pin what wasn't asked."""
        tf = _emit(
            tmp_path,
            {"web": _svc("web", image="w:1", restart_policy={"enabled": True})},
        )
        # the ASSIGNMENT must be absent; the words also appear in the
        # explanatory comment rc emits above the block
        assert not re.search(r"restartAttemptPeriod\s+=", tf)
        assert not re.search(r"ignoredExitCodes\s+=", tf)

    def test_full_block_renders_aws_field_names(self, tmp_path):
        tf = _emit(
            tmp_path,
            {
                "web": _svc(
                    "web",
                    image="w:1",
                    restart_policy={
                        "enabled": True,
                        "ignored_exit_codes": [0, 143],
                        "attempt_period": 120,
                    },
                )
            },
        )
        assert "enabled              = true" in tf
        assert "ignoredExitCodes     = [0, 143]" in tf
        assert "restartAttemptPeriod = 120" in tf

    def test_enabled_false_renders_false(self, tmp_path):
        tf = _emit(
            tmp_path,
            {"web": _svc("web", image="w:1", restart_policy={"enabled": False})},
        )
        assert re.search(r"restartPolicy = \{\s*\n\s*enabled\s+= false", tf)

    def test_it_is_per_container_in_a_group(self, tmp_path):
        from remote_compose.config.v2_schema import TaskGroupV2

        services = {
            "nginx": _svc("nginx", image="n:1", public=True, port=80),
            "reingest": _svc(
                "reingest",
                image="r:1",
                essential=False,
                restart_policy={"enabled": True, "attempt_period": 60},
            ),
        }
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            DeployContext(
                project="app",
                compose_path=tmp_path / "docker-compose.yml",
                rc_yml_v2={},
                provider_config={
                    "ecs": {
                        "region": "us-west-2",
                        "cluster": "c",
                        "vpc_cidr": "10.0.0.0/16",
                    }
                },
                tf_backend_config={"type": "local"},
                working_dir=tmp_path,
                services=services,
                task_groups={
                    "nginx": TaskGroupV2(name="nginx", services=["nginx", "reingest"])
                },
                secrets=[],
            ),
            out,
        )
        tf = (out / "services.tf").read_text()
        # exactly one container carries it
        assert tf.count("restartPolicy = {") == 1
        assert "restartAttemptPeriod = 60" in tf


class TestEssentialFalseWithoutARestartPolicyWarns:
    """The pairing rc-ib01.4 asks for: a non-essential container with no restart
    policy is one nobody will notice dying."""

    def test_warned(self):
        from remote_compose.compose_warnings import (
            detect_non_essential_without_restart_policy,
        )

        raw = {
            "project": "app",
            "services": {"reingest": {"essential": False}},
        }
        out = detect_non_essential_without_restart_policy(raw)
        assert len(out) == 1
        assert "reingest" in out[0] and "restart_policy" in out[0]

    def test_not_warned_when_a_restart_policy_is_declared(self):
        from remote_compose.compose_warnings import (
            detect_non_essential_without_restart_policy,
        )

        raw = {
            "project": "app",
            "services": {
                "reingest": {"essential": False, "restart_policy": {"enabled": True}}
            },
        }
        assert detect_non_essential_without_restart_policy(raw) == []

    def test_not_warned_for_an_essential_container(self):
        from remote_compose.compose_warnings import (
            detect_non_essential_without_restart_policy,
        )

        raw = {"project": "app", "services": {"django": {"essential": True}}}
        assert detect_non_essential_without_restart_policy(raw) == []

    @pytest.mark.parametrize("raw", [None, {}, {"services": "junk"}, "junk"])
    def test_malformed_input_tolerated(self, raw):
        from remote_compose.compose_warnings import (
            detect_non_essential_without_restart_policy,
        )

        assert detect_non_essential_without_restart_policy(raw) == []
