"""rc dev — dev-host lifecycle commands.

Two flavors live under this group:
  - up/list/ssh/stop/start/destroy/status: EC2 dev-host (per-agent box)
  - push: legacy ECS hot-reload (kept for backwards compat with rc-e5u.45)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from remote_compose.terraform.runner import TerraformRunner


@click.group(name="dev")
def dev_group():
    """Dev-host lifecycle: spin per-agent EC2 boxes with docker + your repo + claude.

    \b
    Workflow:
      rc dev up alice                 # provision EC2, clone repo, start docker compose
      rc dev list                     # see all dev hosts
      rc dev ssh alice                # ssh into alice's box (claude is preinstalled)
      rc dev stop alice               # stop EC2 (EBS preserved)
      rc dev destroy alice            # tear down

    \b
    Source defaults to current directory's git remote + branch. Override:
      rc dev up alice --repo URL --branch main
      rc dev up alice --image nginx:alpine
    """


# ---------- helpers ----------


def _build_service():
    """Construct a wired-up DevHostService for CLI use.

    Uses FilesystemKeyStore for SSH keys (no Django dependency) and the
    boto3 default session for AWS calls (honors AWS_PROFILE env var).

    Imports dev_host_service directly to bypass services/__init__.py which
    pulls in Django-dependent modules (TargetService, AuditService, etc.).
    """
    import boto3

    # Direct submodule import — bypasses services/__init__.py Django imports.
    from remote_compose.dev_host.service import (
        DevHostService,
        FilesystemKeyStore,
    )

    class _BotoFactory:
        def get_client(self, service_name, region_name=None):
            return boto3.client(service_name, region_name=region_name)

    return DevHostService(
        credential_service=FilesystemKeyStore(),
        terraform_runner=None,  # set per-command via _runner_for(host_name)
        aws_client_factory=_BotoFactory(),
    )


def _runner_for(
    host_name: str, aws_profile: str | None = None, region: str | None = None
) -> "TerraformRunner":
    """Materialize the per-host terraform working dir and return a runner."""
    from shutil import copy2

    from remote_compose import terraform as tf_pkg
    from remote_compose.terraform.runner import TerraformRunner

    src_module = Path(tf_pkg.__file__).parent / "dev_host"
    work_dir = Path(".rc/terraform-state") / host_name
    work_dir.mkdir(parents=True, exist_ok=True)
    for f in src_module.iterdir():
        if f.is_file():
            copy2(f, work_dir / f.name)

    # Inherit shell env so PATH etc work; override AWS_PROFILE/REGION when given.
    env = dict(os.environ)
    if aws_profile:
        env["AWS_PROFILE"] = aws_profile
    if region:
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

    return TerraformRunner(
        work_dir,
        env=env,
        progress=lambda line: click.echo(f"  tf: {line}"),
    )


def _write_tfvars(host_name: str, variables: dict) -> Path:
    """Write terraform.tfvars.json into the per-host working dir."""
    import json

    work_dir = Path(".rc/terraform-state") / host_name
    work_dir.mkdir(parents=True, exist_ok=True)
    tfvars_path = work_dir / "terraform.tfvars.json"
    tfvars_path.write_text(json.dumps(variables, indent=2))
    return tfvars_path


# ---------- new EC2 dev-host commands ----------


@dev_group.command(name="up")
@click.argument("name")
@click.option(
    "--repo",
    "repos",
    multiple=True,
    help="Git repo URL (repeatable). With 2+ → MultiGitSource (multi-repo deploy). "
    "With 1 → GitSource. With 0 → auto-detect from cwd. Append '=<dir>' to override "
    "the on-box clone directory (default: repo basename), e.g. "
    "'https://github.com/owner/new-name=old-dir'.",
)
@click.option(
    "--branch",
    "branch",
    default=None,
    help="Branch / ref applied to all --repos (default: cwd HEAD or main).",
)
@click.option(
    "--compose",
    "compose_files",
    type=click.Path(exists=True),
    multiple=True,
    help="Top-level compose file(s) to SCP onto the host (repeatable). "
    "Each runs as a separate `docker compose -p <basename>` project "
    "so service-name conflicts across repos are avoided.",
)
@click.option(
    "--image",
    "image",
    default=None,
    help="Docker image to run (alternative to --repo).",
)
@click.option(
    "--instance-type",
    "instance_type",
    default="t4g.2xlarge",
    help="EC2 instance type (default: t4g.2xlarge ARM, 8 vCPU). Provisioning is "
    "dominated by CPU-bound docker image builds, so cores buy wall-clock: the "
    "same multi-repo stack took 20m on t4g.large (2 vCPU) and 10m43s on "
    "t4g.2xlarge. Drop to t4g.large to halve the hourly cost if you don't mind "
    "the wait; t4g.medium OOMs during builds.",
)
@click.option(
    "--region", "region", default=None, help="AWS region (default: rc.yml region)."
)
@click.option(
    "--ebs-size-gb",
    "ebs_size_gb",
    type=int,
    default=100,
    help="Root EBS size in GiB (default: 100).",
)
@click.option(
    "--spot/--no-spot",
    "spot",
    default=True,
    show_default=True,
    help="Request the instance as a persistent Spot Instance (~50-65% "
    "cheaper for the t4g family) instead of on-demand. Configured to STOP "
    "rather than terminate on reclamation, so `rc dev stop`/`start` keeps "
    "working — the tradeoff is `start` needing spare Spot capacity, which "
    "on-demand doesn't.",
)
@click.option(
    "--aws-profile",
    "aws_profile",
    default=None,
    help="AWS profile (default: $AWS_PROFILE or rc.yml aws_profile).",
)
@click.option(
    "--gh-token",
    "gh_token",
    default=None,
    envvar="GH_TOKEN",
    help="GitHub PAT for cloning private repos (default: $GH_TOKEN). "
    "WARNING: lands in EC2 user-data.",
)
@click.option(
    "--anthropic-key",
    "anthropic_key",
    default=None,
    envvar="ANTHROPIC_API_KEY",
    help="Pre-authenticate the in-box claude agent (default: $ANTHROPIC_API_KEY).",
)
@click.option(
    "--env",
    "env_files",
    multiple=True,
    type=click.Path(exists=True),
    help="Env file(s) to copy into the dev host (.envs/.local/.django etc). Repeatable.",
)
@click.option(
    "--port",
    "extra_ports",
    multiple=True,
    type=int,
    help="Extra TCP port(s) to open in the security group. Repeatable.",
)
@click.option(
    "--skip-permissions",
    "skip_permissions",
    is_flag=True,
    help="Boot the in-box claude with --dangerously-skip-permissions "
    "(autonomous mode, no per-tool confirmations).",
)
@click.option(
    "--no-claude-config",
    "no_claude_config",
    is_flag=True,
    help="Skip auto-copy of local ~/.claude config + auth (default: copy).",
)
@click.option(
    "--claude-config-from",
    "claude_config_from",
    type=click.Path(exists=True),
    default=None,
    help="Path to a custom .claude/ directory to copy (default: $HOME/.claude).",
)
@click.option(
    "--wait",
    "wait",
    is_flag=True,
    help=(
        "Block until cloud-init finishes (repos cloned, env files placed, "
        "compose up run). Without this, `up` returns while the box is still "
        "bootstrapping and is NOT yet usable."
    ),
)
@click.option(
    "--wait-timeout",
    "wait_timeout",
    type=int,
    default=1800,
    show_default=True,
    help="Seconds to wait for cloud-init when --wait is given.",
)
@click.pass_context
def dev_up_cmd(
    ctx,
    name,
    repos,
    branch,
    compose_files,
    image,
    instance_type,
    region,
    ebs_size_gb,
    spot,
    aws_profile,
    gh_token,
    anthropic_key,
    env_files,
    extra_ports,
    skip_permissions,
    no_claude_config,
    claude_config_from,
    wait,
    wait_timeout,
):
    """Provision an EC2 dev-host and start the source's docker compose."""
    from remote_compose.exceptions import RemoteComposeError
    from remote_compose.dev_host.bootstrap import (
        GitSource,
        ImageSource,
        MultiGitSource,
        detect_source_from_cwd,
    )

    # A --repo value may carry an optional '=<dir>' suffix that overrides the
    # on-box clone directory (default: the repo basename). This lets a renamed
    # repo keep a legacy checkout dir so compose includes that reference it by the
    # old name still resolve — e.g. '…/debuggai-api=sentinal' clones the (renamed)
    # repo into 'sentinal/'. A git URL never ends in '=<bare-word>', so splitting
    # on a trailing '=<segment-without-/-or-:>' is unambiguous.
    def _parse_repo_spec(spec: str) -> dict:
        repo = {"url": spec, "ref": branch or "main"}
        head, sep, tail = spec.rpartition("=")
        if sep and head and tail and "/" not in tail and ":" not in tail:
            repo["url"] = head
            repo["target"] = tail
        return repo

    # Resolve source: 2+ repos → multi; 1 repo → single git; 0 + image → image;
    # 0 + nothing → autodetect from cwd.
    if len(repos) >= 2:
        if not compose_files:
            click.echo(
                "Error: at least one --compose <file> is required when passing 2+ --repo flags.",
                err=True,
            )
            sys.exit(1)
        source = MultiGitSource(
            repos=[_parse_repo_spec(u) for u in repos],
            compose_filenames=[Path(c).name for c in compose_files],
        )
    elif image:
        source = ImageSource(image=image)
    elif len(repos) == 1:
        _spec = _parse_repo_spec(repos[0])
        source = GitSource(url=_spec["url"], ref=_spec["ref"])
    else:
        try:
            source = detect_source_from_cwd()
        except RemoteComposeError as exc:
            click.echo(f"Error: {exc}", err=True)
            click.echo("Hint: pass --repo / --branch or --image explicitly.", err=True)
            sys.exit(1)
        if branch:
            source.ref = branch

    # Inject secrets — GitSource and MultiGitSource share the same fields
    if isinstance(source, (GitSource, MultiGitSource)):
        if gh_token:
            source.gh_token = gh_token
            # rc-h40: token leaks easily when passed via flag (shell history,
            # /tmp/* logs from wrappers, ps -aux). Prefer env var.
            if os.environ.get("GH_TOKEN") != gh_token:
                click.echo(
                    "  ⚠️  --gh-token was passed as a flag — leaks into shell "
                    "history and any wrapper-script logs. Prefer "
                    "'GH_TOKEN=$(gh auth token) rc dev up ...' (envvar is "
                    "auto-picked up).",
                    err=True,
                )
        if anthropic_key:
            source.extra_env = dict(source.extra_env or {})
            source.extra_env["ANTHROPIC_API_KEY"] = anthropic_key
        if skip_permissions:
            source.skip_permissions = True

    # Resolve region
    if not region:
        region = (
            _region_from_rc_yml(ctx)
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-west-1"
        )

    click.echo(f"Provisioning dev-host '{name}' in {region} ({instance_type})...")
    # rc-h40: sanitize source repr so secrets (gh_token, ANTHROPIC_API_KEY)
    # in dataclass fields don't print to stdout / scrollback / wrapper logs.
    click.echo(f"  source: {_sanitized_source_repr(source)}")
    if isinstance(source, (GitSource, MultiGitSource)) and source.skip_permissions:
        click.echo("  claude: --dangerously-skip-permissions (autonomous mode)")
    click.echo(
        "  capacity: spot (persistent, stops on reclamation)"
        if spot
        else "  capacity: on-demand"
    )

    if not aws_profile:
        aws_profile = _aws_profile_from_rc_yml(ctx) or os.environ.get("AWS_PROFILE")

    service = _build_service()
    service.terraform_runner = _runner_for(name, aws_profile=aws_profile, region=region)

    # Pre-flight: init terraform working dir (downloads aws provider)
    service.terraform_runner.init(backend=False)

    try:
        record = service.create_host(
            name=name,
            source=source,
            instance_type=instance_type,
            region=region,
            ebs_size_gb=ebs_size_gb,
            spot=spot,
        )
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"\n  ✓ dev-host '{name}' is {record.status}")
    click.echo(f"  instance_id: {record.instance_id}")
    click.echo(f"  public_ip:   {record.public_ip}")
    click.echo(f"  ssh:         rc dev ssh {name}")
    click.echo(f"  attach claude: rc dev attach {name}")

    # rc-bd7: secrets never go into user-data, so the box's bootstrap is
    # sitting in a bounded wait for them right now — it cannot clone a private
    # repo until they land. Deliver FIRST, before ports/env files/claude
    # config, so the clone unblocks as early as possible.
    secret_env = (
        source.secret_env_content() if hasattr(source, "secret_env_content") else ""
    )
    if secret_env and record.public_ip:
        click.echo("\n  Delivering secrets over SSH (never via user-data)...")
        keypair = service.credential_service.get_credential(
            record.ssh_key_credential_id
        )
        priv_pem, _ = service.credential_service.get_ssh_keypair(keypair)
        try:
            _deliver_secret_env(record.public_ip, priv_pem, secret_env)
            click.echo("  ✓ secrets delivered — bootstrap can clone private repos")
        except Exception as exc:
            # Non-fatal on purpose: the box is up and billing either way, and
            # `rc dev ssh` still works. Say plainly what will be broken.
            click.echo(
                f"  ! secret delivery failed ({exc}). The box is running, but "
                f"private clones and `gh` will not work on it. Re-run "
                f"`rc dev up` or place the values in /home/ec2-user/.rc-dev-env "
                f"by hand.",
                err=True,
            )

    # rc-5c0: when user didn't pass --port, default to whatever host ports
    # the compose file(s) actually publish. Avoids silent SG/compose drift
    # (deploy works, curl fails because port mapped to 8012 but SG opens 8002).
    if not extra_ports and compose_files:
        detected = _ports_from_compose(compose_files)
        if detected:
            click.echo(f"  auto-detected compose host ports: {detected}")
            extra_ports = tuple(detected)
    # Open extra ports in the SG (compose-declared ports user wants reachable)
    if extra_ports:
        click.echo(f"\n  Opening security group ports: {list(extra_ports)}")
        sg_id = _sg_id_for_instance(record.instance_id, region, aws_profile)
        if sg_id:
            for port in extra_ports:
                _authorize_sg_port(sg_id, int(port), region, aws_profile)
                click.echo(f"  ✓ port {port} open")

    # Copy env files into the box (requires SSH to be up — wait for it)
    if env_files or compose_files:
        click.echo("\n  Copying files into the box — waiting for SSH...")
        keypair = service.credential_service.get_credential(
            record.ssh_key_credential_id
        )
        priv_pem, _ = service.credential_service.get_ssh_keypair(keypair)
        repo_name = ""
        if isinstance(source, GitSource):
            repo_name = source.url.rstrip("/").split("/")[-1].removesuffix(".git")
        elif isinstance(source, MultiGitSource):
            repo_name = "_envs"
        if env_files:
            _wait_for_ssh_and_copy_env(
                record.public_ip, priv_pem, env_files, repo_name or "workspace"
            )
            click.echo(
                "  ✓ env files staged in /tmp/rc-dev-envs/ — bootstrap places them post-clone"
            )
        for cf in compose_files:
            _scp_compose_file(record.public_ip, priv_pem, cf)
            click.echo(f"  ✓ compose file copied to /home/ec2-user/{Path(cf).name}")

    # Auto-copy local Claude config + auth (default ON; --no-claude-config to opt out)
    if not no_claude_config:
        keypair = service.credential_service.get_credential(
            record.ssh_key_credential_id
        )
        priv_pem, _ = service.credential_service.get_ssh_keypair(keypair)
        click.echo("\n  Copying local Claude config (~/.claude + ~/.claude.json)...")
        try:
            _copy_claude_config(
                record.public_ip,
                priv_pem,
                claude_dir=Path(claude_config_from) if claude_config_from else None,
            )
            click.echo(
                "  ✓ in-box claude is pre-authenticated — `rc dev attach` lands ready"
            )
        except Exception as exc:
            click.echo(
                f"  ! claude config copy failed: {exc} — you'll need to login on first attach",
                err=True,
            )

    # Everything above returns as soon as EC2 + SG + SCP are done. cloud-init is
    # still installing docker, cloning the repos, PLACING THE ENV FILES and
    # running `docker compose up` for several more minutes, so the box is not
    # usable yet. Be explicit about that rather than implying success.
    if wait and record.public_ip:
        keypair = service.credential_service.get_credential(
            record.ssh_key_credential_id
        )
        priv_pem, _ = service.credential_service.get_ssh_keypair(keypair)
        click.echo(f"\n  Waiting for cloud-init to finish (timeout {wait_timeout}s)...")
        if _wait_for_cloud_init(record.public_ip, priv_pem, wait_timeout):
            click.echo("  ✓ cloud-init done — repos cloned, compose up run")
            # cloud-init reaching 'done' does NOT mean the compose projects
            # started: the bootstrap keeps going past a failed project so the
            # others still get a chance. Report per-project status, and do not
            # claim success if any of them failed.
            failed = _failed_compose_projects(record.public_ip, priv_pem)
            if failed:
                for project, code in failed:
                    click.echo(
                        f"  ! compose project '{project}' FAILED (exit {code}) — "
                        f"log: ~/rc-dev-compose-{project}.log",
                        err=True,
                    )
                click.echo(
                    f"  the box is only partly provisioned. Inspect with "
                    f"`rc dev ssh {name}`.",
                    err=True,
                )
                sys.exit(1)

            # `compose up -d` returning is not the same as the services being
            # reachable: containers report Up while Django is still migrating and
            # Next.js is still compiling its first route, so --wait would hand
            # back a box whose ports answer nothing for another few minutes.
            # The --port list is exactly the set the user said they care about.
            if extra_ports:
                click.echo(
                    f"  waiting for ports {list(extra_ports)} to accept "
                    f"connections..."
                )
                pending = _wait_for_ports(
                    record.public_ip, [int(p) for p in extra_ports]
                )
                if pending:
                    click.echo(
                        f"  ! ports still not listening: {pending} — the stack "
                        f"may still be warming up, or a service failed to bind. "
                        f"Check `rc dev logs {name}`.",
                        err=True,
                    )
                else:
                    click.echo("  ✓ all requested ports are accepting connections")
        else:
            click.echo(
                f"  ! cloud-init did not report 'done' within {wait_timeout}s — "
                f"check `rc dev ssh {name}` then `cloud-init status`",
                err=True,
            )
            sys.exit(1)
    else:
        click.echo(
            "\n  NOTE: cloud-init is still bootstrapping (docker, clones, env "
            "files, compose up). The box is not usable yet — re-run with --wait "
            "to block until it is."
        )


def _wait_for_ports(
    public_ip: str, ports: list[int], timeout: int = 600, interval: int = 10
) -> list[int]:
    """Wait for each port to accept a TCP connection. Returns those that never did.

    A plain TCP connect rather than an HTTP probe: rc has no idea what protocol
    a given --port speaks, and a Django app answering 400 to a bare-IP Host
    (ALLOWED_HOSTS) is up as far as provisioning is concerned. "Something is
    listening" is the strongest claim rc can honestly make.

    Deliberately advisory — provisioning already succeeded by this point, and a
    service that is merely slow to bind is not a provisioning failure.
    """
    import socket
    import time

    deadline = time.time() + timeout
    pending = list(ports)
    while pending and time.time() < deadline:
        still_pending = []
        for port in pending:
            try:
                with socket.create_connection((public_ip, port), timeout=5):
                    pass
            except OSError:
                still_pending.append(port)
        pending = still_pending
        if pending and time.time() < deadline:
            time.sleep(interval)
    return pending


def _failed_compose_projects(public_ip: str, private_pem: str) -> list[tuple[str, int]]:
    """Return [(project, exit_code)] for compose projects that failed to start.

    The bootstrap writes one `<project>\\t<exit_code>` line per compose file to
    ~/.rc-dev-compose-status. It deliberately continues past a failing project so
    the remaining ones still get attempted, which means cloud-init can reach
    'done' with services missing — `up` must not report success in that case.

    An unreadable/absent status file yields [] (older box, or a source type that
    runs no compose), so this never invents a failure.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-i",
                keypath,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                f"ec2-user@{public_ip}",
                "cat ~/.rc-dev-compose-status 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.SubprocessError:
        return []

    failed: list[tuple[str, int]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        project, raw = parts[0].strip(), parts[1].strip()
        try:
            code = int(raw)
        except ValueError:
            continue
        if code != 0:
            failed.append((project, code))
    return failed


def _wait_for_cloud_init(public_ip: str, private_pem: str, timeout: int = 1800) -> bool:
    """Block until cloud-init reports 'done' on the box.

    `rc dev up` otherwise returns while the bootstrap is still running, so
    anything that SSHes in immediately afterwards races it — the classic symptom
    is a caller finding the env files it just passed via --env "missing", on a
    box that is perfectly fine minutes later.

    Returns True on 'done', False on timeout or an error/degraded status.
    """
    import subprocess
    import tempfile
    import time

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)
    ssh_opts = [
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
    ]
    # `cloud-init status --wait` blocks and streams dots; discard those and read
    # the terminal status. SSH may not be up for the first few tries.
    remote_cmd = "sudo cloud-init status --wait >/dev/null 2>&1; cloud-init status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        try:
            proc = subprocess.run(
                ["ssh"] + ssh_opts + [f"ec2-user@{public_ip}", remote_cmd],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return False
        out = (proc.stdout or "").strip().lower()
        if "done" in out:
            return True
        if "error" in out or "degraded" in out:
            return False
        time.sleep(5)
    return False


def _all_enabled_regions(service) -> list[str]:
    """Every enabled region for the account (for the leak sweep)."""
    try:
        ec2 = service.aws_client_factory.get_client("ec2", region_name="us-east-1")
        resp = ec2.describe_regions(AllRegions=False)
        return sorted(r["RegionName"] for r in resp.get("Regions", []))
    except Exception:
        return []


@dev_group.command(name="list")
@click.option(
    "--region",
    "regions",
    multiple=True,
    help="Region(s) to scan for rc-dev boxes (repeatable). "
    "Default: regions in local state + us-west-1.",
)
@click.option(
    "--all-regions",
    is_flag=True,
    help="Sweep EVERY enabled region — catches leaked boxes anywhere.",
)
def dev_list_cmd(regions, all_regions):
    """List dev hosts from LIVE AWS (tag ManagedBy=rc-dev), merged with local
    state. Surfaces boxes that are billing regardless of the directory you run
    from, and flags drift (untracked AWS-only boxes / stale state entries)."""
    service = _build_service()

    if all_regions:
        scan = _all_enabled_regions(service)
    elif regions:
        scan = list(regions)
    else:
        # state regions + the rc-dev default so a plain `list` covers the
        # common case without a full sweep.
        state_regions = {h.region for h in service.list_hosts() if h.region}
        scan = sorted(state_regions | {"us-west-1"})

    try:
        hosts = service.reconcile_live(scan)
    except Exception as exc:  # AWS unreachable — fall back, don't hide
        click.echo(
            f"warning: live AWS query failed ({exc}); showing local state only.",
            err=True,
        )
        hosts = service.list_hosts()

    # rc-n14: if a plain (non-swept) list finds nothing, auto-widen to all
    # regions so an empty/cwd-wrong state file can't hide billing boxes.
    if not hosts and not all_regions and not regions:
        all_scan = _all_enabled_regions(service)
        if all_scan:
            click.echo("  (nothing in default regions — sweeping all regions…)")
            hosts = service.reconcile_live(all_scan)

    if not hosts:
        click.echo("No dev hosts (checked live AWS). Try: rc dev up <name>")
        return

    click.echo(
        f"{'NAME':<22} {'STATUS':<10} {'TYPE':<14} {'CAP':<8} {'REGION':<12} "
        f"{'PUBLIC_IP':<16} {'TRACKED':<10}"
    )
    for h in sorted(hosts, key=lambda r: (r.region or "", r.name)):
        if h.tracked is False:
            tracked = "untracked"  # in AWS, not in this local state file
        elif h.status == "gone":
            tracked = "stale"  # in state, not in AWS
        else:
            tracked = "yes"
        cap = "spot" if h.spot else "ondmd"
        click.echo(
            f"{h.name:<22} {h.status:<10} {h.instance_type or '-':<14} {cap:<8} "
            f"{h.region or '-':<12} {h.public_ip or '-':<16} {tracked:<10}"
        )
    untracked = [h for h in hosts if h.tracked is False]
    if untracked:
        click.echo(
            f"\n  ⚠ {len(untracked)} untracked box(es) billing but not in this "
            f"local state file (.rc/dev-hosts.yml is cwd-relative). If abandoned, "
            f"terminate: aws ec2 terminate-instances --instance-ids <id> "
            f"--region <region>"
        )


def _claude_session_command(source: dict) -> str:
    """Shell command that (re)launches the in-box claude session.

    Mirrors what cloud-init's `runcmd` originally launched for this source's
    `type`, so a session rebuilt after a stop/start (or a dead-session attach
    fallback) comes back identical to first boot — same working dir, same
    `--dangerously-skip-permissions` flag if the box was provisioned with it.
    `runcmd` only ever fires on first boot, so nothing re-derives this later
    unless we do it explicitly.

    Always includes --continue: resumes the agent's most recent conversation
    for this directory if one exists (stop/start, a Spot interruption that
    stopped the box). The box surviving a stop is only half the story —
    without this, the relaunched agent has no memory of what it was doing
    even though the code on disk is untouched.

    Falls back to a bare `claude {flags}` if --continue fails, via a shell
    `||` baked into the returned command — load-bearing, not defensive
    filler. Measured live: `claude --continue` in interactive mode can exit
    1 with "No deferred tool marker found in the resumed session" (seen on a
    box whose copied ~/.claude.json carried a stale deferred-tool
    reference), and a command that exits non-zero as a detached tmux pane's
    sole process kills the pane, the window, and the whole tmux SERVER along
    with it — `rc dev attach` then has nothing to attach to at all.
    """
    from remote_compose.dev_host.bootstrap import _repo_name_from_url

    flags = "--dangerously-skip-permissions" if source.get("skip_permissions") else ""
    if source.get("type") == "git" and source.get("url"):
        repo_name = _repo_name_from_url(source["url"])
        cd = f"cd /home/ec2-user/{repo_name} 2>/dev/null || cd /home/ec2-user"
    else:
        # multi-git/image/local/script sources all land at /home/ec2-user.
        cd = "cd /home/ec2-user"
    claude_cmd = f"claude --continue {flags}".strip()
    fallback_cmd = f"claude {flags}".strip()
    return f"{cd}; {claude_cmd} || {fallback_cmd}"


def _ssh_opts(keypath: str) -> list[str]:
    return [
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
    ]


def _wait_for_ssh_ready(public_ip: str, private_pem: str, timeout: int = 180) -> bool:
    """Poll until the box accepts an SSH command, or timeout."""
    import subprocess
    import tempfile
    import time

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                ["ssh"] + _ssh_opts(keypath) + [f"ec2-user@{public_ip}", "true"],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0:
                return True
        except subprocess.SubprocessError:
            pass
        time.sleep(5)
    return False


def _relaunch_claude_session(public_ip: str, private_pem: str, source: dict) -> bool:
    """(Re)create the `claude` tmux session on a box that's already reachable.

    A no-op if the session is somehow already alive (e.g. a very fast
    stop/start that didn't actually kill it) — `tmux has-session` short
    circuits the recreation.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)

    launch_cmd = _claude_session_command(source)
    remote_cmd = (
        "tmux has-session -t claude 2>/dev/null && exit 0; "
        f"tmux new-session -d -s claude -x 220 -y 50 '{launch_cmd}'"
    )
    try:
        proc = subprocess.run(
            ["ssh"] + _ssh_opts(keypath) + [f"ec2-user@{public_ip}", remote_cmd],
            capture_output=True,
            timeout=30,
        )
        return proc.returncode == 0
    except subprocess.SubprocessError:
        return False


@dev_group.command(name="attach")
@click.argument("name")
@click.option(
    "--session",
    "session",
    default="claude",
    help="tmux session name to attach to (default: claude).",
)
def dev_attach_cmd(name, session):
    """Attach to the in-box claude tmux session (or create one)."""
    from remote_compose.exceptions import RemoteComposeError

    service = _build_service()
    try:
        record = service.get_host(name)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not record.public_ip:
        click.echo(f"Error: dev host '{name}' has no public IP yet.", err=True)
        sys.exit(1)

    cred = service.credential_service.get_credential(record.ssh_key_credential_id)
    private_pem, _ = service.credential_service.get_ssh_keypair(cred)

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as keyfile:
        keyfile.write(private_pem.encode("utf-8"))
        keypath = keyfile.name
    os.chmod(keypath, 0o600)

    # -t forces a TTY so tmux works through ssh
    # `tmux -u` forces UTF-8 output regardless of the client's locale.
    #
    # Without it the agent UI renders as streams of `qqqqqqqq` and stray `m`/`l`
    # characters: tmux decides whether the CLIENT can do UTF-8 by inspecting its
    # locale, and `ssh host '<cmd>'` is a non-login, non-interactive shell — so
    # /etc/profile.d never runs and LANG is empty. tmux concludes the terminal
    # is not UTF-8 capable and re-encodes the pane's box-drawing into ACS
    # (alternate character set) escapes, which is what those letters are.
    # Measured on a live box, capturing the raw attach stream:
    #   tmux attach                   -> 65 ACS escapes,  0 UTF-8 box chars
    #   LANG=C.UTF-8 tmux attach      ->  0 ACS escapes, 634 UTF-8 box chars
    #   tmux -u attach                ->  0 ACS escapes, 634 UTF-8 box chars
    # Note this is a CLIENT-side decision: fixing TERM/locale for the pane
    # process on the box does not help, because the re-encoding happens when
    # tmux paints the attaching terminal. LANG is exported too so anything the
    # user runs inside the session inherits a sane locale.
    launch_cmd = _claude_session_command(record.source)
    remote_cmd = (
        'export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}"; '
        f"tmux -u attach -t {session} 2>/dev/null || "
        f"tmux -u new-session -s {session} '{launch_cmd}'"
    )
    cmd = [
        "ssh",
        "-t",
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"ec2-user@{record.public_ip}",
        remote_cmd,
    ]
    click.echo(f"$ {' '.join(cmd[:8])} ...")
    os.execvp("ssh", cmd)


@dev_group.command(name="ssh")
@click.argument("name")
def dev_ssh_cmd(name):
    """SSH into a dev host (uses the auto-generated key)."""
    from remote_compose.exceptions import RemoteComposeError

    service = _build_service()
    try:
        record = service.get_host(name)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not record.public_ip:
        click.echo(f"Error: dev host '{name}' has no public IP yet.", err=True)
        sys.exit(1)

    if not record.ssh_key_credential_id:
        click.echo(f"Error: no SSH key registered for '{name}'.", err=True)
        sys.exit(1)

    # materialize the private key as a temp file for ssh -i
    cred = service.credential_service.get_credential(record.ssh_key_credential_id)
    private_pem, _ = service.credential_service.get_ssh_keypair(cred)

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as keyfile:
        keyfile.write(private_pem.encode("utf-8"))
        keypath = keyfile.name
    os.chmod(keypath, 0o600)

    cmd = [
        "ssh",
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"ec2-user@{record.public_ip}",
    ]
    click.echo(f"$ {' '.join(cmd)}")
    os.execvp("ssh", cmd)


@dev_group.command(name="stop")
@click.argument("name")
def dev_stop_cmd(name):
    """Stop the EC2 instance (EBS preserved)."""
    from remote_compose.exceptions import RemoteComposeError

    service = _build_service()
    try:
        service.stop_host(name)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"  ✓ stopped '{name}'")


@dev_group.command(name="start")
@click.argument("name")
def dev_start_cmd(name):
    """Start a previously stopped EC2 dev host."""
    from remote_compose.exceptions import RemoteComposeError

    service = _build_service()
    try:
        service.start_host(name)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"  ✓ started '{name}'")

    # `stop`/`start` is a real EC2 power-off, not a suspend — it kills the
    # in-box tmux server. cloud-init's `runcmd` (which launched the original
    # `claude {flags}` session) only ever runs on first boot, so without this
    # the box comes back with no claude session at all, and a subsequent
    # `rc dev attach` used to fall back to a bare, unflagged `claude` —
    # silently dropping --dangerously-skip-permissions and re-triggering the
    # folder-trust prompt every time.
    record = service.get_host(name)
    if not record.public_ip:
        return
    cred = service.credential_service.get_credential(record.ssh_key_credential_id)
    private_pem, _ = service.credential_service.get_ssh_keypair(cred)

    click.echo("  waiting for SSH to restore the claude session...")
    if _wait_for_ssh_ready(record.public_ip, private_pem) and _relaunch_claude_session(
        record.public_ip, private_pem, record.source
    ):
        click.echo("  ✓ claude tmux session restored")
    else:
        click.echo(
            "  ⚠️  couldn't confirm the claude session restarted — "
            f"check with `rc dev attach {name}`.",
            err=True,
        )


@dev_group.command(name="destroy")
@click.argument("name")
@click.option("--force", is_flag=True, help="Tear down even if not in state.")
@click.option("--aws-profile", "aws_profile", default=None, help="AWS profile.")
@click.pass_context
def dev_destroy_cmd(ctx, name, force, aws_profile):
    """Tear down the dev host (instance, SG, EIP, key)."""
    from remote_compose.exceptions import RemoteComposeError

    if not aws_profile:
        aws_profile = _aws_profile_from_rc_yml(ctx) or os.environ.get("AWS_PROFILE")

    service = _build_service()
    try:
        record = service.get_host(name)
        region = record.region
    except RemoteComposeError:
        region = None

    service.terraform_runner = _runner_for(name, aws_profile=aws_profile, region=region)
    try:
        service.destroy_host(name, force=force)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Terraform state lives in ./.rc/terraform-state/<name>, i.e. it is relative
    # to the CURRENT WORKING DIRECTORY. Running destroy from a different dir than
    # the one that ran `up` gives terraform an empty state: it destroys nothing,
    # exits 0, and we used to print "✓ destroyed" over the top of a box that is
    # still running — and still billing. Verify against AWS before claiming it.
    alive = _live_instance_states_settled(name, region, aws_profile)
    if alive:
        click.echo(
            f"  ! '{name}' still exists in AWS (state: {', '.join(sorted(alive))}) "
            f"— terraform destroyed nothing.",
            err=True,
        )
        click.echo(
            "  Terraform state is per-directory (./.rc/terraform-state/). Re-run "
            "destroy from the directory you ran `rc dev up` in.",
            err=True,
        )
        sys.exit(1)

    # An unassociated Elastic IP bills forever and is invisible unless you go
    # looking. terraform tags ours rc-dev-<name>-eip, so once the host is gone
    # any address still carrying that tag is definitionally orphaned — release
    # it rather than leaving a silent monthly charge behind.
    for ip in _release_orphaned_eips(name, region, aws_profile):
        click.echo(f"  ✓ released orphaned Elastic IP {ip} (rc-dev-{name}-eip)")

    click.echo(f"  ✓ destroyed '{name}'")


def _release_orphaned_eips(
    name: str, region: str | None, aws_profile: str | None
) -> list[str]:
    """Release unassociated EIPs tagged for this dev host. Returns those freed.

    Strictly scoped: the address must carry BOTH this host's
    rc-dev-<name>-eip Name tag and ManagedBy=rc-dev, and must not be attached to
    anything. Best-effort — an AWS error here must not fail an otherwise
    successful destroy.
    """
    try:
        import boto3

        session = (
            boto3.Session(profile_name=aws_profile) if aws_profile else boto3.Session()
        )
        client = session.client("ec2", region_name=region)
        resp = client.describe_addresses(
            Filters=[
                {"Name": "tag:Name", "Values": [f"rc-dev-{name}-eip"]},
                {"Name": "tag:ManagedBy", "Values": ["rc-dev"]},
            ]
        )
    except Exception:
        return []

    released = []
    for addr in resp.get("Addresses", []):
        if addr.get("AssociationId"):
            continue  # still attached to something — leave it alone
        alloc_id = addr.get("AllocationId")
        if not alloc_id:
            continue
        try:
            client.release_address(AllocationId=alloc_id)
            released.append(addr.get("PublicIp", alloc_id))
        except Exception:
            continue
    return released


def _live_instance_states_settled(
    name: str,
    region: str | None,
    aws_profile: str | None,
    timeout: int = 60,
    interval: int = 5,
) -> set[str]:
    """Like _live_instance_states, but tolerant of EC2's eventual consistency.

    describe-instances can still report 'running' for a few seconds after
    terraform has torn the instance down. Checking once immediately after
    destroy therefore flagged perfectly successful teardowns — observed live:
    the warning fired, and the instance read 'terminated' seconds later. A check
    that cries wolf on success gets ignored, which is worse than no check.

    Polls until the instance reads terminated/shutting-down, or the timeout
    expires; only then is it treated as genuinely still alive.
    """
    import time

    deadline = time.time() + timeout
    alive = _live_instance_states(name, region, aws_profile)
    while alive and time.time() < deadline:
        time.sleep(interval)
        alive = _live_instance_states(name, region, aws_profile)
    return alive


def _live_instance_states(
    name: str, region: str | None, aws_profile: str | None
) -> set[str]:
    """Instance states for dev-host `name` that are not terminated/shutting-down.

    Used to confirm a destroy actually destroyed something. Best-effort: if the
    lookup itself fails we return an empty set rather than blocking the command
    on an unrelated AWS/credentials problem.
    """
    try:
        import boto3

        session = (
            boto3.Session(profile_name=aws_profile) if aws_profile else boto3.Session()
        )
        client = session.client("ec2", region_name=region)
        resp = client.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [f"*{name}*"]},
                {"Name": "tag:ManagedBy", "Values": ["rc-dev"]},
            ]
        )
    except Exception:
        return set()

    states = set()
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            state = inst.get("State", {}).get("Name", "")
            if state not in ("terminated", "shutting-down"):
                states.add(state)
    return states


@dev_group.command(name="logs")
@click.argument("name")
@click.option(
    "--service",
    "service_name",
    default=None,
    help="Tail logs for one compose service (default: all).",
)
@click.option("--follow", "-f", is_flag=True, help="Stream logs continuously.")
@click.option(
    "--tail", "tail_n", type=int, default=100, help="Lines per service (default 100)."
)
def dev_logs_cmd(name, service_name, follow, tail_n):
    """Tail docker compose logs from the dev host."""
    from remote_compose.exceptions import RemoteComposeError

    service = _build_service()
    try:
        record = service.get_host(name)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not record.public_ip:
        click.echo(f"Error: dev host '{name}' has no public IP yet.", err=True)
        sys.exit(1)

    cred = service.credential_service.get_credential(record.ssh_key_credential_id)
    private_pem, _ = service.credential_service.get_ssh_keypair(cred)

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as keyfile:
        keyfile.write(private_pem.encode("utf-8"))
        keypath = keyfile.name
    os.chmod(keypath, 0o600)

    # Best-effort repo dir guess (matches what cloud-init creates)
    repo_dir = "$(ls -d /home/ec2-user/*/ 2>/dev/null | head -1)"
    flags = f"--tail={tail_n}" + (" -f" if follow else "")
    svc_arg = service_name or ""
    remote = (
        f"cd {repo_dir} 2>/dev/null && "
        f"sudo docker compose -f $(ls docker-compose.yml local.yml compose.yml 2>/dev/null | head -1) "
        f"logs {flags} {svc_arg}"
    )
    cmd = [
        "ssh",
        "-t",
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"ec2-user@{record.public_ip}",
        remote,
    ]
    os.execvp("ssh", cmd)


@dev_group.command(name="status")
@click.argument("name")
def dev_status_cmd(name):
    """Show dev-host status, IP, source, uptime."""
    from remote_compose.exceptions import RemoteComposeError

    service = _build_service()
    try:
        record = service.get_host(name)
    except RemoteComposeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"name:          {record.name}")
    click.echo(f"status:        {record.status}")
    click.echo(f"capacity:      {'spot' if record.spot else 'on-demand'}")
    click.echo(f"instance_type: {record.instance_type}")
    click.echo(f"region:        {record.region}")
    click.echo(f"ami:           {record.ami}")
    click.echo(f"instance_id:   {record.instance_id}")
    click.echo(f"public_ip:     {record.public_ip}")
    click.echo(f"public_dns:    {record.public_dns}")
    click.echo(f"created_at:    {record.created_at}")
    # rc-bd7: record.source is redacted on load, but print it through the
    # sanitizer anyway — this line is one keystroke from a terminal scrollback
    # or a screen share, and defence in depth costs nothing here.
    click.echo(f"source:        {_sanitized_source_dict(record.source)}")


# ---------- helpers shared with existing rc dev push ----------


def _region_from_rc_yml(ctx) -> str | None:
    """Best-effort load region from rc.yml — returns None if unavailable."""
    import yaml

    config_path = (ctx.obj or {}).get("config_path") or "rc.yml"
    p = Path(config_path)
    if not p.exists():
        return None
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError:
        return None
    return cfg.get("region") or (cfg.get("aws") or {}).get("region")


def _sg_id_for_instance(
    instance_id: str | None, region: str, aws_profile: str | None
) -> str | None:
    """Look up the SG attached to a dev-host instance."""
    if not instance_id:
        return None
    import boto3

    session = boto3.Session(profile_name=aws_profile, region_name=region)
    ec2 = session.client("ec2")
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    try:
        return resp["Reservations"][0]["Instances"][0]["SecurityGroups"][0]["GroupId"]
    except (IndexError, KeyError):
        return None


def _authorize_sg_port(
    sg_id: str, port: int, region: str, aws_profile: str | None
) -> None:
    """Best-effort open one TCP port in a security group; ignore "already exists"."""
    import boto3
    from botocore.exceptions import ClientError

    session = boto3.Session(profile_name=aws_profile, region_name=region)
    ec2 = session.client("ec2")
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
    except ClientError as exc:
        if "InvalidPermission.Duplicate" in str(exc):
            return
        raise


def _read_claude_credentials() -> str | None:
    """Get the OAuth credentials JSON. Linux: file. macOS: keychain.

    Linux Claude Code stores OAuth in ~/.claude/.credentials.json. macOS
    Claude Code stores it in the user's Keychain under service name
    'Claude Code-credentials' (no on-disk file). Without these credentials
    the in-box claude shows 'Not logged in' even with .claude.json present.
    """
    import subprocess
    import sys

    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        return creds_file.read_text()
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "Claude Code-credentials",
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
    return None


def _build_claude_config_tarball(claude_dir: Path, claude_json: Path) -> Path:
    """Tar up ONLY the small auth + settings files from ~/.claude.

    Skips projects/, backups/, cache/, history.jsonl, etc. — those are
    per-machine session state, not auth, and total ~7GB on a typical install.
    Returns path to the gz'd tarball (caller is responsible for cleanup).
    """
    import tarfile
    import tempfile

    # Anything beyond this short list is per-machine session state and
    # would bloat the SCP without making the in-box claude any more useful.
    # 'hooks' is included so the SessionStart/Stop/UserPromptSubmit hooks
    # the local user has configured fire on the box too — assumes those
    # hooks use $HOME or ~ paths (not hardcoded /Users/<name>/...).
    ALLOWED_SUBPATHS = ["settings.json", "CLAUDE.md", "agents", "commands", "hooks"]

    fd, tarpath = tempfile.mkstemp(suffix=".tar.gz", prefix="rc-claude-cfg-")
    os.close(fd)
    with tarfile.open(tarpath, "w:gz") as tar:
        if claude_json.exists():
            tar.add(claude_json, arcname=".claude.json")
        if claude_dir.exists():
            for member in ALLOWED_SUBPATHS:
                p = claude_dir / member
                if p.exists():
                    tar.add(p, arcname=f".claude/{member}")
        # OAuth credentials (live in macOS Keychain on Macs, on-disk on Linux).
        # Without them the in-box claude shows 'Not logged in'.
        creds = _read_claude_credentials()
        if creds:
            creds_fd, creds_tmp = tempfile.mkstemp(
                suffix=".json", prefix="rc-claude-creds-"
            )
            try:
                with os.fdopen(creds_fd, "w") as fh:
                    fh.write(creds)
                # tar.add writes with whatever uid/perms the temp file has;
                # the Linux Claude Code expects 0600 for .credentials.json.
                os.chmod(creds_tmp, 0o600)
                tar.add(creds_tmp, arcname=".claude/.credentials.json")
            finally:
                try:
                    os.unlink(creds_tmp)
                except OSError:
                    pass
    return Path(tarpath)


def _copy_claude_config(
    public_ip: str,
    private_pem: str,
    claude_dir: Path | None = None,
    claude_json: Path | None = None,
) -> None:
    """Stage local claude auth + minimal config onto the box and restart its tmux.

    Default sources: ~/.claude/ and ~/.claude.json. Override via the kwargs
    or via --claude-config-from on the CLI. Restarts the in-box tmux 'claude'
    session afterward so it picks up the freshly copied auth.

    SECURITY: the config bundle includes OAuth/API tokens. They land on the
    EC2 instance and are readable by anyone who can SSH in or read the disk.
    Acceptable for personal dev hosts, NOT for shared/multi-tenant infra.
    """
    import subprocess
    import tempfile

    src_dir = Path(claude_dir) if claude_dir else Path.home() / ".claude"
    src_json = Path(claude_json) if claude_json else Path.home() / ".claude.json"

    if not src_json.exists() and not src_dir.exists():
        click.echo("  ! no local Claude config found at ~/.claude — skipping copy")
        return

    tarball = _build_claude_config_tarball(src_dir, src_json)

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)
    ssh_opts = [
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
    ]

    try:
        # Stage the tarball
        subprocess.run(
            ["scp"]
            + ssh_opts
            + [str(tarball), f"ec2-user@{public_ip}:/tmp/rc-claude-cfg.tar.gz"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Extract + chown — done via sudo so it works regardless of /home/ec2-user perm state
        subprocess.run(
            ["ssh"]
            + ssh_opts
            + [
                f"ec2-user@{public_ip}",
                "sudo tar -xzf /tmp/rc-claude-cfg.tar.gz -C /home/ec2-user/ && "
                "sudo chown -R ec2-user:ec2-user /home/ec2-user/.claude /home/ec2-user/.claude.json 2>/dev/null || true && "
                "sudo chmod 600 /home/ec2-user/.claude.json 2>/dev/null || true && "
                "rm -f /tmp/rc-claude-cfg.tar.gz",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Restart the in-box claude tmux so it picks up the new config (cloud-init
        # may have started a session earlier with no auth — that one is killed).
        # Best-effort: the start script may not exist yet if cloud-init hasn't
        # finished; the user will get a fresh session on first `rc dev attach`.
        subprocess.run(
            ["ssh"]
            + ssh_opts
            + [
                f"ec2-user@{public_ip}",
                "sudo -u ec2-user /usr/local/bin/rc-dev-start-claude.sh 2>/dev/null || true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        try:
            tarball.unlink()
        except OSError:
            pass


_SECRET_FIELDS = (
    "gh_token",
    "anthropic_key",
    "anthropic_api_key",
    "api_key",
    "secret",
    "password",
    "token",
)


def _sanitized_source_dict(source) -> dict:
    """Redact a serialized source dict for display. rc-bd7.

    Companion to _sanitized_source_repr, which takes a dataclass. This takes
    the plain dict form held on DevHostRecord.source (read from the state
    file), where an older rc may have persisted a real token.
    """
    if not isinstance(source, dict):
        return source
    out = {}
    for k, v in source.items():
        if any(s in k.lower() for s in _SECRET_FIELDS):
            out[k] = "<redacted>" if v else ""
        elif k == "extra_env" and isinstance(v, dict):
            out[k] = {
                kk: (
                    ("<redacted>" if vv else "")
                    if any(s in kk.lower() for s in _SECRET_FIELDS)
                    else vv
                )
                for kk, vv in v.items()
            }
        else:
            out[k] = v
    return out


def _sanitized_source_repr(source) -> str:
    """Render a source dataclass for stdout WITHOUT leaking secret-bearing
    fields (gh_token, ANTHROPIC_API_KEY in extra_env, etc.). rc-h40."""
    from dataclasses import asdict, is_dataclass

    cls = type(source).__name__
    if not is_dataclass(source):
        return f"{cls}({source!r})"
    d = asdict(source)
    safe = {}
    for k, v in d.items():
        if any(s in k.lower() for s in _SECRET_FIELDS):
            safe[k] = "<redacted>" if v else ""
        elif k == "extra_env" and isinstance(v, dict):
            safe[k] = {
                kk: (
                    "<redacted>" if any(s in kk.lower() for s in _SECRET_FIELDS) else vv
                )
                for kk, vv in v.items()
            }
        else:
            safe[k] = v
    return f"{cls}({safe})"


def _ports_from_compose(compose_paths) -> list[int]:
    """Extract host ports from compose `services.*.ports[*]` mappings.

    Follows top-level `include:` directives one level (relative to each
    compose file's own dir, matching Docker Compose semantics). Returns
    deduplicated, sorted host port list — used by `rc dev up` to default
    the SG --port allowlist when the user doesn't pass --port explicitly.
    """
    import yaml

    seen: set[int] = set()

    def _scan(path: Path):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return
        for inc in data.get("include") or []:
            inc_path = inc.get("path") if isinstance(inc, dict) else inc
            if inc_path:
                resolved = (path.parent / inc_path).resolve()
                if resolved.exists():
                    _scan(resolved)
        for _, svc in (data.get("services") or {}).items():
            if not isinstance(svc, dict):
                continue
            for p in svc.get("ports") or []:
                if isinstance(p, str):
                    # "host:container" or "host:container/proto" or "host"
                    host = p.split(":")[0].split("/")[0]
                    try:
                        seen.add(int(host))
                    except ValueError:
                        pass
                elif isinstance(p, dict):
                    pub = p.get("published") or p.get("target")
                    try:
                        seen.add(int(pub)) if pub else None
                    except (TypeError, ValueError):
                        pass
                elif isinstance(p, int):
                    seen.add(p)

    for cp in compose_paths:
        _scan(Path(cp).resolve())
    return sorted(seen)


def _scp_compose_file(public_ip: str, private_pem: str, compose_path: str) -> None:
    """SCP a docker-compose file to /home/ec2-user/<basename> on the box.

    The cloud-init bootstrap waits up to 5min for this file before running
    `docker compose up`. Only relevant for MultiGitSource flows.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)
    ssh_opts = [
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
    ]
    basename = Path(compose_path).name
    # Stage to /tmp first (always writable) then sudo cp + chown — same race
    # as env files: /home/ec2-user might still be root-owned at this point.
    subprocess.run(
        ["scp"] + ssh_opts + [compose_path, f"ec2-user@{public_ip}:/tmp/{basename}"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["ssh"]
        + ssh_opts
        + [
            f"ec2-user@{public_ip}",
            f"sudo cp /tmp/{basename} /home/ec2-user/{basename} && "
            f"sudo chown ec2-user:ec2-user /home/ec2-user/{basename}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _deliver_secret_env(public_ip: str, private_pem: str, secret_env: str) -> None:
    """Hand the box its credentials over SSH instead of baking them into user-data.

    rc-bd7. The payload is piped over stdin to a shell that writes it with a
    0600 umask — never passed as an argv element, which would expose it in
    `ps` output on both ends, and never written to a local temp file.

    Cloud-init's bootstrap blocks on /tmp/rc-dev-secrets before cloning, so
    this landing is what unblocks a private-repo clone.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)
    try:
        if not _wait_for_ssh_ready(public_ip, private_pem):
            raise click.ClickException("SSH never came up within the timeout")
        # `cat > file` under a 0600 umask: the secret travels on stdin, so it
        # appears in no process table and no shell history.
        proc = subprocess.run(
            ["ssh"]
            + _ssh_opts(keypath)
            + [
                f"ec2-user@{public_ip}",
                "umask 077 && cat > /tmp/rc-dev-secrets",
            ],
            input=secret_env.encode("utf-8") + b"\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise click.ClickException(
                f"writing secrets to the box failed: "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}"
            )
    finally:
        try:
            os.unlink(keypath)
        except OSError:
            pass


def _wait_for_ssh_and_copy_env(
    public_ip: str, private_pem: str, env_files: tuple, repo_name: str
) -> None:
    """Wait for SSH, then SCP each env file under /home/ec2-user/<repo_name>.

    Uses sudo mkdir+chown for the destination because /home/ec2-user may
    still be root-owned at the moment SCP runs (cloud-init bootstrap is
    likely still in flight and hasn't done its chown yet).
    """
    import subprocess
    import tempfile
    import time

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        kf.write(private_pem.encode("utf-8"))
        keypath = kf.name
    os.chmod(keypath, 0o600)

    ssh_opts = [
        "-i",
        keypath,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
    ]
    deadline = time.time() + 180
    while time.time() < deadline:
        if (
            subprocess.call(
                ["ssh"] + ssh_opts + [f"ec2-user@{public_ip}", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        ):
            break
        time.sleep(5)
    else:
        raise click.ClickException("SSH never came up within 3 minutes")

    # Stage to /tmp/rc-dev-envs first (always writable), then sudo-cp into the
    # repo dir. This avoids racing the cloud-init chown of /home/ec2-user.
    subprocess.run(
        ["ssh"] + ssh_opts + [f"ec2-user@{public_ip}", "mkdir -p /tmp/rc-dev-envs"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Stage every env file in /tmp/rc-dev-envs/ ONLY. The bootstrap script
    # picks them up AFTER cloning the repos and places them into the right
    # subpaths. This avoids the race where pre-creating /home/ec2-user/<repo>/
    # subdirs before bootstrap's git clone causes "destination already exists"
    # errors (rc-7v6 follow-up).
    for f in env_files:
        # Use absolute() instead of resolve() so symlinks in cwd (e.g.
        # `<workspace>/browser-mgr -> /elsewhere/browser-mgr`) don't escape
        # the workspace and force a basename-only fallback (rc-7v6 follow-up).
        abs_f = Path(f).absolute()
        cwd_abs = Path.cwd().absolute()
        try:
            rel = abs_f.relative_to(cwd_abs)
        except ValueError:
            rel = Path(abs_f.name)
        # __ encodes path separators so the flat filename round-trips back
        # to the original subpath in the bootstrap script's placement loop.
        flat = str(rel).replace("/", "__")
        subprocess.run(
            ["scp"]
            + ssh_opts
            + [str(abs_f), f"ec2-user@{public_ip}:/tmp/rc-dev-envs/{flat}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _aws_profile_from_rc_yml(ctx) -> str | None:
    """Best-effort load aws_profile from rc.yml."""
    import yaml

    config_path = (ctx.obj or {}).get("config_path") or "rc.yml"
    p = Path(config_path)
    if not p.exists():
        return None
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError:
        return None
    return cfg.get("aws_profile") or (cfg.get("aws") or {}).get("profile")


# ---------- legacy ECS dev push (kept for backwards compat) ----------


@dev_group.command(name="push")
@click.argument("service", required=False)
@click.option(
    "--watch",
    "watch",
    is_flag=True,
    help="Watch local sources and re-push on every change "
    "(debounced ~250ms). Requires fswatch (macOS) or "
    "inotifywait (Linux).",
)
@click.pass_context
def dev_push_cmd(ctx, service, watch):
    """[Legacy ECS] Push local dev_volume source(s) to a running ECS task via EFS.

    Superseded by `rc dev up` (EC2 dev-host) for new workflows. Kept
    in place for stacks that already use the ECS dev_volume path.
    """
    from remote_compose.dev_push import (
        DevPushError,
        push_all,
        watch_and_push,
    )

    config_path = ctx.obj.get("config_path") or "rc.yml"
    rc_path = Path(config_path)
    if not rc_path.exists():
        click.echo(f"Error: {rc_path} not found.", err=True)
        sys.exit(1)

    def _progress(msg: str) -> None:
        click.echo(msg)

    try:
        if watch:
            watch_and_push(rc_path, service, progress=_progress)
        else:
            results = push_all(rc_path, service, progress=_progress)
            total = sum(r["elapsed_s"] for r in results)
            click.echo(f"\n  pushed {len(results)} dev_volume(s) in {total:.1f}s.")
    except DevPushError as exc:
        click.echo(f"\n  rc dev push: {exc}", err=True)
        sys.exit(1)
