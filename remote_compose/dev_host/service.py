"""DevHostService — orchestrates EC2 dev-host lifecycle for `rc dev`.

Owns the .rc/dev-hosts.yml state file and drives terraform + boto3 to
create/list/destroy per-agent EC2 instances. Each host is one EC2 with
docker pre-installed, the source materialized at /home/ec2-user/<name>,
and an ed25519 SSH key generated and stored via CredentialService.

The service is composable: tests inject mock terraform_runner, credential_service,
aws_client_factory; production wires the real ones via the constructor.
"""

from __future__ import annotations

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
from .bootstrap import SourceSpec


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
        priv_path.write_text(private_pem)
        priv_path.chmod(0o600)
        pub_path.write_text(public_openssh)

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
        return loaded.get("hosts", {}) or {}

    def _save_state(self, hosts: dict[str, dict]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            yaml.safe_dump({"hosts": _scrub_secrets(hosts)}, sort_keys=True)
        )

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
        ebs_size_gb: int = 30,
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
        )

        hosts[name] = record.to_dict()
        self._save_state(hosts)
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
        hosts = self._load_state()
        hosts[name]["status"] = "stopped"
        self._save_state(hosts)

    def start_host(self, name: str) -> None:
        record = self.get_host(name)
        ec2 = self.aws_client_factory.get_client("ec2", region_name=record.region)
        if record.instance_id:
            ec2.start_instances(InstanceIds=[record.instance_id])
        hosts = self._load_state()
        hosts[name]["status"] = "running"
        self._save_state(hosts)

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

        self.terraform_runner.destroy()
        if name in hosts:
            del hosts[name]
            self._save_state(hosts)

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


def _scrub_secrets(hosts: dict[str, dict]) -> dict[str, dict]:
    """Drop credential-bearing source fields from what goes on disk.

    .rc/dev-hosts.yml is an ordinary un-encrypted YAML file in the operator's
    working directory, and it was storing live `gho_` PATs verbatim under
    source.gh_token — four of them, one per box ever provisioned, still there
    long after the boxes were destroyed.

    Applied on save rather than only at write time so this also *cleans*: every
    state-mutating command (up / stop / start / destroy) rewrites the whole
    file, so the first one to run after this change strips whatever the old
    code left behind. Reads stay tolerant — source_from_dict still accepts the
    field, so a state file written by an older rc loads without complaint.
    """
    from .bootstrap import SECRET_SOURCE_FIELDS

    cleaned = {}
    for name, host in hosts.items():
        if not isinstance(host, dict):
            cleaned[name] = host
            continue
        source = host.get("source")
        if isinstance(source, dict) and any(f in source for f in SECRET_SOURCE_FIELDS):
            host = dict(host)
            host["source"] = {
                k: v for k, v in source.items() if k not in SECRET_SOURCE_FIELDS
            }
        cleaned[name] = host
    return cleaned


def _source_to_dict(source: SourceSpec) -> dict:
    """Serialize a SourceSpec dataclass to a state-file dict, minus secrets."""
    from .bootstrap import SECRET_SOURCE_FIELDS

    if hasattr(source, "to_dict"):
        d = source.to_dict()
    else:
        # dataclass fallback
        from dataclasses import asdict as _asdict, is_dataclass

        if is_dataclass(source):
            d = _asdict(source)
        elif isinstance(source, dict):
            d = source
        else:
            raise TypeError(f"unsupported source type: {type(source)!r}")
    return {k: v for k, v in d.items() if k not in SECRET_SOURCE_FIELDS}


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
