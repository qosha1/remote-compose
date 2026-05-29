"""rc copilot — migrate AWS Copilot apps to rc.yml v2.

AWS Copilot reaches end-of-support on 2026-06-12. This command is the
fast path off it.
"""

from __future__ import annotations

from pathlib import Path

import click


@click.group(name="copilot")
def copilot_group():
    """AWS Copilot migration. (Copilot is end-of-support 2026-06-12.)"""
    pass


@copilot_group.command(name="import")
@click.option(
    "--from",
    "from_dir",
    default="./copilot",
    show_default=True,
    type=click.Path(exists=True, file_okay=False),
    help="Path to the source copilot/ directory.",
)
@click.option(
    "--out",
    "out_dir",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Where to write rc.yml + docker-compose.yml + IMPORT_SUMMARY.md.",
)
@click.option(
    "--env",
    "env_name",
    default=None,
    help="Copilot environment to pin (production/staging/dev). "
    "If unset, base manifest values are used and "
    "${COPILOT_ENVIRONMENT_NAME} stays literal in secret ARNs.",
)
@click.option(
    "--project",
    "project_name",
    default=None,
    help="rc.yml v2 project field. Defaults to the parent dir name "
    "of the copilot/ tree.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing rc.yml / docker-compose.yml in --out.",
)
def copilot_import(from_dir, out_dir, env_name, project_name, force):
    """Translate a copilot/ tree to rc.yml v2 + docker-compose.yml.

    \b
    Reads every copilot/<service>/manifest.yml + copilot/environments/*,
    runs the translators, and writes:
      <out>/rc.yml                  rc.yml v2
      <out>/docker-compose.yml      compose file with build/image + env
      <out>/IMPORT_SUMMARY.md       per-service translation report

    AWS Copilot reaches end-of-support on 2026-06-12. This command is the
    fast path off it.
    """
    import yaml
    from remote_compose.copilot import discover, DiscoveryError
    from remote_compose.copilot.translate import compose_app

    src = Path(from_dir).resolve()
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    rc_path = target / "rc.yml"
    compose_path = target / "docker-compose.yml"
    summary_path = target / "IMPORT_SUMMARY.md"

    for p in (rc_path, compose_path):
        if p.exists() and not force:
            click.echo(
                f"refusing to overwrite {p} — re-run with --force to replace.",
                err=True,
            )
            raise click.exceptions.Exit(1)

    try:
        app = discover(src)
    except DiscoveryError as exc:
        click.echo(f"rc copilot import: {exc}", err=True)
        raise click.exceptions.Exit(1)

    result = compose_app(app, project=project_name, env=env_name)

    rc_path.write_text(yaml.safe_dump(result.rc_yml, sort_keys=False))
    compose_path.write_text(yaml.safe_dump(result.docker_compose, sort_keys=False))
    summary_path.write_text(result.summary)

    click.echo(f"\nrc copilot import — {result.rc_yml['project']}")
    click.echo(f"  source:    {src}")
    click.echo(f"  env:       {env_name or '(base manifest values)'}")
    click.echo(f"  services:  {len(result.rc_yml['services'])}")
    if result.warnings:
        click.echo(f"  warnings:  {len(result.warnings)} (see IMPORT_SUMMARY.md)")
    # rc-e5u.43.8: surface untranslated addon CFN templates so the user can
    # decide what to do (RDS, S3, DynamoDB etc. need manual replacement).
    addon_count = sum(len(s.addons or []) for s in app.services)
    if addon_count:
        click.echo(
            f"  addons:    {addon_count} CFN template(s) — manual "
            f"translation required (see IMPORT_SUMMARY.md)"
        )
    click.echo(f"\n  wrote {rc_path}")
    click.echo(f"  wrote {compose_path}")
    click.echo(f"  wrote {summary_path}")
    click.echo(f"\n  Next: review the summary, then `rc plan` from {target}")
