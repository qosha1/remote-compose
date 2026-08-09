"""DevHostService — orchestrates EC2 dev-host lifecycle for `rc dev`.

Owns the .rc/dev-hosts.yml state file and drives terraform + boto3 to
create/list/destroy per-agent EC2 instances. Each host is one EC2 with
docker pre-installed, the source materialized at /home/ec2-user/<name>,
and an ed25519 SSH key generated and stored via CredentialService.

The service is composable: tests inject mock terraform_runner, credential_service,
aws_client_factory; production wires the real ones via the constructor.
"""

from __future__ import annotations

import contextlib
import fcntl
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from ..exceptions import (
    DevHostAlreadyExistsError,
    DevHostNotFoundError,
)
from ..utils.ec2_instance_types import get_arch
from ..utils.ami_catalog import get_ami_id
from .bootstrap import SourceSpec, source_from_dict


# Minimal local stand-in for BaseService — avoids the from-django import
# that BaseService pulls in. DevHostService only needs a logger and the
# observer pattern (which it doesn't use yet); re-add BaseService inheritance
# if/when those features are needed and Django is guaranteed available.
class _StandaloneBase:
    def __init__(self):
        import logging

        self.logger = logging.getLogger(self.__class__.__name__)

    def log_info(self, msg, **_):
        self.logger.info(msg)

    def log_warning(self, msg, **_):
        self.logger.warning(msg)

    def log_error(self, msg, **_):
        self.logger.error(msg)


BaseService = _StandaloneBase


class FilesystemKeyStore:
    """Default keypair store for `rc dev` when Django isn't available.

    Stores ed25519 keypairs in `<root>/<name>.pem` (mode 0600) and
    `<root>/<name>.pub` (mode 0644). No encryption at rest — matches
    how terraform/ansible/ssh handle local keys. Production deploys can
    swap in CredentialService for Django-DB-backed Fernet encryption.
    """

    def __init__(self, root: Path | str = ".rc/dev-host-keys"):
        self.root = Path(root)

    def store_ssh_keypair(self, name: str, private_pem: str, public_openssh: str, **_):
        self.root.mkdir(parents=True, exist_ok=True)
        priv_path = self.root / f"{name}.pem"
        pub_path = self.root / f"{name}.pub"
        # destroy_host doesn't remove local key material, so re-provisioning a
        # same-named box after a previous one was destroyed hits whatever this
        # file's mode was left at. write_text() needs write permission on an
        # EXISTING file to truncate it — a 0400 leftover (however it got that
        # way: a prior chmod, restrictive umask, etc.) then crashes with a raw
        # PermissionError instead of just being overwritten. Ensure writability
        # first so storing a keypair for `name` always succeeds.
        if priv_path.exists():
            priv_path.chmod(0o600)
        priv_path.write_text(private_pem)
        priv_path.chmod(0o600)
        if pub_path.exists():
            pub_path.chmod(0o600)
        pub_path.write_text(public_openssh)
        pub_path.chmod(0o644)

        # SimpleNamespace gives a generic credential-like object with .id and .name.
        from types import SimpleNamespace

        return SimpleNamespace(id=str(priv_path), name=name)

    def get_credential(self, credential_id):
        """Look up by either path or name; returns a credential-like object."""
        from types import SimpleNamespace

        path = (
            Path(credential_id)
            if str(credential_id).endswith(".pem")
            else self.root / f"{credential_id}.pem"
        )
        return SimpleNamespace(id=str(path))

    def get_ssh_keypair(self, credential) -> tuple[str, str]:
        priv_path = Path(credential.id)
        pub_path = priv_path.with_suffix(".pub")
        return priv_path.read_text(), pub_path.read_text() if pub_path.exists() else ""


@dataclass
class DevHostRecord:
    """In-memory and on-disk representation of a single dev host."""

    name: str
    source: dict
    instance_type: str
    region: str
    ami: Optional[str] = None
    instance_id: Optional[str] = None
    public_ip: Optional[str] = None
    public_dns: Optional[str] = None
    ssh_key_credential_id: Optional[int] = None
    created_at: Optional[str] = None
    status: str = "pending"
    # False default here (not True, unlike create_host's default) is
    # deliberately the historically-accurate fallback for records persisted
    # before this field existed — every one of them was on-demand.
    spot: bool = False
    # rc-n14: reconciliation marker for `rc dev list` live view.
    #   True  = present in the local .rc/dev-hosts.yml state
    #   False = found in AWS by tag but NOT in local state (untracked leak)
    #   None  = not reconciled against AWS (plain state listing)
    tracked: Optional[bool] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- ssh keypair generation ----------


def _generate_ssh_keypair() -> tuple[str, str]:
    """Generate an ed25519 keypair and return (private_pem, public_openssh).

    Kept module-private and small so DevHostService can monkey-patch it in
    tests if needed without touching the cryptography import.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_openssh = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )
    return private_pem, public_openssh


# ---------- service ----------


class DevHostService(BaseService):
    """Lifecycle orchestrator for `rc dev` EC2 dev-hosts."""

    def __init__(
        self,
        credential_service=None,
        terraform_runner=None,
        aws_client_factory=None,
        state_path: Path | str | None = None,
        keypair_factory: Callable[[], tuple[str, str]] = _generate_ssh_keypair,
        ami_lookup: Callable[[str, str], str] = get_ami_id,
    ):
        super().__init__()
        self.credential_service = credential_service
        self.terraform_runner = terraform_runner
        self.aws_client_factory = aws_client_factory
        self.state_path = Path(state_path) if state_path else Path(".rc/dev-hosts.yml")
        self._keypair_factory = keypair_factory
        self._ami_lookup = ami_lookup

    # ---------- state file I/O ----------

    def _load_state(self) -> dict[str, dict]:
        if not self.state_path.exists():
            return {}
        loaded = yaml.safe_load(self.state_path.read_text()) or {}
        return self._scrub_legacy_secrets(loaded.get("hosts", {}) or {})

    _legacy_secret_warned = False

    def _scrub_legacy_secrets(self, hosts: dict[str, dict]) -> dict[str, dict]:
        """Redact credentials from a state file written before rc-bd7.

        Closing the leak going forward does nothing about the tokens already
        sitting in an existing .rc/dev-hosts.yml. Those are live until someone
        rotates them, and until then every ``rc dev status`` is one keystroke
        from printing one to a terminal (and a scrollback, and a screen share).

        So: redact in memory, which means nothing re-persists or prints the
        value and the next state write scrubs the file. And say so once —
        rotation is the only thing that actually fixes an already-leaked
        credential, and rc cannot do that for the user.
        """
        exposed: list[str] = []
        cleaned: dict[str, dict] = {}
        for name, record in hosts.items():
            if not isinstance(record, dict):
                cleaned[name] = record
                continue
            src = record.get("source")
            if isinstance(src, dict) and _has_secret_value(src):
                exposed.append(name)
                record = {**record, "source": _redact_secrets(src)}
            cleaned[name] = record

        if exposed and not DevHostService._legacy_secret_warned:
            DevHostService._legacy_secret_warned = True
            sys.stderr.write(
                f"\nSECURITY: {self.state_path} contains plaintext credentials "
                f"for dev host(s) {sorted(exposed)}.\n"
                f"  They were written before rc stopped persisting secrets "
                f"(rc-bd7). rc has redacted them in memory and will not write "
                f"them back, but the values on disk are still live until you "
                f"ROTATE them:\n"
                f"    https://github.com/settings/tokens\n"
                f"  The same token is also in this host's EC2 user-data and in "
                f"any .rc/terraform-state/*.tfstate from when it was created.\n\n"
            )
        return cleaned

    def _save_state(self, hosts: dict[str, dict]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(yaml.safe_dump({"hosts": hosts}, sort_keys=True))

    @contextlib.contextmanager
    def _locked_state(self):
        """Read-modify-write the state file under an exclusive file lock.

        create_host used to load `hosts` once at the top of the method,
        then save that same (by then minutes-stale, post-terraform-apply)
        snapshot at the end — every save overwrote the WHOLE file with
        whatever was in memory, however stale. Two `rc dev up`s running
        concurrently (e.g. provisioning several boxes in parallel) would
        each finish with a snapshot missing the other's entry, and whichever
        saved last silently dropped it. Hit for real: spinning up 3 boxes at
        once lost 2 of them from local state — they were fine in AWS the
        whole time (`rc dev list` still finds them by tag), but `rc dev
        attach`/`ssh`/`stop`/`start`/`destroy` all key off this file via
        get_host(), so an untracked box can't be managed by name until
        something re-adds it.

        flock is per-process-group and released automatically on close, so
        this is safe even if the process dies mid-critical-section (no
        stale lock left behind, unlike a lockfile-existence convention).
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.touch(exist_ok=True)
        with open(self.state_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                loaded = yaml.safe_load(f.read()) or {}
                hosts = self._scrub_legacy_secrets(loaded.get("hosts", {}) or {})
                yield hosts
                f.seek(0)
                f.truncate()
                f.write(yaml.safe_dump({"hosts": hosts}, sort_keys=True))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _write_tfvars_if_possible(self, variables: dict) -> None:
        """Write terraform.tfvars.json to runner.working_dir if it's a real path.

        Mocks have a MagicMock for working_dir whose str() repr starts with
        'MagicMock/' — writing that creates a stray directory in the project
        root. Skip anything that doesn't look like a real Path or string.
        """
        import json

        working_dir = getattr(self.terraform_runner, "working_dir", None)
        if working_dir is None or not isinstance(working_dir, (str, Path)):
            return
        try:
            working_dir = Path(working_dir)
            working_dir.mkdir(parents=True, exist_ok=True)
            (working_dir / "terraform.tfvars.json").write_text(
                json.dumps(variables, indent=2)
            )
        except (TypeError, OSError):
            return

    def _read_outputs_if_possible(self) -> dict:
        """Read TF outputs from a real runner; return {} for mocks."""
        try:
            raw = self.terraform_runner.output()
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        # terraform output -json wraps each value in {"value": ..., "type": ...}
        return {k: v.get("value") if isinstance(v, dict) else v for k, v in raw.items()}

    def _record_from_state_dict(self, name: str, d: dict) -> DevHostRecord:
        return DevHostRecord(
            name=name,
            source=d.get("source", {}),
            instance_type=d.get("instance_type", ""),
            region=d.get("region", ""),
            ami=d.get("ami"),
            instance_id=d.get("instance_id"),
            public_ip=d.get("public_ip"),
            public_dns=d.get("public_dns"),
            ssh_key_credential_id=d.get("ssh_key_credential_id"),
            created_at=d.get("created_at"),
            status=d.get("status", "unknown"),
            spot=d.get("spot", False),
        )

    # ---------- public API ----------

    def create_host(
        self,
        name: str,
        source: SourceSpec,
        # t4g.2xlarge (8 vCPU): provisioning is dominated by CPU-bound docker
        # image builds, so cores buy wall-clock — 20m on t4g.large vs 10m43s on
        # this, same multi-repo stack. t4g.medium is NOT a safe default: it OOMs
        # during the builds.
        instance_type: str = "t4g.2xlarge",
        region: Optional[str] = None,
        # 30GiB (the old default) leaves a multi-repo box (sentinal +
        # react-web-app + browser-mgr, 3 docker compose projects) at 94% full
        # right after first boot — ~9GB images + ~7GB build cache + containers
        # + volumes on top of the OS and repo checkouts. Heavy rebuild days
        # (many agent stacks in flight) fill the remaining headroom fast.
        ebs_size_gb: int = 100,
        # ~50-65% cheaper than on-demand for the t4g family (confirmed via
        # the Pricing API). persistent + interruption_behavior=stop (set in
        # the terraform module) keeps `rc dev stop`/`start` working the same
        # way it does on-demand — a reclaimed Spot instance stops rather than
        # terminates. The real tradeoff is start-time capacity, not data
        # loss: `start` on a stopped Spot instance needs AWS to have spare
        # capacity at or below the current price, which on-demand doesn't.
        spot: bool = True,
    ) -> DevHostRecord:
        # validate inputs eagerly
        arch = get_arch(instance_type)  # raises ValidationError on unknown
        if not region:
            raise DevHostNotFoundError(
                "region must be provided (no rc.yml fallback in v1)"
            )
        ami = self._ami_lookup(region, arch)

        hosts = self._load_state()
        if name in hosts:
            raise DevHostAlreadyExistsError(f"dev host {name!r} already exists")

        # generate ssh keypair, store via credential service
        private_pem, public_openssh = self._keypair_factory()
        credential = self.credential_service.store_ssh_keypair(
            name=f"dev-host-{name}-key",
            private_pem=private_pem,
            public_openssh=public_openssh,
        )

        # render cloud-init for the source
        user_data = (
            source.render_user_data() if hasattr(source, "render_user_data") else ""
        )
        # EC2 caps user-data at 16 KiB, and the multi-git template had grown to
        # 16,442 bytes — provisioning failed outright, terraform rolled back, and
        # the "error" was the truncated blob itself, which reads like nothing in
        # particular. Every comment added to cloud-init pushed toward that cliff.
        #
        # cloud-init detects and inflates gzip-compressed user-data, so compress
        # it: the same payload lands around 4 KiB, leaving real headroom instead
        # of a tripwire nobody sees until a box refuses to build.
        user_data_b64 = _compress_user_data(user_data)

        tags = {
            "DevHost": name,
            "ManagedBy": "rc-dev",
        }
        variables = {
            "name": name,
            "instance_type": instance_type,
            "ami_id": ami,
            "ssh_public_key": public_openssh,
            "user_data_base64": user_data_b64,
            "ebs_size_gb": ebs_size_gb,
            "spot": spot,
            "tags": tags,
            "region": region,
        }
        # Real TerraformRunner.apply() takes only (plan_file, auto_approve) —
        # tfvars must be written to the working dir before apply. Mock runners
        # in unit/integration tests accept extra kwargs and may return outputs
        # directly; in those cases we use what the mock returns and skip the
        # tfvars write (no working_dir to write into).
        self._write_tfvars_if_possible(variables)
        # apply() may partially succeed — e.g. EC2 instance created but EIP
        # allocation fails (quota exceeded). Without rollback the AWS resources
        # leak indefinitely (rc-xiq: 13 boxes + 7 EIPs leaked over 24h before
        # we noticed). On any apply failure: attempt terraform destroy from the
        # same working dir (which still has the partial tfstate), then re-raise.
        try:
            try:
                apply_result = self.terraform_runner.apply(
                    variables=variables,
                    tags=tags,
                    user_data=user_data,
                    ssh_public_key=public_openssh,
                )
            except TypeError:
                # real runner — apply() rejects kwargs; call with no args
                apply_result = self.terraform_runner.apply()
        except Exception as apply_exc:
            rollback_note = ""
            try:
                self.terraform_runner.destroy()
                rollback_note = " (rolled back via terraform destroy)"
            except Exception as destroy_exc:
                # Couldn't roll back — write a 'failed' state entry so the user
                # can retry `rc dev destroy <name> --force` later. Better a
                # tracked orphan than an invisible one.
                try:
                    hosts = self._load_state()
                    hosts[name] = {
                        "name": name,
                        "source": _source_to_dict(source),
                        "instance_type": instance_type,
                        "region": region,
                        "ami": ami,
                        "instance_id": None,
                        "public_ip": None,
                        "public_dns": None,
                        "ssh_key_credential_id": getattr(credential, "id", None),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "status": "failed",
                        "failure_reason": str(apply_exc)[:500],
                    }
                    self._save_state(hosts)
                    rollback_note = (
                        f" (rollback ALSO failed: {destroy_exc}; "
                        f"registry entry marked status=failed for retry)"
                    )
                except Exception:
                    rollback_note = (
                        f" (rollback failed: {destroy_exc}; "
                        f"registry write also failed — manual AWS cleanup required)"
                    )
            # Re-raise the original apply error. We don't wrap it because some
            # exception types (e.g. TerraformError) have non-trivial __init__
            # signatures — wrapping changes the exception class and breaks
            # except-clauses upstream. Instead attach rollback context as a
            # PEP 678 note (Python 3.11+; harmless on older versions).
            if rollback_note and hasattr(apply_exc, "add_note"):
                apply_exc.add_note(rollback_note.strip())
            raise

        tf_outputs = apply_result or self._read_outputs_if_possible()

        record = DevHostRecord(
            name=name,
            source=_source_to_dict(source),
            instance_type=instance_type,
            region=region,
            ami=ami,
            instance_id=tf_outputs.get("instance_id"),
            public_ip=tf_outputs.get("public_ip"),
            public_dns=tf_outputs.get("public_dns"),
            ssh_key_credential_id=getattr(credential, "id", None),
            created_at=datetime.now(timezone.utc).isoformat(),
            status="running" if tf_outputs.get("instance_id") else "pending",
            spot=spot,
        )

        with self._locked_state() as locked_hosts:
            locked_hosts[name] = record.to_dict()
        return record

    def list_hosts(self) -> list[DevHostRecord]:
        hosts = self._load_state()
        return [self._record_from_state_dict(n, d) for n, d in hosts.items()]

    def reconcile_live(self, regions: list[str]) -> list[DevHostRecord]:
        """List dev hosts from LIVE AWS (tag ManagedBy=rc-dev) across regions,
        merged with the local state file (rc-n14).

        AWS is the source of truth for existence + status, so this surfaces
        boxes that are billing regardless of the cwd-relative state file:
          - tracked=True  : present in both AWS and local state
          - tracked=False : in AWS but NOT local state (untracked leak)
          - status="gone" : in local state but absent from AWS (stale entry)

        A region whose describe_instances call fails is skipped (best-effort);
        the stale-state pass still runs so nothing silently disappears.
        """
        state = self._load_state()
        seen: set[str] = set()
        records: list[DevHostRecord] = []
        live_states = [
            "pending",
            "running",
            "stopping",
            "stopped",
            "shutting-down",
        ]
        for region in regions:
            ec2 = self.aws_client_factory.get_client("ec2", region_name=region)
            try:
                resp = ec2.describe_instances(
                    Filters=[
                        {"Name": "tag:ManagedBy", "Values": ["rc-dev"]},
                        {"Name": "instance-state-name", "Values": live_states},
                    ]
                )
            except Exception:
                continue
            for resv in resp.get("Reservations", []):
                for inst in resv.get("Instances", []):
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", []) or []}
                    name = tags.get("DevHost") or inst.get("InstanceId", "")
                    seen.add(name)
                    sd = state.get(name, {})
                    records.append(
                        DevHostRecord(
                            name=name,
                            source=sd.get("source", {}),
                            instance_type=inst.get(
                                "InstanceType", sd.get("instance_type", "")
                            ),
                            region=region,
                            ami=inst.get("ImageId", sd.get("ami")),
                            instance_id=inst.get("InstanceId"),
                            public_ip=(
                                inst.get("PublicIpAddress") or sd.get("public_ip")
                            ),
                            public_dns=(
                                inst.get("PublicDnsName") or sd.get("public_dns")
                            ),
                            ssh_key_credential_id=sd.get("ssh_key_credential_id"),
                            created_at=sd.get("created_at"),
                            status=inst.get("State", {}).get("Name", "unknown"),
                            # AWS reports this directly (InstanceLifecycle is
                            # "spot" or absent) — prefer it over local state
                            # for the same reason instance_type/ami/etc. do:
                            # it's live truth, local state can be stale.
                            spot=(
                                inst.get("InstanceLifecycle") == "spot"
                                or sd.get("spot", False)
                            ),
                            tracked=name in state,
                        )
                    )
        # State entries AWS never returned: stale / destroyed out-of-band.
        for name, sd in state.items():
            if name in seen:
                continue
            rec = self._record_from_state_dict(name, sd)
            rec.status = "gone"
            rec.tracked = True
            records.append(rec)
        return records

    def get_host(self, name: str) -> DevHostRecord:
        hosts = self._load_state()
        if name not in hosts:
            raise DevHostNotFoundError(f"dev host {name!r} not found in state")
        return self._record_from_state_dict(name, hosts[name])

    def stop_host(self, name: str) -> None:
        record = self.get_host(name)
        ec2 = self.aws_client_factory.get_client("ec2", region_name=record.region)
        if record.instance_id:
            ec2.stop_instances(InstanceIds=[record.instance_id])
        with self._locked_state() as hosts:
            if name in hosts:
                hosts[name]["status"] = "stopped"

    def start_host(self, name: str) -> None:
        record = self.get_host(name)
        ec2 = self.aws_client_factory.get_client("ec2", region_name=record.region)
        if record.instance_id:
            self._start_instance_with_spot_retry(ec2, record.instance_id)
        with self._locked_state() as hosts:
            if name in hosts:
                hosts[name]["status"] = "running"

    def _start_instance_with_spot_retry(
        self, ec2, instance_id: str, attempts: int = 15, delay_seconds: float = 15.0
    ) -> None:
        """start_instances on a just-stopped persistent Spot Instance can
        fail with IncorrectSpotRequestState while AWS settles the spot
        request's internal state after the stop. Transient, not a real
        block — but the settling window is neither short nor consistent:
        measured live across two back-to-back stop/start cycles on the same
        instance, one retry succeeded in well under a minute, the other was
        still failing after 5 attempts x 8s (40s) and only succeeded when
        checked again some time after that. 15 attempts x 15s (~3.75min
        worst case) is sized off that second, slower measurement rather than
        the faster one — surfacing a raw ClientError traceback for something
        that resolves itself if you just wait is worse than a slow success.
        """
        import time

        from botocore.exceptions import ClientError

        for attempt in range(1, attempts + 1):
            try:
                ec2.start_instances(InstanceIds=[instance_id])
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "IncorrectSpotRequestState" or attempt == attempts:
                    raise
                time.sleep(delay_seconds)

    def destroy_host(self, name: str, force: bool = False) -> None:
        """Tear the host down. With force=True, proceed even if it is unknown.

        --force is documented as "tear down even if not in state", but this used
        to `return` on that exact path — doing NOTHING while the CLI printed
        "✓ destroyed". Since dev-host state is per-directory, running destroy
        from anywhere but the dir that ran `up` hit that branch every time. The
        instance often got cleaned up by other means later; the EIP, a separate
        terraform resource, was simply abandoned and billed indefinitely. That
        is where the stray rc-dev-<name>-eip addresses come from.

        Now force actually attempts the terraform destroy. With no terraform
        state present it is a harmless no-op, which is the same idempotence the
        old early-return was reaching for — minus the silent abandonment.
        """
        hosts = self._load_state()
        if name not in hosts and not force:
            raise DevHostNotFoundError(f"dev host {name!r} not found in state")

        # The persisted terraform.tfvars.json reflects whatever variable
        # schema was current when the box was CREATED. If the module's
        # variables have changed since (e.g. user_data -> user_data_base64,
        # added in the user-data-gzip fix), destroy fails outright with "No
        # value for required variable" on any box old enough to predate the
        # change — even though nothing about tearing it down actually
        # depends on that variable's value. Regenerate tfvars from the
        # stored record first so destroy keeps working across schema drift
        # instead of leaving old boxes permanently stuck (and billing).
        # Best-effort: any failure here just leaves the existing tfvars.json
        # in place, i.e. today's behavior — never a new way to fail.
        if name in hosts:
            self._refresh_tfvars_for_destroy(name, hosts[name])

        self.terraform_runner.destroy()
        # Reload under lock immediately before removing — terraform destroy
        # can take ~1min, and the `hosts` loaded at the top of this method
        # would otherwise go stale over that window the same way
        # create_host's did (see _locked_state).
        with self._locked_state() as locked_hosts:
            locked_hosts.pop(name, None)

    def _refresh_tfvars_for_destroy(self, name: str, stored: dict) -> None:
        try:
            source = source_from_dict(stored.get("source") or {})
            user_data = (
                source.render_user_data() if hasattr(source, "render_user_data") else ""
            )
            ssh_public_key = ""
            cred_id = stored.get("ssh_key_credential_id")
            if cred_id:
                cred = self.credential_service.get_credential(cred_id)
                _, ssh_public_key = self.credential_service.get_ssh_keypair(cred)
            variables = {
                "name": name,
                "instance_type": stored.get("instance_type") or "t4g.large",
                "ami_id": stored.get("ami") or "",
                "ssh_public_key": ssh_public_key,
                "user_data_base64": _compress_user_data(user_data),
                # Not persisted anywhere on the record — irrelevant for a
                # pure destroy (it deletes whatever's in tfstate regardless
                # of variable-driven attribute values; only presence/type
                # matters for terraform to evaluate the plan).
                "ebs_size_gb": 100,
                "spot": stored.get("spot", False),
                "tags": {"DevHost": name, "ManagedBy": "rc-dev"},
                "region": stored.get("region") or "",
            }
            self._write_tfvars_if_possible(variables)
        except Exception:
            pass

    def get_ssh_command(self, name: str) -> str:
        """Return a shell-ready ssh invocation string for `rc dev ssh`."""
        record = self.get_host(name)
        host = record.public_ip or record.public_dns
        if not host:
            raise DevHostNotFoundError(
                f"dev host {name!r} has no public address yet (instance may still be booting)"
            )
        # the actual key file is materialized by the CLI via
        # CredentialService.get_ssh_key_file context manager — this method
        # returns the command shape, not a runnable command.
        return f"ssh -o StrictHostKeyChecking=no -i <key> ec2-user@{host}"


def _source_to_dict(source: SourceSpec) -> dict:
    """Serialize a SourceSpec dataclass to a state-file dict, minus secrets.

    rc-bd7: .rc/dev-hosts.yml is a plaintext file in the user's working tree,
    and this used to write ``gh_token`` into it verbatim — four live gho_
    tokens were found sitting in one. Nothing that reads this record back
    needs the real value: the only consumer is
    ``_refresh_tfvars_for_destroy``, which re-renders user-data purely so
    terraform has a variable of the right shape to tear the box down, and a
    destroy does not authenticate to GitHub.

    Redaction happens here rather than at the call sites so every future
    persist path inherits it — a new caller cannot forget.
    """
    from dataclasses import asdict as _asdict, is_dataclass

    if hasattr(source, "to_dict"):
        raw = source.to_dict()
    elif is_dataclass(source):
        raw = _asdict(source)
    elif isinstance(source, dict):
        raw = dict(source)
    else:
        raise TypeError(f"unsupported source type: {type(source)!r}")
    return _redact_secrets(raw)


def _has_secret_value(raw: dict) -> bool:
    """True when a serialized source still carries a real credential value."""
    from .bootstrap import is_secret_env_key

    for k, v in raw.items():
        if is_secret_env_key(k) and v:
            return True
        if k == "extra_env" and isinstance(v, dict):
            if any(is_secret_env_key(kk) and vv for kk, vv in v.items()):
                return True
    return False


def _redact_secrets(raw: dict) -> dict:
    """Strip credential-bearing values out of a serialized source."""
    from .bootstrap import is_secret_env_key

    out: dict = {}
    for k, v in raw.items():
        if is_secret_env_key(k):
            # Drop the value, keep nothing — an empty string round-trips
            # through source_from_dict() into a source that renders a
            # token-less (and therefore secret-free) user-data blob.
            out[k] = ""
        elif k == "extra_env" and isinstance(v, dict):
            out[k] = {kk: ("" if is_secret_env_key(kk) else vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def _compress_user_data(user_data: str) -> str:
    """gzip+base64 a cloud-config blob so it fits EC2's 16 KiB user-data cap.

    cloud-init sniffs the gzip magic bytes and inflates the payload itself, so
    this is transparent on the box. mtime is pinned to 0 so the same input
    always produces identical bytes — otherwise every terraform plan would show
    a spurious user-data diff and replace the instance.
    """
    import base64
    import gzip
    import io

    if not user_data:
        return ""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(user_data.encode("utf-8"))
    return base64.b64encode(buf.getvalue()).decode("ascii")
