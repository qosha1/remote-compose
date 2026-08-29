"""rc up — one-shot: scaffold (if missing) + deploy + push secrets.

The "I have a docker-compose.yml — get me a running stack" command. Wraps
init scaffolding, deploy dispatch, and secrets push into a single flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click
import yaml

from ._dispatchers import _push_existing_secrets_before_apply, _secrets_push_v2


@click.command(name="up")
@click.option(
    "--from-compose",
    "from_compose",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="If rc.yml is missing, scaffold one from this docker-compose.yml first.",
)
@click.option(
    "--public-service",
    "public_service",
    default=None,
    help="Override the auto-detected ALB-fronted service when scaffolding.",
)
@click.option(
    "--region",
    default="us-west-2",
    help="AWS region used when scaffolding rc.yml (ignored if rc.yml exists).",
)
@click.option(
    "--aws-profile",
    "aws_profile",
    default=None,
    help="aws_profile used when scaffolding rc.yml (ignored if rc.yml exists).",
)
@click.option(
    "--testing-defaults/--no-testing-defaults",
    "testing_defaults",
    default=None,
    help="Inject DJANGO_ALLOWED_HOSTS=* / CSRF_TRUSTED_ORIGINS=* "
    "on Django services when scaffolding rc.yml (ignored if "
    "rc.yml exists). Default auto-on for rc-test-* projects. "
    "See rc-e5u.46.4.",
)
@click.option(
    "--ttl",
    "ttl",
    default=None,
    help="Mark this stack ephemeral with the given TTL "
    "(e.g. 30m, 4h, 2h30m). Tags resources Ephemeral=true + "
    "ExpiresAt=<iso>; `rc reap` later destroys past-due stacks.",
)
@click.option(
    "--dev",
    "dev_mode",
    is_flag=True,
    help="Dev-mode deploy: provision EFS-backed bind mounts for "
    "every services[*].dev_volumes entry so `rc dev push` can "
    "stream local source into the running task for sub-second "
    "iteration. See rc-e5u.45.8.",
)
@click.option(
    "--domain",
    "domain",
    default=None,
    help="Wire the ALB to a custom FQDN. Scaffolds "
    "services.<public>.domain + provider_config.ecs."
    "route53_zone (drop-leftmost-label heuristic) so "
    "terraform creates ACM cert + Route 53 A records. "
    "Verifies the zone exists in the configured aws_profile "
    "before deploying. See rc-e5u.46.7.",
)
@click.option(
    "--alias",
    "aliases",
    multiple=True,
    help="Add an additional FQDN as a SAN on the ACM cert + an "
    "extra A record. Repeat --alias N times for N aliases. "
    "Requires --domain.",
)
@click.option(
    "--route53-zone",
    "route53_zone",
    default=None,
    help="Override the Route 53 hosted-zone name (defaults to "
    "--domain with the leftmost label dropped). Use when "
    "your zone is something other than parent-of-FQDN.",
)
@click.pass_context
def up_cmd(
    ctx,
    from_compose,
    public_service,
    region,
    aws_profile,
    testing_defaults,
    ttl,
    dev_mode,
    domain,
    aliases,
    route53_zone,
):
    """One-shot: scaffold rc.yml (if missing), deploy, push secrets, print ALB URL.

    The "I have a docker-compose.yml — get me a running stack" command. With
    --from-compose, rc generates a v2 rc.yml on the fly when none exists,
    then runs the deploy and secrets-push pipeline. Idempotent: rerun on an
    unchanged config does the no-op terraform apply and a forced rollout.
    """
    config_path = ctx.obj.get("config_path") or "rc.yml"
    target = Path(config_path)

    if aliases and not domain:
        raise click.ClickException(
            "--alias requires --domain. Use --domain <FQDN> + "
            "--alias <other-FQDN> together."
        )

    if not target.exists():
        if not from_compose:
            raise click.ClickException(
                f"{target} not found. Pass --from-compose <docker-compose.yml> to "
                f"scaffold inline, or run `rc init --from-compose <path>` first."
            )
        from remote_compose.init_from_compose import generate_v2_rc_yml

        click.echo(f"Scaffolding {target} from {from_compose}...")
        text = generate_v2_rc_yml(
            Path(from_compose),
            output_path=target,
            public_service=public_service,
            region=region,
            aws_profile=aws_profile,
            testing_defaults=testing_defaults,
        )
        target.write_text(text)
        click.echo(f"  written ({len(text)} bytes).\n")

    if domain:
        from remote_compose.init_from_compose import _patch_rc_yml_domain
        from remote_compose.init_from_compose import _zone_from_domain_drop_leftmost

        # rc-d7s: when rc.yml exists and declares provider_config.ecs.aws_profile,
        # use it as the default for the Route 53 pre-flight. Without this, the
        # pre-flight uses --aws-profile=None (no boto3 profile, may differ from
        # the rc.yml-declared profile) and reports 'zone not found' even when
        # the zone exists in the right profile. Same fallback for region.
        effective_profile = aws_profile
        effective_region = region
        if target.exists():
            try:
                import yaml as _yaml

                rc_raw = _yaml.safe_load(target.read_text()) or {}
                if isinstance(rc_raw, dict):
                    ecs_cfg = (rc_raw.get("provider_config") or {}).get("ecs") or {}
                    effective_profile = effective_profile or ecs_cfg.get("aws_profile")
                    effective_region = ecs_cfg.get("region") or effective_region
            except Exception:
                pass
        zone_check = route53_zone or _zone_from_domain_drop_leftmost(domain)
        try:
            import boto3

            session = boto3.Session(
                profile_name=effective_profile,
                region_name=effective_region,
            )
            r53 = session.client("route53")
            zones = r53.list_hosted_zones().get("HostedZones") or []
            zone_names = {z.get("Name", "").rstrip(".") for z in zones}
            if zone_check.rstrip(".") not in zone_names:
                raise click.ClickException(
                    f"--domain pre-flight: Route 53 hosted zone "
                    f"'{zone_check}' not found in profile {effective_profile!r}. "
                    f"Existing zones: {sorted(zone_names) or '(none)'}. "
                    f"Override with --route53-zone <zone-you-own>."
                )
        except click.ClickException:
            raise
        except Exception as exc:
            click.echo(
                f"  WARN: could not verify Route 53 zone {zone_check!r} "
                f"({type(exc).__name__}: {exc}); proceeding anyway.",
                err=True,
            )
        try:
            wired = _patch_rc_yml_domain(
                target,
                domain,
                list(aliases or []),
                route53_zone=route53_zone,
            )
        except ValueError as exc:
            raise click.ClickException(f"--domain: {exc}")
        click.echo(
            f"  Domain wired (rc-e5u.46.7): "
            f"services.{wired['public_service']}.domain = {wired['domain']}; "
            f"route53_zone = {wired['route53_zone']}"
        )
        if wired["aliases"]:
            click.echo(f"    aliases: {', '.join(wired['aliases'])}")
        click.echo("")

    compose_path_for_autofix: Optional[Path] = None
    if from_compose:
        compose_path_for_autofix = Path(from_compose).resolve()
    elif target.exists():
        try:
            rc_raw_existing = yaml.safe_load(target.read_text()) or {}
        except yaml.YAMLError:
            rc_raw_existing = {}
        compose_field = (
            rc_raw_existing.get("compose_file")
            if isinstance(rc_raw_existing, dict)
            else None
        )
        if compose_field:
            cp = Path(compose_field)
            if not cp.is_absolute():
                cp = (target.parent / cp).resolve()
            if cp.exists():
                compose_path_for_autofix = cp
    if compose_path_for_autofix is not None:
        try:
            from remote_compose.init_from_compose import auto_fix_nginx_if_needed

            result = auto_fix_nginx_if_needed(target, compose_path_for_autofix)
        except Exception as exc:
            click.echo(f"  WARN: nginx auto-fix skipped: {exc}", err=True)
            result = None
        if result:
            project_dir = target.parent.resolve()
            try:
                nginx_rel = result["nginx_path"].relative_to(project_dir)
                df_rel = result["dockerfile_path"].relative_to(project_dir)
            except ValueError:
                nginx_rel = result["nginx_path"]
                df_rel = result["dockerfile_path"]
            ups = ", ".join(
                f"{u.name}:{u.port}{' (django)' if u.django else ''}"
                for u in result["upstreams"]
            )
            click.echo(
                f"  auto-fixed nginx config for ECS — see "
                f"./{result['output_subdir']}/ "
                f"(rc-e5u.46.2; rc-e5u.44.18 detector fired)."
            )
            click.echo(f"    upstreams:  {ups}")
            click.echo(f"    wrote:      {nginx_rel}")
            click.echo(f"                {df_rel}")
            click.echo(
                f"    rc.yml:     services.{result['nginx_service']}."
                f"dockerfile = {result['dockerfile_rel']}\n"
            )

    from remote_compose.cli_v2 import (
        dispatch_if_v2,
        run_auto_on_deploy_hooks_for_path,
    )

    # rc-mbav: push secret VALUES BEFORE the deploy, not just after it.
    #
    # The push below (after dispatch_if_v2) was always too late for a bundle
    # that gained a NEW key: terraform's apply points aws_ecs_service at the
    # task def referencing that key, ECS starts placing tasks immediately, and
    # placement fails with "did not contain json key ..." minutes before this
    # function gets to push anything. The circuit breaker then rolls back onto
    # a previous task def whose sibling services the same apply may already
    # have destroyed — a ~12 minute production outage, observed 2026-08-26.
    #
    # rc-1bk's skip_force_roll deferral does NOT cover this. It only holds back
    # rc's own force-new-deployment; terraform updating the service starts a
    # rollout on its own.
    #
    # This pass skips secrets that do not exist yet (nothing is running against
    # them), so the post-deploy push below still owns first-apply population.
    _push_existing_secrets_before_apply(str(target))

    if not dispatch_if_v2(
        str(target),
        "deploy",
        ttl=ttl,
        dev=dev_mode,
        defer_lifecycle_hooks=True,
        # rc-1bk: defer the force-roll until after _secrets_push_v2 below
        # populates SM. Otherwise the deploy rolls services with
        # placeholder secrets and tasks fail to start, which then hangs
        # the run_auto_on_deploy_hooks wait_for_stable + exec polls for
        # 30+ minutes.
        skip_force_roll=True,
    ):
        raise click.ClickException(
            f"{target} is not a v2 rc.yml. `rc up` only supports v2 — "
            f"migrate with `rc migrate` or use `rc deploy` for v1."
        )

    try:
        _secrets_push_v2(str(target), rollout=True)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        click.echo(f"\n  WARN: secrets push failed: {exc}", err=True)
        click.echo("  Run `rc secrets push` after fixing the issue above.")

    try:
        run_auto_on_deploy_hooks_for_path(str(target))
    except Exception as exc:
        click.echo(
            f"\n  WARN: auto_on_deploy hooks failed: {exc!s}. "
            f"Rerun with `rc lifecycle <hook> <service>` for full output.",
            err=True,
        )

    if ttl:
        click.echo(
            f"\n  Stack is up (TTL {ttl}). `rc reap` tears it down once expired, "
            f"or `rc destroy --yes` to teardown now."
        )
    else:
        click.echo(
            "\n  Stack is up. Use `rc status` to inspect and `rc destroy` to tear down."
        )
