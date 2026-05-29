"""rc destroy + rc reap — service + infrastructure teardown.

destroy single-stack: v2 dispatched via cli_v2; v1 walks Django models +
boto3. destroy --all-ephemeral and rc reap share _destroy_ephemeral_targets
to walk the local ephemeral registry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._legacy import _bootstrap_django, _load_config


def _destroy_ephemeral_targets(targets, yes: bool, command_name: str) -> None:
    """Sequentially destroy each ephemeral stack via provider.destroy."""
    from remote_compose.ephemeral import remove_stack
    from remote_compose.cli_v2 import (
        build_deploy_context,
        load_rc_yml,
        resolve_provider,
    )

    if not yes:
        if not click.confirm(
            f"\n  Destroy these {len(targets)} stack(s)?",
            default=False,
        ):
            click.echo("  aborted.")
            return

    failures: list[tuple[str, str]] = []
    succeeded = 0
    for r in targets:
        click.echo(f"\n  Destroying {r.project} ({r.region})...")
        rc_path = Path(r.rc_yml_path)
        tf_dir = Path(r.terraform_dir) if r.terraform_dir else None

        if not rc_path.exists():
            if tf_dir and tf_dir.exists():
                click.echo(
                    f"    rc.yml at {rc_path} missing — falling back to "
                    f"terraform destroy in {tf_dir}."
                )
                from remote_compose.terraform.runner import (
                    TerraformError,
                    TerraformRunner,
                )

                try:
                    runner = TerraformRunner(tf_dir)
                    runner.init()
                    runner.destroy()
                except TerraformError as exc:
                    click.echo(
                        f"    FAILED: terraform destroy in {tf_dir}: {exc}", err=True
                    )
                    failures.append((r.project, f"tf destroy: {exc}"))
                    continue
                except Exception as exc:
                    click.echo(f"    FAILED: terraform destroy: {exc}", err=True)
                    failures.append((r.project, str(exc)))
                    continue
                remove_stack(project=r.project, region=r.region)
                succeeded += 1
                click.echo("    done (via terraform_dir fallback).")
                continue
            click.echo(
                f"    rc.yml + terraform_dir both missing — "
                f"falling back to AWS audit for project={r.project} "
                f"region={r.region}."
            )
            try:
                import boto3
                from remote_compose.audit import audit_project

                session = boto3.Session(
                    region_name=r.region,
                    profile_name=r.aws_profile,
                )
                report = audit_project(
                    session,
                    project=r.project,
                    region=r.region,
                )
            except Exception as exc:
                click.echo(f"    FAILED: audit fallback: {exc}", err=True)
                failures.append((r.project, f"audit fallback: {exc}"))
                continue
            if report.is_clean:
                remove_stack(project=r.project, region=r.region)
                succeeded += 1
                click.echo(
                    "    audit clean — no AWS resources match. "
                    "Removed orphan registry entry."
                )
                continue
            click.echo(
                f"    audit found {len(report.findings)} leftover "
                f"resource(s) — leaving registry entry. Clean up with:",
                err=True,
            )
            click.echo(
                f"      rc audit --project {r.project} --region {r.region}"
                + (f" --profile {r.aws_profile}" if r.aws_profile else ""),
                err=True,
            )
            failures.append(
                (
                    r.project,
                    f"rc.yml + terraform_dir missing; "
                    f"{len(report.findings)} AWS leftover(s) need manual cleanup",
                ),
            )
            continue
        try:
            version, raw, v2 = load_rc_yml(rc_path)
        except Exception as exc:
            click.echo(f"    FAILED: rc.yml parse: {exc}", err=True)
            failures.append((r.project, f"rc.yml parse: {exc}"))
            continue
        if version != 2 or v2 is None:
            click.echo(
                f"    FAILED: rc.yml at {rc_path} is not v2 "
                f"(only v2 stacks can be ephemeral).",
                err=True,
            )
            failures.append((r.project, "not v2"))
            continue
        try:
            ctx = build_deploy_context(v2, raw, rc_path)
            provider = resolve_provider(v2)
            provider.destroy(ctx)
        except Exception as exc:
            click.echo(f"    FAILED: provider.destroy: {exc}", err=True)
            failures.append((r.project, str(exc)))
            continue
        remove_stack(project=r.project, region=r.region)
        succeeded += 1
        click.echo("    done.")

    click.echo(
        f"\n  {command_name} complete: {succeeded} destroyed, {len(failures)} failed."
    )
    if failures:
        for proj, why in failures:
            click.echo(f"    {proj}: {why}", err=True)
        sys.exit(1)


def _teardown_services(cluster, project_name):
    """Tear down ECS services for the cluster."""
    from remote_compose.models import ECSService as ECSServiceModel, TargetGroupConfig
    from remote_compose.services.ecs_service import ECSService

    ecs_logic = ECSService()
    services = ECSServiceModel.objects.filter(cluster=cluster)

    if not services.exists():
        click.echo("  No services to tear down.")
        return

    click.echo(f"  Tearing down {services.count()} services...")

    for svc in services:
        click.echo(f"    {svc.name}...", nl=False)
        try:
            if svc.aws_service_arn:
                try:
                    ecs_logic.update_service(svc, desired_count=0)
                except Exception:
                    pass
                try:
                    ecs_logic.delete_service(svc, force=True)
                except Exception:
                    pass
            svc.delete()
            click.echo(" removed")
        except Exception as e:
            click.echo(f" FAILED ({e})")

    tgs = TargetGroupConfig.objects.filter(cluster=cluster)
    if tgs.exists():
        click.echo(f"  Removing {tgs.count()} target groups...")
        from remote_compose.services.aws_client_factory import get_aws_client_factory

        factory = get_aws_client_factory()
        elbv2 = factory.get_client(
            "elbv2",
            region=cluster.aws_region,
            credential=cluster.aws_credential,
        )

        for tg in tgs:
            try:
                elbv2.delete_target_group(TargetGroupArn=tg.target_group_arn)
                tg.delete()
            except Exception:
                pass


def _teardown_infrastructure(cluster, force_delete_secrets: bool = False):
    """Tear down VPC, ALB, security groups, and other infrastructure."""
    click.echo("  Tearing down infrastructure...")

    try:
        lb = cluster.load_balancer
        if lb:
            click.echo(f"    ALB ({lb.alb_dns_name})...", nl=False)
            from remote_compose.services.aws_client_factory import (
                get_aws_client_factory,
            )

            factory = get_aws_client_factory()
            elbv2 = factory.get_client(
                "elbv2",
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )
            for arn in [lb.http_listener_arn, lb.https_listener_arn]:
                if arn:
                    try:
                        elbv2.delete_listener(ListenerArn=arn)
                    except Exception:
                        pass
            try:
                elbv2.delete_load_balancer(LoadBalancerArn=lb.alb_arn)
            except Exception:
                pass
            lb.delete()
            click.echo(" removed")
    except Exception:
        pass

    try:
        ns = cluster.service_connect_namespace
        if ns:
            click.echo(f"    Namespace ({ns.namespace_name})...", nl=False)
            from remote_compose.services.aws_client_factory import (
                get_aws_client_factory,
            )

            factory = get_aws_client_factory()
            sd = factory.get_client(
                "servicediscovery",
                region=cluster.aws_region,
                credential=cluster.aws_credential,
            )
            try:
                sd.delete_namespace(Id=ns.namespace_id)
            except Exception:
                pass
            ns.delete()
            click.echo(" removed")
    except Exception:
        pass

    from remote_compose.models import SecurityGroupConfig

    sgs = SecurityGroupConfig.objects.filter(cluster=cluster)
    if sgs.exists():
        click.echo(f"    {sgs.count()} security groups...", nl=False)
        from remote_compose.services.aws_client_factory import get_aws_client_factory

        factory = get_aws_client_factory()
        ec2 = factory.get_client(
            "ec2",
            region=cluster.aws_region,
            credential=cluster.aws_credential,
        )
        for sg in sgs:
            try:
                ec2.delete_security_group(GroupId=sg.security_group_id)
                sg.delete()
            except Exception:
                pass
        click.echo(" removed")

    try:
        vpc = cluster.vpc_infrastructure
        if vpc and vpc.is_managed:
            click.echo(f"    VPC ({vpc.vpc_id})...", nl=False)
            from remote_compose.services.vpc_service import VPCService

            vpc_service = VPCService()
            try:
                vpc_service.teardown_vpc(vpc)
            except Exception:
                pass
            click.echo(" removed")
    except Exception:
        pass

    from remote_compose.models import SecretConfig

    secrets = SecretConfig.objects.filter(cluster=cluster)
    if secrets.exists():
        click.echo(f"    {secrets.count()} secrets...", nl=False)
        from remote_compose.services.aws_client_factory import get_aws_client_factory

        factory = get_aws_client_factory()
        sm = factory.get_client(
            "secretsmanager",
            region=cluster.aws_region,
            credential=cluster.aws_credential,
        )
        for secret in secrets:
            try:
                if force_delete_secrets:
                    sm.delete_secret(
                        SecretId=secret.secret_arn,
                        ForceDeleteWithoutRecovery=True,
                    )
                else:
                    sm.delete_secret(SecretId=secret.secret_arn)
                secret.delete()
            except Exception:
                pass
        if force_delete_secrets:
            click.echo(" removed (force)")
        else:
            click.echo(" scheduled for deletion (30d recovery window)")


@click.command(name="destroy")
@click.option("--infra", is_flag=True, help="Also destroy VPC, ALB, etc.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--all-ephemeral",
    "all_ephemeral",
    is_flag=True,
    help="Destroy every stack in the ephemeral registry (deployed via "
    "rc deploy --ttl / rc up --ttl), regardless of TTL expiry. "
    "Single confirmation prompt covers all stacks.",
)
@click.option(
    "--force-delete-secrets",
    "force_delete_secrets",
    is_flag=True,
    help="Bypass AWS Secrets Manager 30-day recovery window when "
    "deleting per-secret blobs as part of teardown. Default "
    "(off) preserves the recovery window so a mistaken destroy "
    "is reversible — but the secret name stays reserved for 30 "
    "days, blocking re-create with the same name. Set this when "
    "you intend to immediately re-deploy with the same project "
    "name. (remote-compose-myw)",
)
@click.pass_context
def destroy_cmd(ctx, infra, yes, all_ephemeral, force_delete_secrets):
    """Tear down all services (prompts for confirmation)."""
    if all_ephemeral:
        from remote_compose.ephemeral import (
            DEFAULT_REGISTRY_PATH,
            list_records,
        )

        targets = list_records()
        if not targets:
            click.echo(f"  No ephemeral stacks in registry ({DEFAULT_REGISTRY_PATH}).")
            return
        click.echo(
            f"\nrc destroy --all-ephemeral — {len(targets)} stack(s) in registry:"
        )
        for r in targets:
            prof = f" profile={r.aws_profile}" if r.aws_profile else ""
            click.echo(
                f"  - {r.project} (region={r.region}{prof}) "
                f"expires_at={r.expires_at}"
            )
        _destroy_ephemeral_targets(
            targets, yes=yes, command_name="destroy --all-ephemeral"
        )
        return

    from remote_compose.cli_v2 import dispatch_if_v2, load_rc_yml

    if dispatch_if_v2(ctx.obj.get("config_path"), "destroy", yes=yes):
        try:
            from remote_compose.ephemeral import remove_stack

            cfg_path = ctx.obj.get("config_path") or "rc.yml"
            _, raw, v2 = load_rc_yml(cfg_path)
            if v2 is not None:
                ecs_cfg = (
                    ((raw.get("provider_config") or {}).get("ecs") or {})
                    if isinstance(raw, dict)
                    else {}
                )
                region = ecs_cfg.get("region")
                if region:
                    remove_stack(project=v2.project, region=region)
        except Exception:
            pass
        return

    config = _load_config(ctx.obj.get("config_path"))
    _bootstrap_django(config)

    project_name = config["project_name"]

    from remote_compose.models import ECSCluster

    try:
        cluster = ECSCluster.objects.get(name=config["cluster"])
    except ECSCluster.DoesNotExist:
        click.echo(f"Cluster '{config['cluster']}' not found. Nothing to destroy.")
        return

    scope = "all services and infrastructure" if infra else "all services"
    if not click.confirm(
        f"\nThis will destroy {scope} for {project_name} in {cluster.aws_region}.\n"
        f"Are you sure?"
    ):
        click.echo("Aborted.")
        return

    click.echo(f"\nRemote Compose — destroying {project_name}\n")

    _teardown_services(cluster, project_name)

    if infra:
        _teardown_infrastructure(cluster, force_delete_secrets=force_delete_secrets)

    click.echo("\n  Teardown complete.")


_LAUNCHD_LABEL = "com.remote-compose.reaper"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _install_reaper_schedule(interval_minutes: int) -> None:
    """Write + load a launchd job that runs `rc reap -y` every N minutes."""
    import os
    import shutil
    import subprocess

    if sys.platform != "darwin":
        click.echo(
            "  --install-schedule is macOS-only today. On Linux, add a cron "
            "entry like: */30 * * * * /abs/path/to/rc reap -y",
            err=True,
        )
        raise click.exceptions.Exit(2)

    rc_bin = shutil.which("rc")
    if not rc_bin:
        click.echo(
            "  Could not find 'rc' on PATH. Activate your venv first, "
            "or rerun from the env where 'rc' resolves.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / ".config" / "remote-compose"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "reaper.log"
    interval_seconds = max(60, int(interval_minutes) * 60)
    bin_dir = str(Path(rc_bin).parent)
    user_path = f"{bin_dir}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{rc_bin}</string>
        <string>reap</string>
        <string>-y</string>
    </array>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{user_path}</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)

    # Best-effort unload (idempotent) + load.
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        check=False,
        capture_output=True,
    )
    res = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        click.echo(
            f"  Wrote {plist_path} but launchctl load failed:\n"
            f"    {res.stderr.strip()}\n"
            f"  Load manually with: launchctl load {plist_path}",
            err=True,
        )
        raise click.exceptions.Exit(2)
    click.echo(
        f"  Installed {_LAUNCHD_LABEL} — runs `rc reap -y` every "
        f"{interval_seconds // 60} min."
    )
    click.echo(f"  plist: {plist_path}")
    click.echo(f"  log:   {log_path}")


def _uninstall_reaper_schedule() -> None:
    import subprocess

    if sys.platform != "darwin":
        click.echo("  --uninstall-schedule is macOS-only today.", err=True)
        raise click.exceptions.Exit(2)
    plist_path = _launchd_plist_path()
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            check=False,
            capture_output=True,
        )
        plist_path.unlink()
        click.echo(f"  Removed {plist_path}.")
    else:
        click.echo(f"  No reaper schedule installed at {plist_path}.")


@click.command(name="reap")
@click.option(
    "--dry-run", is_flag=True, help="List past-due stacks without destroying."
)
@click.option(
    "--all",
    "reap_all",
    is_flag=True,
    help="Destroy every ephemeral stack regardless of TTL.",
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option(
    "--install-schedule",
    "install_sched",
    is_flag=True,
    help="Install a launchd job (macOS) that runs `rc reap -y` periodically. "
    "Default interval 30 min — tune with --interval-minutes.",
)
@click.option(
    "--uninstall-schedule",
    "uninstall_sched",
    is_flag=True,
    help="Remove the launchd reaper job.",
)
@click.option(
    "--interval-minutes",
    "interval_minutes",
    default=30,
    show_default=True,
    type=int,
    help="Reaper schedule interval, in minutes. Used with --install-schedule.",
)
def reap_cmd(dry_run, reap_all, yes, install_sched, uninstall_sched, interval_minutes):
    """Destroy ephemeral stacks past their TTL.

    Reads the local registry (~/.config/remote-compose/ephemeral.json)
    written by `rc deploy --ttl ...`, finds entries whose expires_at is
    in the past, and runs `provider.destroy(ctx)` for each. A failure
    on one stack does not stop the rest. Successfully destroyed stacks
    are removed from the registry.
    """
    if install_sched:
        _install_reaper_schedule(interval_minutes)
        return
    if uninstall_sched:
        _uninstall_reaper_schedule()
        return

    from remote_compose.ephemeral import (
        DEFAULT_REGISTRY_PATH,
        list_records,
        find_expired,
    )

    if reap_all:
        targets = list_records()
        scope = "all ephemeral"
    else:
        targets = find_expired()
        scope = "past-due"

    if not targets:
        click.echo(f"  No {scope} stacks in registry ({DEFAULT_REGISTRY_PATH}).")
        return

    click.echo(f"\nrc reap — {len(targets)} {scope} stack(s):")
    for r in targets:
        prof = f" profile={r.aws_profile}" if r.aws_profile else ""
        click.echo(
            f"  - {r.project} (region={r.region}{prof}) " f"expires_at={r.expires_at}"
        )

    if dry_run:
        click.echo("\n  --dry-run: nothing destroyed.")
        return

    _destroy_ephemeral_targets(targets, yes=yes, command_name="Reap")
