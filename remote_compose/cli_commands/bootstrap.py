"""rc bootstrap — emit + plan/apply the committed GitHub-OIDC CI deploy-role stack.

The CI/bootstrap IAM (the role CI assumes via OIDC to trigger deploys) is not a
per-service runtime resource, so it lives in its own COMMITTED stack with its own
terraform state rather than the regenerated workload stack. This command reads the
rc.yml ``bootstrap:`` section, generates that stack into ``bootstrap.output_dir``,
and runs ``terraform init`` + ``plan``. With ``--apply`` it also applies — opt-in,
and it never destroys (the deploy role must not be torn down by a routine run).
"""

from __future__ import annotations

from pathlib import Path

import click


def _make_runner(out_dir: Path, progress=None):
    """Indirection point so tests can inject a RecordingTerraformRunner."""
    from remote_compose.terraform.runner import TerraformRunner

    return TerraformRunner(out_dir, progress=progress)


def _workload_backend_dict(v2) -> dict:
    b = v2.terraform.backend
    out: dict = {"type": b.type}
    for k in ("bucket", "key", "region", "dynamodb_table"):
        val = getattr(b, k, None)
        if val is not None:
            out[k] = val
    out.update(getattr(b, "extra", {}) or {})
    return out


@click.command(name="bootstrap")
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    help="Apply the stack after planning (opt-in). Never destroys.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt when applying.",
)
@click.pass_context
def bootstrap_cmd(ctx, do_apply, yes):
    """Emit + plan the committed GitHub-OIDC CI deploy-role stack.

    \b
    Reads rc.yml `bootstrap:`, generates a committed, separately-stated
    terraform stack for the deploy role CI assumes, and runs
    `terraform init` + `plan`. Commit the generated stack to your repo.

    \b
    With --apply: also runs `terraform apply` (never destroys). Use
    --yes to skip the confirmation prompt.
    """
    from remote_compose.bootstrap import emit_bootstrap_stack
    from remote_compose.cli_v2 import load_rc_yml

    config_path = ctx.obj.get("config_path") if ctx.obj else None
    rc_path = Path(config_path).resolve() if config_path else (Path.cwd() / "rc.yml")
    if not rc_path.exists():
        raise click.ClickException(f"{rc_path} not found.")

    try:
        version, _raw, v2 = load_rc_yml(rc_path)
    except Exception as exc:
        raise click.ClickException(f"rc.yml parse failed: {exc}")
    if version != 2 or v2 is None:
        raise click.ClickException(
            "rc bootstrap requires a rc.yml v2 config. Run `rc migrate` first."
        )
    if v2.bootstrap is None or v2.bootstrap.github_oidc_deploy_role is None:
        raise click.ClickException(
            "rc.yml has no bootstrap.github_oidc_deploy_role section — nothing to "
            "bootstrap. See the README `bootstrap:` docs to add one."
        )

    role = v2.bootstrap.github_oidc_deploy_role
    project = v2.project
    ecs_cfg = (v2.provider_config or {}).get("ecs") or {}
    cluster = ecs_cfg.get("cluster") or f"{project}-cluster"
    out_dir = rc_path.parent / v2.bootstrap.output_dir

    click.echo(f"\nrc bootstrap — {rc_path}")
    click.echo(f"  emitting committed deploy-role stack -> {out_dir}")
    emit_bootstrap_stack(
        role,
        project=project,
        cluster=cluster,
        workload_backend=_workload_backend_dict(v2),
        out_dir=out_dir,
    )

    runner = _make_runner(out_dir, click.echo)
    runner.init()
    summary = runner.plan()
    click.echo(
        f"\n  plan: +{summary.create} ~{summary.update} -{summary.destroy} "
        f"(role={role.role_name or project + '-github-deploy'})"
    )

    if not do_apply:
        click.echo(
            "\n  (plan only) re-run with --apply to apply. "
            "Commit the generated stack so the deploy role stays tracked."
        )
        return

    # No-clobber guard: this stack must NEVER destroy live deploy IAM.
    if summary.destroy > 0:
        raise click.ClickException(
            f"plan would DESTROY {summary.destroy} resource(s); refusing to apply. "
            "The bootstrap stack must never destroy live deploy IAM — investigate "
            "the diff (likely a role_name mismatch or a missing terraform import)."
        )
    if not yes:
        click.confirm("  Apply the bootstrap stack?", abort=True)
    runner.apply()
    click.echo(
        "\n  Applied. Commit the generated stack so the deploy role is tracked in git."
    )
