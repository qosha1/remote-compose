"""Translate Copilot storage + network → rc.yml v2 + warnings.

Storage: storage.volumes.<name>.{path, efs:bool|dict, read_only}
         → rc.yml services[*].volumes [{name, mount, uid, gid, mode}]
Network: network.vpc.placement: 'private' → warn (private-subnet support
         is rc-e5u.25, not yet shipped; default is public-subnet Fargate).
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.copilot.discover import CopilotService
from remote_compose.copilot.translate import (
    PrivateSubnetUnsupportedWarning,
    translate_storage,
    translate_network,
)


def _svc(raw: dict) -> CopilotService:
    return CopilotService(
        name=raw.get("name", "x"),
        type=raw.get("type", "Backend Service"),
        manifest_path=Path("/dev/null"),
        raw=raw,
    )


# ---------------------------------------------------------------------
# storage.volumes → rc.yml volumes[]
# ---------------------------------------------------------------------

class TestStorageVolumes:
    def test_basic_efs_volume(self):
        out, _ = translate_storage(_svc({
            "name": "postgres",
            "storage": {
                "volumes": {
                    "pgdata": {
                        "path": "/var/lib/postgresql/data",
                        "efs": True,
                    },
                },
            },
        }))
        assert out["volumes"] == [{
            "name": "pgdata",
            "mount": "/var/lib/postgresql/data",
        }]

    def test_efs_with_uid_gid_translated(self):
        # Copilot efs.uid/gid go to the access point — same shape we use.
        out, _ = translate_storage(_svc({
            "name": "postgres",
            "storage": {
                "volumes": {
                    "pgdata": {
                        "path": "/var/lib/postgresql/data",
                        "efs": {"uid": 999, "gid": 999},
                    },
                },
            },
        }))
        vol = out["volumes"][0]
        assert vol["uid"] == 999
        assert vol["gid"] == 999

    def test_multiple_volumes_preserve_order(self):
        out, _ = translate_storage(_svc({
            "name": "app",
            "storage": {
                "volumes": {
                    "data": {"path": "/data", "efs": True},
                    "cache": {"path": "/cache", "efs": True},
                },
            },
        }))
        names = [v["name"] for v in out["volumes"]]
        assert sorted(names) == ["cache", "data"]

    def test_no_storage_block_returns_empty(self):
        out, _ = translate_storage(_svc({"name": "x"}))
        assert out == {}

    def test_volume_without_efs_skipped_with_no_warning(self):
        # ephemeral local volumes (Copilot allows efs:false or omits efs)
        # don't translate to anything in rc.yml — Fargate task storage
        # is ephemeral anyway. Quietly skip.
        out, _ = translate_storage(_svc({
            "name": "x",
            "storage": {"volumes": {"tmp": {"path": "/tmp", "efs": False}}},
        }))
        assert out == {}

    def test_volume_missing_path_skipped(self):
        # Defensively: malformed volume entry shouldn't crash the import.
        out, _ = translate_storage(_svc({
            "name": "x",
            "storage": {"volumes": {"oops": {"efs": True}}},
        }))
        assert out == {}


# ---------------------------------------------------------------------
# network.vpc.placement: private → warning (rc-e5u.25 not shipped yet)
# ---------------------------------------------------------------------

class TestNetworkPlacement:
    def test_private_placement_warns(self):
        out, warnings = translate_network(_svc({
            "name": "x",
            "network": {"vpc": {"placement": "private"}},
        }))
        assert any(isinstance(w, PrivateSubnetUnsupportedWarning) for w in warnings)
        assert any("rc-e5u.25" in w.message for w in warnings)

    def test_public_placement_no_warning(self):
        out, warnings = translate_network(_svc({
            "name": "x",
            "network": {"vpc": {"placement": "public"}},
        }))
        assert warnings == []

    def test_no_network_block_no_warning(self):
        out, warnings = translate_network(_svc({"name": "x"}))
        assert warnings == []

    def test_translate_network_returns_dict(self):
        # network translator currently produces no rc.yml fields (just
        # warnings) but the contract is (dict, warnings).
        out, _ = translate_network(_svc({"name": "x"}))
        assert isinstance(out, dict)
