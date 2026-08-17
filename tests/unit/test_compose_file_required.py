"""rc.yml's ``compose_file`` must name a file that actually has services.

startsim-wxb7: a ``compose_file`` pointing at a path that does not exist used
to parse to ``{}`` with no error and no warning. Every service then got
``compose_image=None`` / ``has_build_context=False``, and services.tf.j2 fell
through to ``image = "${aws_ecr_repository.<svc>.repository_url}:latest"``.
Terraform happily created an empty ECR repo and pointed the task definition at
a tag with nothing behind it — the render *succeeded*, and the failure only
surfaced later as a task that could not pull its image.

The compose file is the only place rc reads images and build contexts from, so
a compose_file that yields no services is the same defect as one that is
missing outright: a declared input that silently evaluates to empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
from remote_compose.config._errors import ConfigError
from remote_compose.provider.ecs import ECSProvider

RC_YML = {
    "version": 2,
    "project": "wxb7",
    "compose_file": "docker-compose.yml",
    "provider": "ecs",
    "provider_config": {
        "ecs": {"region": "us-west-2", "cluster": "wxb7-cluster"},
    },
    "services": {
        "web": {"cpu": 256, "memory": 512, "type": "proxy", "public": True, "port": 80},
    },
    "terraform": {"backend": {"type": "local"}},
}


def _write_rc(tmp_path: Path, **overrides) -> Path:
    cfg = dict(RC_YML)
    cfg.update(overrides)
    p = tmp_path / "rc.yml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class TestMissingComposeFileIsAnError:
    def test_build_deploy_context_names_the_key_and_the_path(self, tmp_path):
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError) as exc:
            build_deploy_context(v2, raw, p)

        msg = str(exc.value)
        # The config key that pointed at it...
        assert "compose_file" in msg
        assert "docker-compose.yml" in msg
        # ...and the path it actually resolved to.
        assert str((tmp_path / "docker-compose.yml").resolve()) in msg

    def test_no_terraform_is_emitted_with_a_bogus_latest_tag(self, tmp_path):
        """The end-to-end shape of the bug.

        Before the fix this whole block ran clean and left .tf on disk whose
        task definition pointed at ``<ecr_repo>.repository_url:latest``. Now
        it stops at the context build, so emit_terraform is never reached and
        nothing is written — both halves are asserted, since "no bogus tag in
        the emitted files" is satisfied vacuously by "no emitted files".
        """
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)
        out = tmp_path / "tf"

        with pytest.raises(ConfigError):
            ctx = build_deploy_context(v2, raw, p)
            ECSProvider().emit_terraform(ctx, out)

        emitted = list(out.glob("*.tf")) if out.exists() else []
        assert not emitted, f"terraform was emitted: {[str(f) for f in emitted]}"
        bogus = [f for f in emitted if "repository_url}:latest" in f.read_text()]
        assert not bogus, (
            "terraform was emitted pointing at an ECR tag rc never builds or "
            f"pushes: {[str(f) for f in bogus]}"
        )

    def test_absolute_compose_file_is_reported_verbatim(self, tmp_path):
        missing = tmp_path / "elsewhere" / "compose.yml"
        p = _write_rc(tmp_path, compose_file=str(missing))
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError) as exc:
            build_deploy_context(v2, raw, p)
        assert str(missing) in str(exc.value)


class TestEmptyComposeFileIsAnError:
    def test_no_services_key(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("volumes:\n  pgdata: {}\n")
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError, match="no services"):
            build_deploy_context(v2, raw, p)

    def test_empty_file(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("")
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError, match="no services"):
            build_deploy_context(v2, raw, p)

    def test_scalar_document(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("not-a-compose-file\n")
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError, match="must be a mapping"):
            build_deploy_context(v2, raw, p)

    def test_unrendered_template_literal(self, tmp_path):
        """A half-rendered compose template names the file it came from."""
        (tmp_path / "docker-compose.yml").write_text("{{ COMPOSE_FILE }}\n")
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        with pytest.raises(ConfigError) as exc:
            build_deploy_context(v2, raw, p)
        assert str((tmp_path / "docker-compose.yml").resolve()) in str(exc.value)


class TestLiveStackCommandsTolerateAMissingComposeFile:
    """A missing compose file must never strand deployed infrastructure.

    Ephemeral stacks delete their generated compose file once the deploy
    lands, so for those stacks it is absent *by design* from then on. Status,
    outputs, exec, run, lifecycle hooks and destroy only read live state or
    act inside already-running containers — nothing durable is derived from
    compose — so they have to keep working. The paths that turn compose data
    into infrastructure (plan, deploy, adopt) still error out.
    """

    def test_build_deploy_context_opt_out(self, tmp_path, capsys):
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p, require_compose_file=False)

        assert "web" in ctx.services
        assert "compose_file" in capsys.readouterr().err

    def test_opt_out_also_survives_an_unreadable_compose_file(self, tmp_path, capsys):
        """Present-but-broken is as fatal to a live stack as absent."""
        (tmp_path / "docker-compose.yml").write_text("{{ COMPOSE_FILE }}\n")
        p = _write_rc(tmp_path)
        _, raw, v2 = load_rc_yml(p)

        ctx = build_deploy_context(v2, raw, p, require_compose_file=False)

        assert "web" in ctx.services
        assert "WARN" in capsys.readouterr().err

    def test_destroy_dispatches_without_a_compose_file(self, tmp_path, monkeypatch):
        from remote_compose import cli_v2

        p = _write_rc(tmp_path, provider="fake")
        destroyed = []

        class _FakeProvider:
            def destroy(self, ctx):
                destroyed.append(ctx.project)

        monkeypatch.setattr(cli_v2, "resolve_provider", lambda *a, **k: _FakeProvider())
        assert cli_v2.dispatch_if_v2(p, "destroy", yes=True) is True
        assert destroyed == ["wxb7"]

    def test_status_dispatches_without_a_compose_file(self, tmp_path, monkeypatch):
        """rc status only reads live ECS state — the bead's own trigger
        scenario is a deployed stack whose compose file is already gone."""
        from remote_compose import cli_v2
        from remote_compose.provider import StatusReport

        p = _write_rc(tmp_path, provider="fake")
        asked = []

        class _FakeProvider:
            def status(self, ctx):
                asked.append(sorted(ctx.services))
                return StatusReport(services=[], cluster_health="active")

        monkeypatch.setattr(cli_v2, "resolve_provider", lambda *a, **k: _FakeProvider())
        assert cli_v2.dispatch_if_v2(p, "status") is True
        assert asked == [["web"]]

    def test_plan_still_errors_without_a_compose_file(self, tmp_path, monkeypatch):
        """The tolerant commands must not soften the ones that emit."""
        import click

        from remote_compose import cli_v2

        p = _write_rc(tmp_path, provider="fake")
        monkeypatch.setattr(cli_v2, "resolve_provider", lambda *a, **k: object())
        with pytest.raises(click.exceptions.Exit):
            cli_v2.dispatch_if_v2(p, "plan")
