"""rc-1ce9: every rc.yml service field must survive build_deploy_context.

``build_deploy_context`` rebuilds each rc.yml ``ServiceV2`` into a provider
``ServiceSpec`` by naming every field by hand. That hand-written kwarg list is
the bug surface: a field added to the parser AND the renderer, but not to the
list, is parsed correctly, rendered correctly, and silently lost in between.
``ServiceSpec``'s dataclass default then stands in for the user's value, so
there is no error, no warning, and terraform reports "No changes".

This has now shipped three times:

  * ``default_target``   — ALB catch-all fell to the alphabetically-first
                           public service (see test_cli_v2.py).
  * ``essential``        — rc-m2sn. A grouped task rendered essential=true on
                           every container, so a daily cron loop exiting took
                           nginx + django + frontend down with it.
  * ``restart_policy``   — rc-ib01.4. Never reached a task definition at all.

Two point-fix regression tests did not stop the third, so the guard below is
structural: it fails when a NEW ``ServiceV2`` field is added without a decision
about forwarding, rather than waiting for someone to hit it in production.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
from remote_compose.config._schema_types import ServiceV2
from remote_compose.provider.base import ServiceSpec

# ServiceV2 field -> ServiceSpec field, where the two names differ. A plain
# set difference cannot see a rename: the field looks absent from ServiceSpec
# and absent from the kwarg list, which is indistinguishable from a drop.
# Spell renames out so the guard keeps working across them.
RENAMED_ON_SERVICE_SPEC = {
    "subnets": "subnet_group",
}

# ServiceV2 fields that deliberately do NOT reach ServiceSpec, with the reason.
# Adding a field here is the explicit "not forwarded, on purpose" decision the
# guard exists to force.
NOT_FORWARDED = {
    # Consumed during context build to pick framework-aware lifecycle hooks and
    # domain env vars; the provider never needs the raw declaration.
    "framework": "consumed by _merge_framework_lifecycle / domain_env at build time",
    # ServiceSpec.name is positional-by-key in the services dict, and IS passed.
    # (kept out of the map only because it is never absent)
}


def _service_spec_kwargs_in_build_deploy_context() -> set[str]:
    """Kwarg names on the rc.yml-declared ServiceSpec(...) call in cli_v2.

    Reads the source rather than calling the function because the point is to
    check the *list itself* is complete, independently of whether any given
    fixture happens to exercise a field.
    """
    src = Path(build_deploy_context.__code__.co_filename).read_text()
    tree = ast.parse(src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "ServiceSpec"
    ]
    assert calls, "no ServiceSpec(...) call found in cli_v2 — did it move?"
    # Two call sites: the rc.yml-declared branch and the compose-only branch.
    # The compose-only branch has no ServiceV2 to forward from, so the branch
    # under test is the one naming the most fields.
    richest = max(calls, key=lambda c: len(c.keywords))
    return {kw.arg for kw in richest.keywords if kw.arg}


class TestEveryRcYmlFieldIsForwarded:
    def test_no_service_v2_field_is_silently_dropped(self):
        """The guard. Fails when a ServiceV2 field reaches neither the
        ServiceSpec kwarg list nor an explicit exemption above."""
        forwarded = _service_spec_kwargs_in_build_deploy_context()
        spec_fields = {f.name for f in dataclasses.fields(ServiceSpec)}

        dropped = []
        for f in dataclasses.fields(ServiceV2):
            name = f.name
            if name in NOT_FORWARDED:
                continue
            target = RENAMED_ON_SERVICE_SPEC.get(name, name)
            if target not in spec_fields:
                dropped.append(
                    f"{name!r}: no ServiceSpec field {target!r} — add it to "
                    f"RENAMED_ON_SERVICE_SPEC or NOT_FORWARDED"
                )
                continue
            if target not in forwarded:
                dropped.append(
                    f"{name!r}: parsed into ServiceV2 but never passed to "
                    f"ServiceSpec(...) in build_deploy_context, so "
                    f"ServiceSpec.{target}'s default silently replaces the "
                    f"user's value (this is rc-1ce9)"
                )

        assert (
            not dropped
        ), "rc.yml service field(s) dropped between parse and emit:\n  " + "\n  ".join(
            dropped
        )

    def test_exemptions_still_refer_to_real_fields(self):
        """Keeps the two maps above honest — a stale entry would silently
        exempt a field that no longer exists while masking one that does."""
        v2_fields = {f.name for f in dataclasses.fields(ServiceV2)}
        stale = (set(NOT_FORWARDED) | set(RENAMED_ON_SERVICE_SPEC)) - v2_fields
        assert not stale, f"exemption(s) name fields ServiceV2 no longer has: {stale}"


class TestEssentialAndRestartPolicyReachTheSpec:
    """rc-1ce9 end-to-end: the two fields the guard was written for."""

    def _ctx(self, tmp_path, monkeypatch, service_body):
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n"
            "  nginx:\n    image: nginx:latest\n"
            "  reingest:\n    image: busybox:latest\n"
        )
        rc = tmp_path / "rc.yml"
        rc.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "project": "p",
                    "compose_file": "docker-compose.yml",
                    "provider": "fake",
                    "services": {
                        "nginx": {
                            "cpu": 256,
                            "memory": 512,
                            "public": True,
                            "port": 80,
                        },
                        "reingest": service_body,
                    },
                }
            )
        )
        monkeypatch.chdir(tmp_path)
        _version, raw, v2 = load_rc_yml(rc)
        return build_deploy_context(v2, raw, rc)

    def test_essential_false_survives(self, tmp_path, monkeypatch):
        # The production failure: the service is declared in BOTH the compose
        # file and rc.yml, and `essential` exists only in rc.yml. Before the
        # fix this asserted True — task defs shipped with every container
        # essential, so a cron loop exiting killed the whole tenant task.
        ctx = self._ctx(
            tmp_path, monkeypatch, {"cpu": 256, "memory": 512, "essential": False}
        )
        assert ctx.services["reingest"].essential is False
        # Unset stays at ECS's own default, so existing task defs don't move.
        assert ctx.services["nginx"].essential is True

    def test_restart_policy_survives(self, tmp_path, monkeypatch):
        ctx = self._ctx(
            tmp_path,
            monkeypatch,
            {
                "cpu": 256,
                "memory": 512,
                "restart_policy": {"enabled": True, "ignored_exit_codes": [0]},
            },
        )
        rp = ctx.services["reingest"].restart_policy
        assert rp is not None, "restart_policy dropped between parse and emit"
        assert rp["enabled"] is True
        assert rp["ignored_exit_codes"] == [0]
        # Absent block emits nothing, so no already-deployed task def changes.
        assert ctx.services["nginx"].restart_policy is None


class TestEssentialReachesRenderedTerraform:
    """The layer the bead actually measured: the emitted task definition.

    The unit tests above stop at ServiceSpec. This one renders terraform, so
    it fails if the loss moves somewhere else in the chain later.
    """

    def test_emitted_task_def_carries_essential_false(self, tmp_path, monkeypatch):
        pytest.importorskip("jinja2")
        from remote_compose.provider.ecs.provider import ECSProvider

        (tmp_path / "docker-compose.yml").write_text(
            "services:\n"
            "  nginx:\n    image: nginx:latest\n"
            "  reingest:\n    image: busybox:latest\n"
        )
        rc = tmp_path / "rc.yml"
        # The shape from the field report: nginx and reingest share ONE task,
        # and reingest is the non-essential member. `essential: false` on a
        # LONE service is rejected (a task of one must have an essential
        # container) — that rejection only became reachable once the field
        # started arriving, so it is exercised separately below.
        rc.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "project": "p",
                    "compose_file": "docker-compose.yml",
                    "provider": "ecs",
                    "provider_config": {
                        "ecs": {"region": "us-west-2", "launch_type": "FARGATE"}
                    },
                    "services": {
                        "nginx": {
                            "cpu": 256,
                            "memory": 512,
                            "public": True,
                            "port": 80,
                        },
                        "reingest": {"cpu": 256, "memory": 512, "essential": False},
                    },
                    "task_groups": {"web": {"services": ["nginx", "reingest"]}},
                    "terraform": {"backend": {"type": "local"}},
                }
            )
        )
        monkeypatch.chdir(tmp_path)
        _version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)

        out = tmp_path / "tf"
        ECSProvider().emit_terraform(ctx, out)
        services_tf = (out / "services.tf").read_text()
        assert "essential = false" in services_tf, (
            "rendered terraform has no `essential = false` — the rc.yml value "
            "was lost somewhere between parse and render (rc-1ce9)"
        )

    def test_lone_non_essential_service_is_now_rejected(self, tmp_path, monkeypatch):
        """Behavior change on upgrade, recorded deliberately.

        ``essential: false`` on an UNGROUPED service is a task whose only
        container is non-essential, which AWS rejects. rc has always had the
        check (validate_task_groups), but it could never fire while the field
        was being dropped — so a stack that wrote it got a silent no-op. It
        now fails at plan time instead of registering a task definition AWS
        would refuse. Anyone upgrading with that config sees a new error, and
        the error is the correct answer.
        """
        pytest.importorskip("jinja2")
        from remote_compose.provider.base import ProviderConfigError
        from remote_compose.provider.ecs.provider import ECSProvider

        (tmp_path / "docker-compose.yml").write_text(
            "services:\n  reingest:\n    image: busybox:latest\n"
        )
        rc = tmp_path / "rc.yml"
        rc.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "project": "p",
                    "compose_file": "docker-compose.yml",
                    "provider": "ecs",
                    "provider_config": {
                        "ecs": {"region": "us-west-2", "launch_type": "FARGATE"}
                    },
                    "services": {
                        "reingest": {"cpu": 256, "memory": 512, "essential": False}
                    },
                    "terraform": {"backend": {"type": "local"}},
                }
            )
        )
        monkeypatch.chdir(tmp_path)
        _version, raw, v2 = load_rc_yml(rc)
        ctx = build_deploy_context(v2, raw, rc)
        with pytest.raises(ProviderConfigError, match="at least one essential"):
            ECSProvider().emit_terraform(ctx, tmp_path / "tf")
