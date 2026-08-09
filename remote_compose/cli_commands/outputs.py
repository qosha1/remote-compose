"""rc outputs — machine-readable resource ids from the deployed stack.

The declared-network primitives (`network:` / `repositories:` in rc.yml) exist
so something *outside* rc can attach to them: a backend calling ``run_task``, a
Lambda, a CI job. That handshake only works if the ids come back out in a shape
a script can consume, so this command is the other half of the feature — not a
convenience wrapper over ``terraform output``.

    rc outputs                 human-readable table
    rc outputs --json          raw terraform output JSON (values unwrapped)
    rc outputs --env           KEY=value lines, ready for eval/export
    rc outputs security_groups a single output, unwrapped

``--env`` flattens nested maps and lists so every value is a plain scalar::

    RC_VPC_ID=vpc-0a1b2c
    RC_SECURITY_GROUPS_RUNNERS=sg-0d4e5f
    RC_SUBNETS_RUNNERS_PRIVATE=subnet-0aa,subnet-0bb
    RC_REPOSITORIES_DB_SIDECAR=1234.dkr.ecr.us-west-2.amazonaws.com/bmgr/db-sidecar
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click


def _env_key(prefix: str, *parts: str) -> str:
    """Build a shell-safe env var name from an output name and its key path."""
    joined = "_".join(str(p) for p in parts if str(p) != "")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", joined).strip("_").upper()
    return f"{prefix}{slug}"


def _flatten(value: Any, prefix: str, *parts: str) -> list[tuple[str, str]]:
    """Flatten a terraform output value into scalar KEY=value pairs.

    Lists of scalars become one comma-joined value rather than indexed keys —
    ``RUNNER_PRIVATE_SUBNETS=subnet-a,subnet-b`` is the shape consumers of a
    subnet list actually want. Maps recurse, so ``security_groups.runners``
    becomes ``..._SECURITY_GROUPS_RUNNERS``.
    """
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for k in sorted(value):
            out.extend(_flatten(value[k], prefix, *parts, k))
        return out
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            out = []
            for i, v in enumerate(value):
                out.extend(_flatten(v, prefix, *parts, str(i)))
            return out
        return [(_env_key(prefix, *parts), ",".join(str(v) for v in value))]
    if isinstance(value, bool):
        return [(_env_key(prefix, *parts), "true" if value else "false")]
    if value is None:
        return [(_env_key(prefix, *parts), "")]
    return [(_env_key(prefix, *parts), str(value))]


def _warn_on_aliased_keys(pairs: list[tuple[str, str]]) -> None:
    """Surface two different resources flattening to one variable name.

    ``_env_key`` collapses every non-alphanumeric run to ``_``, and some output
    maps have composite keys — ``vpc_endpoints`` is keyed
    ``"<name>.<service suffix>"``, so endpoint ``ecr`` serving ``ecr.api`` and
    endpoint ``ecr-ecr`` serving ``api`` both land on
    ``RC_VPC_ENDPOINTS_ECR_ECR_API``. Last one wins. That is rare enough not to
    be worth mangling every name to avoid, but far too quiet to leave unsaid —
    the whole point of this command is that something downstream trusts the
    value.
    """
    seen: dict[str, str] = {}
    for key, value in pairs:
        prior = seen.get(key)
        if prior is not None and prior != value:
            click.echo(
                f"warning: {key} is produced by two different outputs "
                f"({prior!r} and {value!r}); only the last is emitted. "
                f"Rename one of the declared resources to disambiguate.",
                err=True,
            )
        seen[key] = value


def _render_table(outputs: dict[str, Any]) -> str:
    lines: list[str] = []
    for name in sorted(outputs):
        value = outputs[name]
        if isinstance(value, dict):
            lines.append(f"{name}:")
            if not value:
                lines.append("    (empty)")
            for k in sorted(value):
                v = value[k]
                rendered = ", ".join(str(i) for i in v) if isinstance(v, list) else v
                lines.append(f"    {k} = {rendered}")
        elif isinstance(value, list):
            lines.append(f"{name}:")
            for v in value:
                lines.append(f"    {v}")
        else:
            lines.append(f"{name} = {value}")
    return "\n".join(lines)


@click.command(name="outputs")
@click.argument("name", required=False)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON (values unwrapped from terraform's {value,type} envelope).",
)
@click.option(
    "--env",
    "as_env",
    is_flag=True,
    help="Emit KEY=value lines, flattening maps and lists. Pipe to a .env or eval.",
)
@click.option(
    "--prefix",
    default="RC_",
    show_default=True,
    help="Prefix for --env variable names.",
)
@click.pass_context
def outputs_cmd(ctx, name, as_json, as_env, prefix):
    """Show resource ids emitted by the deployed stack.

    Covers rc's built-in outputs (cluster, ALB, ECR repos) plus every declared
    `network:` / `repositories:` resource — security group ids, subnet ids per
    group, VPC endpoint ids, and repository URIs.
    """
    from remote_compose.cli_v2 import build_deploy_context, load_rc_yml
    from remote_compose.terraform.runner import TerraformError, TerraformRunner

    if as_json and as_env:
        click.echo("--json and --env are mutually exclusive.", err=True)
        raise click.exceptions.Exit(1)

    config_path = ctx.obj.get("config_path") if ctx.obj else None
    path = Path(config_path) if config_path else Path.cwd() / "rc.yml"
    if not path.exists():
        click.echo(f"no rc.yml at {path}", err=True)
        raise click.exceptions.Exit(1)

    try:
        version, raw, v2 = load_rc_yml(path)
    except Exception as exc:
        click.echo(f"rc.yml parse failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    if version != 2 or v2 is None:
        click.echo(
            "rc outputs requires a rc.yml v2 config. Run `rc migrate` first.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    deploy_ctx = build_deploy_context(v2, raw, path)
    tf_dir = Path(deploy_ctx.working_dir) / "terraform"
    if not tf_dir.exists():
        click.echo(
            f"no terraform state directory at {tf_dir} — run `rc deploy` first.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    try:
        envelope = TerraformRunner(tf_dir).output()
    except TerraformError as exc:
        click.echo(f"terraform output failed: {exc}", err=True)
        raise click.exceptions.Exit(1)

    # terraform wraps each output as {"value": ..., "type": ...}; nobody
    # consuming this wants the envelope.
    resolved = {
        k: (v.get("value") if isinstance(v, dict) and "value" in v else v)
        for k, v in (envelope or {}).items()
    }

    if name:
        if name not in resolved:
            click.echo(
                f"no output named {name!r}. Available: "
                f"{', '.join(sorted(resolved)) or 'none'}",
                err=True,
            )
            raise click.exceptions.Exit(1)
        value = resolved[name]
        if as_env:
            pairs = _flatten(value, prefix, name)
            _warn_on_aliased_keys(pairs)
            for key, val in pairs:
                click.echo(f"{key}={val}")
        elif as_json or isinstance(value, (dict, list)):
            click.echo(json.dumps(value, indent=2, sort_keys=True))
        else:
            click.echo(value)
        return

    if not resolved:
        click.echo("(no outputs — has the stack been deployed?)", err=True)
        return

    if as_json:
        click.echo(json.dumps(resolved, indent=2, sort_keys=True))
    elif as_env:
        pairs = [
            pair
            for out_name in sorted(resolved)
            for pair in _flatten(resolved[out_name], prefix, out_name)
        ]
        _warn_on_aliased_keys(pairs)
        for key, val in pairs:
            click.echo(f"{key}={val}")
    else:
        click.echo(_render_table(resolved))
