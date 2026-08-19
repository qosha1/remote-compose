"""rc-hbjb: EC2 launch type silently downgraded a service's scratch space.

`ephemeral_storage` is a Fargate-only task field and rc correctly rejects it
on EC2. The bug was what happened next: the only way past the error was to
delete the setting, and capacity.tf.j2 declared no block_device_mappings, so
the instance took the ECS-optimized AMI's 30 GiB root volume and every
binpacked task on it shared that one disk. debuggai-api's django went from
40 GiB private to ~30 GiB shared with up to two neighbours, with a clean
plan and no warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import (
    ECS_AMI_DEFAULT_ROOT_VOLUME_GIB,
    ECS_AMI_ROOT_DEVICE_NAME,
    _resolve_root_volume_options,
)


def _ctx(tmp_path: Path, *, services=None, ec2_capacity=None) -> DeployContext:
    ecs: dict = {
        "region": "us-west-2",
        "cluster": "c",
        "vpc_cidr": "10.0.0.0/16",
        "default_launch_type": "EC2",
    }
    if ec2_capacity is not None:
        ecs["ec2_capacity"] = ec2_capacity
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services
        or {
            "django": ServiceSpec(name="django", cpu=512, memory=1024, replicas=1),
            "worker": ServiceSpec(name="worker", cpu=512, memory=1024, replicas=1),
        },
    )


class TestRootVolumeResolution:
    def test_unset_renders_no_block_device_mapping(self, tmp_path):
        """Byte-identical to pre-rc-hbjb behavior when the knob is absent."""
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(_ctx(tmp_path), out)
        assert "block_device_mappings" not in (out / "capacity.tf").read_text()

    def test_size_renders_the_mapping_on_the_amis_actual_root_device(self, tmp_path):
        """/dev/xvda, verified live against the AMI this template resolves.

        Any other device_name creates a SECOND, unmounted volume instead of
        resizing root — clean plan, real bill, bug unchanged.
        """
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, ec2_capacity={"root_volume_size": 100}), out
        )
        tf = (out / "capacity.tf").read_text()
        assert f'device_name = "{ECS_AMI_ROOT_DEVICE_NAME}"' in tf
        assert ECS_AMI_ROOT_DEVICE_NAME == "/dev/xvda"
        assert "volume_size           = 100" in tf
        assert 'volume_type           = "gp3"' in tf
        assert "delete_on_termination = true" in tf

    def test_volume_type_is_overridable(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                ec2_capacity={"root_volume_size": 60, "root_volume_type": "gp2"},
            ),
            out,
        )
        assert 'volume_type           = "gp2"' in (out / "capacity.tf").read_text()

    def test_encryption_defaults_on_and_is_overridable(self, tmp_path):
        out = tmp_path / "tf"
        ECSProvider().emit_terraform(
            _ctx(tmp_path, ec2_capacity={"root_volume_size": 60}), out
        )
        assert "encrypted             = true" in (out / "capacity.tf").read_text()

        out2 = tmp_path / "tf2"
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                ec2_capacity={"root_volume_size": 60, "root_volume_encrypted": False},
            ),
            out2,
        )
        assert "encrypted             = false" in (out2 / "capacity.tf").read_text()

    def test_defaults_when_unset(self):
        resolved = _resolve_root_volume_options({})
        assert resolved["root_volume_size"] is None
        assert resolved["root_volume_device"] == ECS_AMI_ROOT_DEVICE_NAME

    @pytest.mark.parametrize("bad", ["100", 12.5, True, [50]])
    def test_non_integer_size_rejected(self, bad):
        with pytest.raises(ProviderConfigError, match="must be an integer"):
            _resolve_root_volume_options({"root_volume_size": bad})

    def test_size_below_the_ami_snapshot_rejected(self):
        """AWS rejects a root volume smaller than the AMI snapshot outright."""
        with pytest.raises(ProviderConfigError, match="at least 30 GiB"):
            _resolve_root_volume_options({"root_volume_size": 20})

    def test_unknown_volume_type_rejected(self):
        with pytest.raises(ProviderConfigError, match="root_volume_type"):
            _resolve_root_volume_options(
                {"root_volume_size": 50, "root_volume_type": "gp9"}
            )


class TestEphemeralStorageErrorNamesTheAlternative:
    def test_rejection_points_at_root_volume_size(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            services={
                "django": ServiceSpec(
                    name="django", cpu=512, memory=1024, ephemeral_storage=40
                )
            },
        )
        with pytest.raises(ProviderConfigError) as exc:
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
        msg = str(exc.value)
        assert "ec2_capacity.root_volume_size" in msg
        # Names the real semantic difference, not just the substitute knob.
        assert "shared with every other task" in msg
        assert "40 GiB" in msg

    def test_fargate_service_still_accepts_ephemeral_storage(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            services={
                "django": ServiceSpec(
                    name="django",
                    cpu=512,
                    memory=1024,
                    launch_type="FARGATE",
                    ephemeral_storage=40,
                )
            },
        )
        ECSProvider().emit_terraform(ctx, tmp_path / "tf")  # must not raise


class TestSharedRootVolumeWarning:
    def test_warns_when_tasks_share_the_default_disk(self, tmp_path):
        """3 tasks over 2 instances packs 2 per box -> ~15 GiB of scratch each.

        Density is bounded by what the SIZED fleet actually packs, not by the
        shape's raw ENI ceiling: t3.xlarge could hold 3 awsvpc tasks, but with
        desired=2 the honest figure is ceil(3/2)=2 neighbours. Quoting the
        ceiling would overstate the squeeze — and badly so once ENI trunking
        raises that ceiling to 20 (rc-hguq).
        """
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            ec2_capacity={"instance_type": "t3.xlarge", "desired": 2, "max": 4},
            services={
                f"svc{i}": ServiceSpec(name=f"svc{i}", cpu=512, memory=1024)
                for i in range(3)
            },
        )
        provider.emit_terraform(ctx, tmp_path / "tf")
        [warning] = [w for w in provider._warnings if "root_volume_size" in w]
        assert str(ECS_AMI_DEFAULT_ROOT_VOLUME_GIB) in warning
        assert "15 GiB" in warning
        assert "takes its neighbours down" in warning

    def test_silent_once_root_volume_size_is_set(self, tmp_path):
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            ec2_capacity={
                "instance_type": "t3.xlarge",
                "desired": 2,
                "max": 4,
                "root_volume_size": 120,
            },
            services={
                f"svc{i}": ServiceSpec(name=f"svc{i}", cpu=512, memory=1024)
                for i in range(3)
            },
        )
        provider.emit_terraform(ctx, tmp_path / "tf")
        assert not [w for w in provider._warnings if "root_volume_size" in w]

    def test_silent_when_only_one_task_can_land_per_instance(self, tmp_path):
        """A private 30 GiB is not the hazard — sharing it is."""
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            ec2_capacity={"instance_type": "t3.xlarge", "desired": 1, "max": 2},
            services={"only": ServiceSpec(name="only", cpu=512, memory=1024)},
        )
        provider.emit_terraform(ctx, tmp_path / "tf")
        assert not [w for w in provider._warnings if "root_volume_size" in w]

    def test_unmodeled_instance_type_reports_nothing(self, tmp_path):
        """Same 'not modeled' convention as InstanceShape.max_enis=None."""
        provider = ECSProvider()
        ctx = _ctx(
            tmp_path,
            ec2_capacity={"instance_type": "r7iz.metal-32xl", "desired": 1, "max": 2},
            services={
                f"svc{i}": ServiceSpec(name=f"svc{i}", cpu=512, memory=1024)
                for i in range(3)
            },
        )
        provider.emit_terraform(ctx, tmp_path / "tf")
        assert not [w for w in provider._warnings if "root_volume_size" in w]
