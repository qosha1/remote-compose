"""rc — Simple Remote Compose CLI.

Drop an rc.yml in your project directory and deploy to ECS with:
    rc provision   # one-time infrastructure setup (v1)
    rc deploy      # build, push, deploy
    rc status      # check service health
    rc destroy     # tear it all down

This file is the thin entry point. Each command lives in its own module
under cli_commands/ and is registered below via cli.add_command. Shared
helpers live in cli_commands/_dispatchers.py (v2-aware) and
cli_commands/_legacy.py (v1).
"""

import click

# Backwards-compat re-exports for existing test suites that import helpers
# directly from remote_compose.cli. Kept here so the split is invisible to
# anything outside the cli_commands/* package. Remove once tests update.
from .cli_commands._dispatchers import (  # noqa: F401, E402
    _build_restore_script,
    _db_push_v2,
    _detect_dump_format,
    _detect_empty_file_secrets,
    _exec_v2,
    _flatten_v2_to_legacy,
    _secrets_push_v2,
)
from .cli_commands._legacy import (  # noqa: F401, E402
    _bootstrap_django,
    _load_config,
)
from .cli_commands.destroy import _teardown_infrastructure  # noqa: F401, E402
from .cli_commands.list_stacks import _format_relative_time  # noqa: F401, E402

# Command modules. cli.py owns registration (cli.add_command below); each
# module owns its command body.
from .cli_commands.adopt import adopt_cmd as _adopt_cmd
from .cli_commands.audit import audit_cmd as _audit_cmd
from .cli_commands.bootstrap import bootstrap_cmd as _bootstrap_cmd
from .cli_commands.compose import compose_group as _compose_group
from .cli_commands.copilot import copilot_group as _copilot_group
from .cli_commands.db import db_group as _db_group
from .cli_commands.deploy import deploy_cmd as _deploy_cmd
from .cli_commands.destroy import destroy_cmd as _destroy_cmd
from .cli_commands.destroy import reap_cmd as _reap_cmd
from .cli_commands.dev import dev_group as _dev_group
from .cli_commands.doctor import doctor_cmd as _doctor_cmd
from .cli_commands.doctor import install_cmd as _install_cmd
from .cli_commands.exec import exec_cmd as _exec_cmd
from .cli_commands.fix import fix_group as _fix_group
from .cli_commands.init import init_cmd as _init_cmd
from .cli_commands.lifecycle import lifecycle_cmd as _lifecycle_cmd
from .cli_commands.list_stacks import list_cmd as _list_cmd
from .cli_commands.migrate import migrate_cmd as _migrate_cmd
from .cli_commands.plan import plan_cmd as _plan_cmd
from .cli_commands.provision import provision_cmd as _provision_cmd
from .cli_commands.run import run_cmd as _run_cmd
from .cli_commands.secrets import secrets_group as _secrets_group
from .cli_commands.service_ops import logs_cmd as _logs_cmd
from .cli_commands.service_ops import restart_cmd as _restart_cmd
from .cli_commands.service_ops import status_cmd as _status_cmd
from .cli_commands.up import up_cmd as _up_cmd
from .cli_commands.v1_migrate import v1_group as _v1_group


def _warn_on_rc_yml_ambiguity() -> None:
    """rc-td9: when multiple rc*.yml configs exist in cwd and no -c was
    passed, the user may not realize 'rc' will silently use rc.yml. Once
    bit (sentinal had rc.yml us-west-2 + rc.core.yml us-west-1; rc up
    without -c started creating us-west-2 resources against a missing VPC).

    Heuristic: scan cwd for rc.yml + rc.*.yml siblings. If >1 exists, emit
    a stderr nudge listing them. Does NOT block — the user may genuinely
    want the rc.yml default.
    """
    from pathlib import Path

    cwd = Path.cwd()
    candidates = sorted(
        {p.name for p in cwd.glob("rc.yml")} | {p.name for p in cwd.glob("rc.*.yml")}
    )
    if len(candidates) <= 1:
        return
    click.echo(
        f"  ! Multiple rc configs found in {cwd}: "
        f"{', '.join(candidates)}. Defaulting to rc.yml — pass "
        f"-c <file> to choose.",
        err=True,
    )


@click.group()
@click.option("-c", "--config", "config_path", default=None, help="Path to rc.yml")
@click.pass_context
def cli(ctx, config_path):
    """rc — Simple Remote Compose CLI for ECS deployments."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    if config_path is None:
        _warn_on_rc_yml_ambiguity()


# =============================================================================
# Register every command module. cli.py owns the registration; module owns body.
# =============================================================================

cli.add_command(_adopt_cmd)
cli.add_command(_audit_cmd)
cli.add_command(_bootstrap_cmd)
cli.add_command(_compose_group)
cli.add_command(_copilot_group)
cli.add_command(_db_group)
cli.add_command(_deploy_cmd)
cli.add_command(_destroy_cmd)
cli.add_command(_dev_group)
cli.add_command(_doctor_cmd)
cli.add_command(_exec_cmd)
cli.add_command(_fix_group)
cli.add_command(_init_cmd)
cli.add_command(_install_cmd)
cli.add_command(_lifecycle_cmd)
cli.add_command(_list_cmd)
cli.add_command(_logs_cmd)
cli.add_command(_migrate_cmd)
cli.add_command(_plan_cmd)
cli.add_command(_provision_cmd)
cli.add_command(_run_cmd)
cli.add_command(_reap_cmd)
cli.add_command(_restart_cmd)
cli.add_command(_secrets_group)
cli.add_command(_status_cmd)
cli.add_command(_up_cmd)
cli.add_command(_v1_group)


if __name__ == "__main__":
    cli()
