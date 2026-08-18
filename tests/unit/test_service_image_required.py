"""Every deployed service must resolve to an image rc can actually pull.

rc-2r1r, the sibling of startsim-wxb7. wxb7 closed the route where a missing
or service-less ``compose_file`` left *every* service with no image. This is
the one it left open: rc.yml may declare a service that compose does not have
(``test_rc_yml_service_not_in_compose_still_deploys`` pins that capability),
but ``ServiceV2`` had no ``image`` field — so there was no way to say what that
service should run. services.tf.j2's final ``{%- else %}`` branch then emitted
``image = "${aws_ecr_repository.<svc>.repository_url}:latest"``, and the
neighbouring ``owns_image_repo`` block created that repo empty. Terraform
applied clean; ECS then failed to pull, forever.

The fix is the field the capability always implied: ``services.<svc>.image``.
With it declared, a compose-less service renders its image verbatim. Without
it — and with nothing in compose either — rc stops at context build instead of
emitting terraform for a container that cannot start.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
from remote_compose.config._errors import ConfigError
from remote_compose.provider.ecs import ECSProvider

COMPOSE = "services:\n  api:\n    image: busybox\n"

RC_YML = {
    "version": 2,
    "project": "r2r1r",
    "compose_file": "docker-compose.yml",
    "provider": "ecs",
    "provider_config": {
        "ecs": {"region": "us-west-2", "cluster": "r2r1r-cluster"},
    },
    "services": {},
    "terraform": {"backend": {"type": "local"}},
}


def _write(tmp_path: Path, services: dict, *, compose: str | None = COMPOSE) -> Path:
    if compose is not None:
        (tmp_path / "docker-compose.yml").write_text(compose)
    cfg = dict(RC_YML)
    cfg["services"] = services
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(cfg))
    return p


REDIS = {"cpu": 256, "memory": 512, "type": "infrastructure"}


class TestRcYmlServiceCanDeclareAnImage:
    def test_declared_image_reaches_the_spec(self, tmp_path):
        p = _write(tmp_path, {"redis": {**REDIS, "image": "redis:7-alpine"}})
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p)

        assert ctx.services["redis"].image == "redis:7-alpine"
        assert ctx.services["redis"].build_context is None

    def test_declared_image_renders_verbatim_with_no_ecr_placeholder(self, tmp_path):
        """The end-to-end shape of the bug, inverted.

        Before the field existed this same rc.yml rendered an ECR tag rc never
        pushes. Both halves are asserted: the real image is in the task def,
        and no ``repository_url}:latest`` is anywhere in the emitted stack.
        """
        p = _write(tmp_path, {"redis": {**REDIS, "image": "redis:7-alpine"}})
        _, raw, v2 = load_rc_yml(p)
        out = tmp_path / "tf"

        ECSProvider().emit_terraform(build_deploy_context(v2, raw, p), out)

        services_tf = (out / "services.tf").read_text()
        assert '"redis:7-alpine"' in services_tf
        bogus = [f for f in out.glob("*.tf") if "repository_url}:latest" in f.read_text()]
        assert not bogus, (
            "emitted an ECR tag rc never builds or pushes: "
            f"{[str(f) for f in bogus]}"
        )

    def test_compose_only_services_are_untouched(self, tmp_path):
        """The field is additive: omitting it changes nothing."""
        p = _write(tmp_path, {})
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p)

        assert ctx.services["api"].image == "busybox"


class TestRcYmlImageWinsOverCompose:
    """Same precedence as ``services.<svc>.dockerfile`` (rc-e5u.46.1): the
    rc.yml layer overrides compose so users can point rc at a pre-built image
    without editing docker-compose.yml.

    Clearing the build context is the load-bearing half. services.tf.j2 tests
    ``has_build_context`` *first*, so leaving it set would make rc build and
    push to ECR while the declared image was silently ignored — the same class
    of defect this bead exists to close, just with a different empty tag.
    """

    def test_declared_image_beats_a_compose_build_context(self, tmp_path):
        p = _write(
            tmp_path,
            {"api": {"cpu": 256, "memory": 512, "image": "ghcr.io/acme/api:v9"}},
            compose="services:\n  api:\n    build: .\n",
        )
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p)

        assert ctx.services["api"].image == "ghcr.io/acme/api:v9"
        assert ctx.services["api"].build_context is None

    def test_that_image_is_what_gets_rendered(self, tmp_path):
        p = _write(
            tmp_path,
            {"api": {"cpu": 256, "memory": 512, "image": "ghcr.io/acme/api:v9"}},
            compose="services:\n  api:\n    build: .\n",
        )
        _, raw, v2 = load_rc_yml(p)
        out = tmp_path / "tf"

        ECSProvider().emit_terraform(build_deploy_context(v2, raw, p), out)

        assert '"ghcr.io/acme/api:v9"' in (out / "services.tf").read_text()

    def test_compose_image_still_applies_when_rc_yml_is_silent(self, tmp_path):
        p = _write(tmp_path, {"api": {"cpu": 1024, "memory": 2048}})
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p)

        assert ctx.services["api"].image == "busybox"
        assert ctx.services["api"].cpu == 1024


class TestUnresolvableImageIsAnError:
    def test_compose_less_service_without_an_image(self, tmp_path):
        p = _write(tmp_path, {"redis": REDIS})
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError) as exc:
            build_deploy_context(v2, raw, p)

        msg = str(exc.value)
        # Name the service, the field that fixes it, and the file it's missing
        # from — the three things the user needs to act.
        assert "redis" in msg
        assert "image" in msg
        assert "docker-compose.yml" in msg

    def test_every_unresolvable_service_is_named_at_once(self, tmp_path):
        """One error listing all of them, not one error per run."""
        p = _write(tmp_path, {"redis": REDIS, "memcached": REDIS})
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError) as exc:
            build_deploy_context(v2, raw, p)

        assert "memcached" in str(exc.value)
        assert "redis" in str(exc.value)

    def test_no_terraform_is_emitted(self, tmp_path):
        p = _write(tmp_path, {"redis": REDIS})
        _, raw, v2 = load_rc_yml(p)
        out = tmp_path / "tf"

        with pytest.raises(ConfigError):
            ECSProvider().emit_terraform(build_deploy_context(v2, raw, p), out)

        assert not (list(out.glob("*.tf")) if out.exists() else [])

    def test_an_excluded_service_is_not_checked(self, tmp_path):
        """The guard runs on the deploy set, not on everything declared."""
        cfg = dict(RC_YML)
        cfg["services"] = {"redis": REDIS}
        cfg["compose"] = {"exclude": ["redis"]}
        (tmp_path / "docker-compose.yml").write_text(COMPOSE)
        p = tmp_path / "rc.yml"
        p.write_text(yaml.safe_dump(cfg))
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p)

        assert "redis" not in ctx.services
        assert "api" in ctx.services

    def test_a_build_context_alone_is_resolvable(self, tmp_path):
        """No image anywhere is fine when rc is the one building it."""
        p = _write(
            tmp_path,
            {"api": {"cpu": 256, "memory": 512}},
            compose="services:\n  api:\n    build: .\n",
        )
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p)

        assert ctx.services["api"].build_context is not None


class TestLiveStackCommandsAreUnaffected:
    """Mirrors the wxb7 carve-out, and for the same reason: destroy and status
    derive nothing durable from images. A stack whose compose file is already
    gone would otherwise be undestroyable — strictly worse than the bug.
    """

    def test_build_deploy_context_opt_out(self, tmp_path):
        p = _write(tmp_path, {"redis": REDIS})
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p, require_resolvable_images=False)

        assert "redis" in ctx.services

    def test_destroy_dispatches_with_an_unresolvable_service(self, tmp_path, monkeypatch):
        from remote_compose import cli_v2

        cfg = dict(RC_YML)
        cfg["services"] = {"redis": REDIS}
        cfg["provider"] = "fake"
        (tmp_path / "docker-compose.yml").write_text(COMPOSE)
        p = tmp_path / "rc.yml"
        p.write_text(yaml.safe_dump(cfg))
        destroyed = []

        class _FakeProvider:
            def destroy(self, ctx):
                destroyed.append(ctx.project)

        monkeypatch.setattr(cli_v2, "resolve_provider", lambda *a, **k: _FakeProvider())
        assert cli_v2.dispatch_if_v2(p, "destroy", yes=True) is True
        assert destroyed == ["r2r1r"]

    def test_plan_still_errors(self, tmp_path, monkeypatch):
        """A plan for a container that cannot start is not a useful plan."""
        import click

        from remote_compose import cli_v2

        cfg = dict(RC_YML)
        cfg["services"] = {"redis": REDIS}
        cfg["provider"] = "fake"
        (tmp_path / "docker-compose.yml").write_text(COMPOSE)
        p = tmp_path / "rc.yml"
        p.write_text(yaml.safe_dump(cfg))

        monkeypatch.setattr(cli_v2, "resolve_provider", lambda *a, **k: object())
        with pytest.raises(click.exceptions.Exit):
            cli_v2.dispatch_if_v2(p, "plan")
