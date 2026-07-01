"""`rc deploy --no-roll` threads skip_force_roll into the v2 dispatch.

Exposes the existing skip_force_roll path (used internally by `rc up`) as a CLI
flag so a caller can build+push images, run migrations on the freshly-pushed
:latest, THEN roll with `rc deploy --no-build`. Default (flag absent) must keep
force-rolling exactly as before.
"""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from remote_compose.cli_commands.deploy import deploy_cmd


def _dispatch_kwargs(args):
    with mock.patch("remote_compose.cli_v2.dispatch_if_v2") as d:
        d.return_value = True  # short-circuit: pretend v2 handled the deploy
        CliRunner().invoke(deploy_cmd, args, obj={"config_path": "rc.yml"})
        return d.call_args.kwargs if d.call_args else {}


def test_no_roll_threads_skip_force_roll():
    assert _dispatch_kwargs(["--no-roll", "--no-state"]).get("skip_force_roll") is True


def test_default_deploy_does_not_skip_force_roll():
    assert _dispatch_kwargs(["--no-state"]).get("skip_force_roll") is False


def test_no_roll_appears_in_help():
    result = CliRunner().invoke(deploy_cmd, ["--help"])
    assert "--no-roll" in result.output
