"""rc-6akx: per-service rollout percentages (services.<svc>.deployment).

services.tf.j2 hardcoded ``deployment_minimum_healthy_percent = 100`` /
``deployment_maximum_percent = 200`` for every non-stateful service, so every
service briefly ran DOUBLE its tasks on each deploy. On EC2 that is not a
transient: rc-anl6 sizes the ASG for the roll, so the fleet carries the
doubling all month.

Measured on debuggai-api-prod 2026-08-23: 12 tasks reserving 5.00 vCPU /
25.8 GiB on 5x m5.xlarge (20 vCPU / 77 GiB registered) -- ~34% of memory,
with 15.50 vCPU / 51.9 GiB idle. The burst is concentrated in celery-worker
(3x3072) and celery-browser (3x4096), queue-backed workers behind no ALB,
where running 2 of 3 during a roll costs queue latency and not availability.

The 100% default is deliberate (see the healthCheck comment in
services.tf.j2) and stays. This is the knob, not a change of default:

  * no ``deployment:`` block  -> byte-identical terraform (test_golden.py)
  * stateless default         -> 100 / 200
  * stateful default          -> 0 / 100, and NOT overridable
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import ProviderConfigError
from remote_compose.provider.ecs import ECSProvider
from remote_compose.provider.ecs.provider import _deployment_percents

IN_PLACE = {"minimum_healthy_percent": 50, "maximum_percent": 100}


def _ctx(tmp_path: Path, services, ecs_over=None) -> DeployContext:
    ecs: dict = {
        "region": "us-west-2",
        "cluster": "app-cluster",
        "vpc_cidr": "10.0.0.0/16",
    }
    ecs.update(ecs_over or {})
    return DeployContext(
        project="app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=[],
    )


def _services_tf(tmp_path, services, ecs_over=None, name="tf") -> str:
    out = tmp_path / name
    ECSProvider().emit_terraform(_ctx(tmp_path, services, ecs_over), out)
    return (out / "services.tf").read_text()


def _assignments(block: str, argument: str) -> int:
    """How many times an argument is actually ASSIGNED in a block. Counting
    substrings would also count the rationale comments, and terraform only
    refuses to parse on a duplicate assignment."""
    return sum(
        1
        for line in block.splitlines()
        if line.strip().startswith(f"{argument} ")
        and "=" in line
        and not line.strip().startswith("#")
    )


def _block(tf: str, service: str) -> str:
    """The aws_ecs_service block for one service, so a percentage assertion
    can't accidentally match a sibling's."""
    marker = f'resource "aws_ecs_service" "{service}"'
    start = tf.index(marker)
    end = tf.find('\nresource "', start + 1)
    return tf[start : end if end != -1 else len(tf)]


class TestDefaultsAreUnchanged:
    """The property that matters most: nobody's stack moves without an edit."""

    def test_stateless_service_still_renders_100_200(self, tmp_path):
        tf = _services_tf(
            tmp_path, {"w": ServiceSpec(name="w", cpu=256, memory=512, replicas=3)}
        )
        assert "deployment_minimum_healthy_percent = 100" in tf
        assert "deployment_maximum_percent         = 200" in tf

    def test_stateless_service_keeps_the_zero_downtime_rationale(self, tmp_path):
        """The comment explains WHY 100% is deliberate (ECS holds old tasks
        until new ones pass their healthCheck, so workers behind no ALB get a
        zero-downtime roll too). Losing it would make the default look like an
        oversight the next time someone reads this file."""
        tf = _services_tf(tmp_path, {"w": ServiceSpec(name="w", cpu=256, memory=512)})
        assert "# Zero-downtime rolling deploy: keep 100% of old tasks" in tf
        assert "allowing up to 200% during the roll" in tf

    def test_stateful_service_still_renders_0_100(self, tmp_path):
        tf = _services_tf(
            tmp_path,
            {
                "db": ServiceSpec(
                    name="db",
                    cpu=256,
                    memory=512,
                    volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}],
                )
            },
        )
        assert "deployment_minimum_healthy_percent = 0" in tf
        assert "deployment_maximum_percent         = 100" in tf
        assert 'availability_zone_rebalancing = "DISABLED"' in tf

    def test_resolver_returns_the_historical_literals(self):
        assert _deployment_percents("w", ServiceSpec(name="w", cpu=1, memory=1)) == (
            100,
            200,
            False,
        )
        assert _deployment_percents(
            "celery-beat", ServiceSpec(name="celery-beat", cpu=1, memory=1)
        ) == (0, 100, False)

    def test_empty_mapping_is_not_an_override(self):
        """`deployment: {}` in YAML (a key with nothing under it) must not be
        read as "override with nothing" -- it is a no-op."""
        spec = ServiceSpec(name="w", cpu=1, memory=1, deployment={})
        assert _deployment_percents("w", spec) == (100, 200, False)


class TestOverrideIsApplied:
    def test_percentages_reach_the_terraform(self, tmp_path):
        tf = _services_tf(
            tmp_path,
            {
                "worker": ServiceSpec(
                    name="worker",
                    cpu=256,
                    memory=512,
                    replicas=3,
                    deployment=IN_PLACE,
                ),
                "web": ServiceSpec(name="web", cpu=256, memory=512, replicas=2),
            },
        )
        worker = _block(tf, "worker")
        assert "deployment_minimum_healthy_percent = 50" in worker
        assert "deployment_maximum_percent         = 100" in worker
        # ...and only that service.
        web = _block(tf, "web")
        assert "deployment_minimum_healthy_percent = 100" in web
        assert "deployment_maximum_percent         = 200" in web

    def test_circuit_breaker_survives_the_override(self, tmp_path):
        """Auto-rollback of a deploy whose tasks never go healthy is not part
        of the capacity trade -- it must not silently drop out."""
        worker = _block(
            _services_tf(
                tmp_path,
                {
                    "worker": ServiceSpec(
                        name="worker",
                        cpu=256,
                        memory=512,
                        replicas=3,
                        deployment=IN_PLACE,
                    )
                },
            ),
            "worker",
        )
        assert "deployment_circuit_breaker {" in worker
        assert "rollback = true" in worker


class TestAvailabilityZoneRebalancingConflict:
    """ECS rejects availability_zone_rebalancing alongside maximumPercent
    <= 100, and NOT rendering the argument does not mean "off" -- ECS defaults
    it to ENABLED on create and keeps the live value on update. rc-5a4g learned
    this the hard way on debuggai-api (6 of 7 services 400'd on the FARGATE ->
    EC2 apply). maximum_percent: 100 walks into the same conflict."""

    def _svc(self, deployment):
        return {
            "worker": ServiceSpec(
                name="worker",
                cpu=256,
                memory=512,
                replicas=3,
                deployment=deployment,
            )
        }

    def test_fargate_in_place_roll_pins_it_off(self, tmp_path):
        worker = _block(
            _services_tf(
                tmp_path, self._svc(IN_PLACE), {"default_launch_type": "FARGATE"}
            ),
            "worker",
        )
        assert 'launch_type     = "FARGATE"' in worker
        assert 'availability_zone_rebalancing = "DISABLED"' in worker

    def test_fargate_default_is_untouched(self, tmp_path):
        """Rendered only when the percentages require it — an existing Fargate
        service must not silently have this argument appear on it."""
        worker = _block(
            _services_tf(
                tmp_path, self._svc(None), {"default_launch_type": "FARGATE"}, "tf2"
            ),
            "worker",
        )
        assert _assignments(worker, "availability_zone_rebalancing") == 0

    def test_ec2_pins_it_exactly_once(self, tmp_path):
        """The EC2 branch already pins it (binpack requires it). Two renderings
        would be a duplicate argument and terraform would refuse to parse."""
        worker = _block(
            _services_tf(
                tmp_path, self._svc(IN_PLACE), {"default_launch_type": "EC2"}, "tf3"
            ),
            "worker",
        )
        assert _assignments(worker, "availability_zone_rebalancing") == 1

    def test_stateful_fargate_pins_it_exactly_once(self, tmp_path):
        """The stateful branch pins it for its own maximumPercent reason."""
        worker = _block(
            _services_tf(
                tmp_path,
                {
                    "db": ServiceSpec(
                        name="db",
                        cpu=256,
                        memory=512,
                        volumes=[{"name": "pgdata", "mount": "/data"}],
                    )
                },
                {"default_launch_type": "FARGATE"},
                "tf4",
            ),
            "db",
        )
        assert _assignments(worker, "availability_zone_rebalancing") == 1

    def test_overridden_service_gets_the_trade_off_explained(self, tmp_path):
        """The default comment claims 100%/200% behaviour; leaving it on an
        overridden service would document the opposite of what applies."""
        worker = _block(
            _services_tf(
                tmp_path,
                {
                    "worker": ServiceSpec(
                        name="worker",
                        cpu=256,
                        memory=512,
                        replicas=3,
                        deployment=IN_PLACE,
                    )
                },
            ),
            "worker",
        )
        assert "# Zero-downtime rolling deploy" not in worker
        assert "services.worker.deployment" in worker

    def test_partial_override_keeps_the_other_default(self):
        spec = ServiceSpec(
            name="w",
            cpu=1,
            memory=1,
            replicas=4,
            deployment={"minimum_healthy_percent": 50},
        )
        assert _deployment_percents("w", spec) == (50, 200, True)


class TestFleetSizing:
    """The payoff. Rendering '100' proves nothing about the bill -- the ASG
    has to actually get smaller."""

    def _desired(self, tmp_path, deployment, name):
        services = {
            "worker": ServiceSpec(
                name="worker",
                cpu=512,
                memory=2048,
                replicas=3,
                launch_type="EC2",
                deployment=deployment,
            )
        }
        out = tmp_path / name
        ECSProvider().emit_terraform(
            _ctx(
                tmp_path,
                services,
                {
                    "default_launch_type": "EC2",
                    "ec2_capacity": {
                        "size_for_rolling_deploy": True,
                        "max": 20,
                    },
                },
            ),
            out,
        )
        tf = (out / "capacity.tf").read_text()
        line = next(x for x in tf.splitlines() if "desired_capacity" in x)
        return int(line.split("=")[1].strip())

    def test_in_place_roll_shrinks_the_autosized_asg(self, tmp_path):
        """With maximum_percent=100, peak_replicas collapses to replicas, so
        a fleet sized for the roll is a fleet sized for steady state. This is
        the whole reason the knob exists."""
        default = self._desired(tmp_path, None, "tf-default")
        override = self._desired(tmp_path, IN_PLACE, "tf-override")
        assert override < default, (
            f"maximum_percent=100 did not reach the ASG sizer: "
            f"desired stayed {default} -> {override}"
        )

    def test_pending_window_warning_stops_firing(self, tmp_path):
        """rc warns that a steady-state-sized fleet must scale out mid-deploy
        with tasks PENDING. An in-place roll removes the condition, so the
        warning must go quiet rather than nag about a peak that no longer
        exists."""

        def warnings(deployment, name):
            provider = ECSProvider()
            provider.emit_terraform(
                _ctx(
                    tmp_path,
                    {
                        "django": ServiceSpec(
                            name="django",
                            cpu=1024,
                            memory=2048,
                            replicas=2,
                            deployment=deployment,
                        ),
                        "worker": ServiceSpec(
                            name="worker",
                            cpu=1024,
                            memory=2048,
                            replicas=3,
                            deployment=deployment,
                        ),
                    },
                    {
                        "default_launch_type": "EC2",
                        "ec2_capacity": {
                            "instance_type": "t3.large",
                            "desired": 3,
                            "max": 6,
                        },
                    },
                ),
                tmp_path / name,
            )
            return provider._warnings

        assert [w for w in warnings(None, "tf-a") if "PENDING" in w]
        assert not [w for w in warnings(IN_PLACE, "tf-b") if "PENDING" in w]


class TestNoStateRollMirrorsIt:
    """rc-usk0's lesson: the --no-state force-roll writes
    deploymentConfiguration onto the LIVE service on every deploy. A literal
    100/200 there silently reverts the rc.yml override, and terraform is
    bypassed so nothing puts it back."""

    def _update_kwargs(self, services, names):
        provider = ECSProvider()
        client = MagicMock()
        session = MagicMock()
        session.client.return_value = client
        ctx = DeployContext(
            project="app",
            compose_path=Path("/tmp/docker-compose.yml"),
            rc_yml_v2={},
            provider_config={"ecs": {"region": "us-west-2", "cluster": "app-cluster"}},
            tf_backend_config={"type": "local"},
            working_dir=Path("/tmp"),
            services=services,
            secrets=[],
        )
        with (
            patch.object(provider, "session_factory", lambda _c: session),
            patch.object(provider, "_watch_post_rollout_errors", lambda *a, **k: None),
        ):
            provider._force_new_deployments(ctx, names)
        return {
            c.kwargs["service"]: c.kwargs for c in client.update_service.call_args_list
        }

    def test_default_roll_is_unchanged(self):
        kw = self._update_kwargs(
            {"w": ServiceSpec(name="w", cpu=1, memory=1, replicas=2)}, ["w"]
        )
        assert kw["w"]["deploymentConfiguration"]["minimumHealthyPercent"] == 100
        assert kw["w"]["deploymentConfiguration"]["maximumPercent"] == 200

    def test_override_is_carried_into_the_live_service(self):
        kw = self._update_kwargs(
            {
                "w": ServiceSpec(
                    name="w", cpu=1, memory=1, replicas=3, deployment=IN_PLACE
                )
            },
            ["w"],
        )
        assert kw["w"]["deploymentConfiguration"]["minimumHealthyPercent"] == 50
        assert kw["w"]["deploymentConfiguration"]["maximumPercent"] == 100

    def test_stateful_service_still_sends_no_deployment_config(self):
        kw = self._update_kwargs(
            {"celery-beat": ServiceSpec(name="celery-beat", cpu=1, memory=1)},
            ["celery-beat"],
        )
        assert "deploymentConfiguration" not in kw["celery-beat"]


class TestValidationRejectsBadInput:
    def _err(self, deployment, **spec_kw):
        spec = ServiceSpec(name="w", cpu=1, memory=1, deployment=deployment, **spec_kw)
        with pytest.raises(ProviderConfigError) as exc:
            _deployment_percents("w", spec)
        return str(exc.value)

    def test_non_mapping(self):
        assert "must be a mapping" in self._err(50)

    def test_unknown_key_is_not_silently_ignored(self):
        """Services have no unknown-key rejection at the config layer, so a
        typo would otherwise parse fine and do nothing at all."""
        msg = self._err({"minimum_health_percent": 50, "maximum_percent": 100})
        assert "unknown key" in msg
        assert "minimum_health_percent" in msg

    @pytest.mark.parametrize("bad", ["50", 50.0, None, [50]])
    def test_non_integer(self, bad):
        assert "must be an integer" in self._err({"minimum_healthy_percent": bad})

    def test_yaml_boolean_is_rejected_not_coerced(self):
        """`minimum_healthy_percent: yes` parses to True, and bool is a
        subclass of int -- an isinstance check would let it through as 1."""
        assert "must be an integer" in self._err({"minimum_healthy_percent": True})

    @pytest.mark.parametrize("bad", [-1, 101, 200])
    def test_minimum_out_of_range(self, bad):
        assert "between 0 and 100" in self._err({"minimum_healthy_percent": bad})

    @pytest.mark.parametrize("bad", [0, 50, 99])
    def test_maximum_below_100(self, bad):
        assert "at least 100" in self._err(
            {"minimum_healthy_percent": 0, "maximum_percent": bad}
        )

    def test_100_100_cannot_roll_at_any_replica_count(self):
        """ECS must keep replicas healthy while running at most replicas, so it
        can neither start a replacement nor stop an old task. Not a 1-replica
        special case -- it deadlocks at 3 too."""
        msg = self._err(
            {"minimum_healthy_percent": 100, "maximum_percent": 100}, replicas=3
        )
        assert "can never roll" in msg
        assert "replicas=3" in msg

    def test_50_100_cannot_roll_at_one_replica(self):
        """minimumHealthyPercent rounds UP: 50% of 1 is still 1 task that must
        stay healthy, with room for exactly 1. Only 0 works at replicas=1."""
        msg = self._err(IN_PLACE, replicas=1)
        assert "can never roll" in msg

    def test_50_100_is_fine_at_three_replicas(self):
        spec = ServiceSpec(name="w", cpu=1, memory=1, replicas=3, deployment=IN_PLACE)
        assert _deployment_percents("w", spec) == (50, 100, True)

    def test_max_percent_alone_is_caught(self):
        """`maximum_percent: 100` on its own leaves minimum at its 100 default
        -- the deadlock, arrived at by omission rather than by typing it."""
        assert "can never roll" in self._err({"maximum_percent": 100}, replicas=3)

    def test_the_error_names_a_value_that_would_work(self):
        msg = self._err(
            {"minimum_healthy_percent": 100, "maximum_percent": 100}, replicas=4
        )
        assert "75 is the highest" in msg

    def test_scaled_to_zero_service_is_not_a_deadlock(self):
        """replicas=0 has no roll to deadlock; raising there would break a
        legitimate scaled-to-zero config."""
        spec = ServiceSpec(name="w", cpu=1, memory=1, replicas=0, deployment=IN_PLACE)
        assert _deployment_percents("w", spec) == (50, 100, True)

    def test_the_error_surfaces_through_emit(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="can never roll"):
            _services_tf(
                tmp_path,
                {
                    "w": ServiceSpec(
                        name="w",
                        cpu=256,
                        memory=512,
                        replicas=2,
                        deployment={
                            "minimum_healthy_percent": 100,
                            "maximum_percent": 100,
                        },
                    )
                },
            )


class TestStatefulServicesRejectTheOverride:
    """Stop-then-start is a data-integrity guarantee, not a default. Two tasks
    on one EFS access point means postgres initdb can wipe a directory the
    outgoing task still holds, so any percentage permitting overlap undoes
    precisely what the branch exists for. Reject loudly rather than accept and
    ignore -- an ignored knob reads as a broken feature."""

    def _err(self, name, **spec_kw):
        spec = ServiceSpec(name=name, cpu=1, memory=1, deployment=IN_PLACE, **spec_kw)
        with pytest.raises(ProviderConfigError) as exc:
            _deployment_percents(name, spec)
        return str(exc.value)

    def test_efs_mount(self):
        msg = self._err(
            "db", volumes=[{"name": "pgdata", "mount": "/var/lib/postgresql/data"}]
        )
        assert "mounts 1 EFS volume(s)" in msg

    def test_explicit_stateful_flag(self):
        assert "stateful: true" in self._err("redis", stateful=True)

    def test_inferred_singleton_scheduler_says_so(self):
        """Statefulness is INFERRED here. A service called `report-scheduler`
        with no volume at all must not be handed an EFS lecture."""
        msg = self._err("report-scheduler")
        assert "singleton scheduler" in msg
        assert "EFS" not in msg

    def test_emit_raises_rather_than_silently_pinning(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="stop-then-start"):
            _services_tf(
                tmp_path,
                {
                    "db": ServiceSpec(
                        name="db",
                        cpu=256,
                        memory=512,
                        deployment=IN_PLACE,
                        volumes=[{"name": "pgdata", "mount": "/data"}],
                    )
                },
            )


class TestConfigLayerWiring:
    def test_rc_yml_deployment_block_reaches_the_service_spec(self):
        from remote_compose.config._schema_parser import _parse_service

        svc = _parse_service(
            "worker",
            {
                "cpu": 256,
                "memory": 512,
                **{
                    "deployment": {
                        "minimum_healthy_percent": 50,
                        "maximum_percent": 100,
                    }
                },
            },
        )
        assert svc.deployment == IN_PLACE

    def test_absent_block_is_none(self):
        from remote_compose.config._schema_parser import _parse_service

        assert _parse_service("worker", {"cpu": 256, "memory": 512}).deployment is None
