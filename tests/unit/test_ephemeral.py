"""Unit tests for the ephemeral registry + duration parser.

Covers the contract that ``rc deploy --ttl`` and ``rc reap`` rely on:

  * ``parse_duration`` accepts the documented unit combinations and rejects
    obvious garbage.
  * ``register_stack`` is idempotent on (project, region) and preserves
    ``created_at`` across reruns (so age accounting survives multiple
    ``rc deploy --ttl`` invocations).
  * ``find_expired`` correctly partitions records by their ISO timestamp.
  * ``to_iso_utc`` / ``from_iso_utc`` round-trip and produce the trailing
    'Z' form that ends up in terraform tags.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from remote_compose import ephemeral

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5m", timedelta(minutes=5)),
        ("30m", timedelta(minutes=30)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        ("4h30m", timedelta(hours=4, minutes=30)),
        ("90s", timedelta(seconds=90)),
        ("1d12h", timedelta(days=1, hours=12)),
        ("2h15m30s", timedelta(hours=2, minutes=15, seconds=30)),
        ("0s", timedelta(0)),
        ("  5m  ", timedelta(minutes=5)),
        ("2H", timedelta(hours=2)),  # case-insensitive
    ],
)
def test_parse_duration_accepts_documented_forms(text, expected):
    assert ephemeral.parse_duration(text) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "5", "5x", "abc", "5m3", "-5m", "1.5h", "5 minutes", None],
)
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        ephemeral.parse_duration(bad)


# ---------------------------------------------------------------------------
# ISO timestamp serialization
# ---------------------------------------------------------------------------


def test_iso_utc_roundtrip_strips_microseconds_and_uses_z():
    dt = datetime(2026, 4, 25, 18, 30, 45, 123456, tzinfo=timezone.utc)
    iso = ephemeral.to_iso_utc(dt)
    assert iso == "2026-04-25T18:30:45Z"
    parsed = ephemeral.from_iso_utc(iso)
    assert parsed == dt.replace(microsecond=0)


def test_iso_utc_normalizes_naive_datetime_as_utc():
    naive = datetime(2026, 4, 25, 12, 0, 0)
    iso = ephemeral.to_iso_utc(naive)
    assert iso.endswith("Z")
    assert ephemeral.from_iso_utc(iso) == naive.replace(tzinfo=timezone.utc)


def test_iso_utc_converts_aware_non_utc_to_utc():
    # +05:00 -> back to UTC
    aware = datetime(2026, 4, 25, 18, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    iso = ephemeral.to_iso_utc(aware)
    assert iso == "2026-04-25T13:00:00Z"


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    return tmp_path / "ephemeral.json"


def test_list_records_returns_empty_when_file_missing(registry):
    assert ephemeral.list_records(path=registry) == []


def test_register_stack_creates_file_and_record(registry):
    rec = ephemeral.register_stack(
        project="myapp",
        region="us-east-1",
        expires_at="2026-04-25T18:00:00Z",
        rc_yml_path="/tmp/rc.yml",
        terraform_dir="/tmp/terraform",
        aws_profile="dev",
        path=registry,
    )
    assert rec.project == "myapp"
    assert rec.created_at  # populated
    assert registry.exists()
    data = json.loads(registry.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["project"] == "myapp"
    assert data[0]["aws_profile"] == "dev"


def test_register_stack_idempotent_updates_expires_at_preserves_created_at(registry):
    first = ephemeral.register_stack(
        project="myapp",
        region="us-east-1",
        expires_at="2026-04-25T18:00:00Z",
        rc_yml_path="/tmp/rc.yml",
        terraform_dir="/tmp/tf",
        path=registry,
        now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
    )
    second = ephemeral.register_stack(
        project="myapp",
        region="us-east-1",
        expires_at="2026-04-26T18:00:00Z",
        rc_yml_path="/tmp/rc.yml",
        terraform_dir="/tmp/tf",
        path=registry,
        now=datetime(2026, 4, 25, 18, 0, tzinfo=timezone.utc),
    )
    records = ephemeral.list_records(path=registry)
    assert len(records) == 1
    assert records[0].expires_at == "2026-04-26T18:00:00Z"
    # created_at preserved across the rerun even though "now" advanced
    assert records[0].created_at == first.created_at
    # the returned record from the second call also reflects the original
    assert second.created_at == first.created_at


def test_register_stack_two_distinct_stacks_coexist(registry):
    ephemeral.register_stack(
        project="a",
        region="us-east-1",
        expires_at="2026-04-25T18:00:00Z",
        rc_yml_path="/p/a/rc.yml",
        terraform_dir="/p/a/tf",
        path=registry,
    )
    ephemeral.register_stack(
        project="b",
        region="us-east-1",
        expires_at="2026-04-25T19:00:00Z",
        rc_yml_path="/p/b/rc.yml",
        terraform_dir="/p/b/tf",
        path=registry,
    )
    # Same project, different region -> distinct
    ephemeral.register_stack(
        project="a",
        region="us-west-2",
        expires_at="2026-04-25T20:00:00Z",
        rc_yml_path="/p/a/rc.yml",
        terraform_dir="/p/a/tf",
        path=registry,
    )
    records = ephemeral.list_records(path=registry)
    assert {r.key for r in records} == {
        ("a", "us-east-1"),
        ("b", "us-east-1"),
        ("a", "us-west-2"),
    }


def test_remove_stack_drops_only_matching_record(registry):
    for proj in ("a", "b", "c"):
        ephemeral.register_stack(
            project=proj,
            region="us-east-1",
            expires_at="2026-04-25T18:00:00Z",
            rc_yml_path="/p/rc.yml",
            terraform_dir="/p/tf",
            path=registry,
        )
    assert ephemeral.remove_stack(project="b", region="us-east-1", path=registry)
    remaining = {r.project for r in ephemeral.list_records(path=registry)}
    assert remaining == {"a", "c"}


def test_remove_stack_returns_false_when_no_match(registry):
    ephemeral.register_stack(
        project="a",
        region="us-east-1",
        expires_at="2026-04-25T18:00:00Z",
        rc_yml_path="/p/rc.yml",
        terraform_dir="/p/tf",
        path=registry,
    )
    assert (
        ephemeral.remove_stack(
            project="missing",
            region="us-east-1",
            path=registry,
        )
        is False
    )
    assert len(ephemeral.list_records(path=registry)) == 1


def test_find_expired_partitions_by_now(registry):
    ephemeral.register_stack(
        project="past",
        region="us-east-1",
        expires_at="2020-01-01T00:00:00Z",
        rc_yml_path="/p/rc.yml",
        terraform_dir="/p/tf",
        path=registry,
    )
    ephemeral.register_stack(
        project="future",
        region="us-east-1",
        expires_at="2099-01-01T00:00:00Z",
        rc_yml_path="/p/rc.yml",
        terraform_dir="/p/tf",
        path=registry,
    )
    expired = ephemeral.find_expired(
        now=datetime(2026, 4, 25, tzinfo=timezone.utc),
        path=registry,
    )
    assert {r.project for r in expired} == {"past"}


def test_corrupt_registry_raises_clear_error(registry):
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{not json")
    with pytest.raises(ValueError, match="corrupt JSON"):
        ephemeral.list_records(path=registry)


def test_non_list_registry_raises_clear_error(registry):
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text('{"oops": true}')
    with pytest.raises(ValueError, match="must contain a JSON list"):
        ephemeral.list_records(path=registry)


def test_register_stack_creates_parent_dir(tmp_path):
    nested = tmp_path / "deep" / "nest" / "ephemeral.json"
    ephemeral.register_stack(
        project="a",
        region="us-east-1",
        expires_at="2026-04-25T18:00:00Z",
        rc_yml_path="/p/rc.yml",
        terraform_dir="/p/tf",
        path=nested,
    )
    assert nested.exists()


# ---------------------------------------------------------------------------
# Provider integration: tags propagate through emit_terraform
# ---------------------------------------------------------------------------


def test_expires_at_emits_ephemeral_tags_in_providers_tf(tmp_path):
    """When ctx.expires_at is set, providers.tf gains Ephemeral + ExpiresAt."""
    from remote_compose.provider import DeployContext, ServiceSpec
    from remote_compose.provider.ecs import ECSProvider

    ctx = DeployContext(
        project="ttl-demo",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "ttl-demo-cluster",
                "vpc_cidr": "10.99.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
            )
        },
        secrets=[],
        expires_at="2026-04-25T22:00:00Z",
    )
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(ctx, out)
    providers = (out / "providers.tf").read_text()
    assert 'Ephemeral   = "true"' in providers
    assert 'ExpiresAt   = "2026-04-25T22:00:00Z"' in providers


def test_no_expires_at_means_no_ephemeral_tags(tmp_path):
    from remote_compose.provider import DeployContext, ServiceSpec
    from remote_compose.provider.ecs import ECSProvider

    ctx = DeployContext(
        project="prod-app",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-east-1",
                "cluster": "prod-app-cluster",
                "vpc_cidr": "10.99.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "web": ServiceSpec(
                name="web",
                cpu=256,
                memory=512,
                type="proxy",
                public=True,
                port=80,
            )
        },
        secrets=[],
    )
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(ctx, out)
    providers = (out / "providers.tf").read_text()
    assert "Ephemeral" not in providers
    assert "ExpiresAt" not in providers


# ---------------------------------------------------------------------------
# CLI: rc deploy --ttl + rc reap (FakeProvider, no AWS)
# ---------------------------------------------------------------------------


def test_deploy_ttl_then_reap_round_trip_via_fake_provider(tmp_path, monkeypatch):
    """End-to-end: `rc deploy --ttl 0s` registers a stack and `rc reap`
    locates + destroys it using the FakeProvider (no AWS needed).

    Demonstrates the full lifecycle path the bead's acceptance criteria
    describe, exercising every code seam this feature touches:

      cli.deploy --ttl  ->  cli_v2.dispatch_if_v2  ->  ephemeral.register_stack
                                                 ->  provider.deploy
      cli.reap          ->  ephemeral.find_expired
                        ->  provider.destroy
                        ->  ephemeral.remove_stack
    """
    from click.testing import CliRunner

    registry = tmp_path / "ephemeral.json"
    monkeypatch.setattr(ephemeral, "DEFAULT_REGISTRY_PATH", registry)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx\n"
    )
    (project_dir / "rc.yml").write_text(
        "version: 2\n"
        "project: ttl-demo\n"
        "compose_file: docker-compose.yml\n"
        "provider: fake\n"
        "provider_config:\n"
        "  fake: {}\n"
        "terraform:\n"
        "  output_dir: ./tf\n"
        "  backend:\n"
        "    type: local\n"
        "services:\n"
        "  web:\n"
        "    cpu: 256\n"
        "    memory: 512\n"
        "    type: application\n"
        "    public: true\n"
        "    port: 80\n"
    )

    from remote_compose.cli import cli

    runner = CliRunner()

    deploy_result = runner.invoke(
        cli,
        ["-c", str(project_dir / "rc.yml"), "deploy", "--ttl", "0s"],
    )
    assert deploy_result.exit_code == 0, deploy_result.output
    assert "Ephemeral: stack expires at" in deploy_result.output

    records = ephemeral.list_records(path=registry)
    assert len(records) == 1
    assert records[0].project == "ttl-demo"

    # Past-due (expires_at == now since ttl=0s). Reap should destroy + clear.
    reap_result = runner.invoke(cli, ["reap", "--yes"])
    assert reap_result.exit_code == 0, reap_result.output
    assert "destroyed, 0 failed" in reap_result.output
    assert ephemeral.list_records(path=registry) == []


def test_reap_dry_run_lists_without_destroying(tmp_path, monkeypatch):
    from click.testing import CliRunner

    registry = tmp_path / "ephemeral.json"
    monkeypatch.setattr(ephemeral, "DEFAULT_REGISTRY_PATH", registry)
    ephemeral.register_stack(
        project="past-due",
        region="us-east-1",
        expires_at="2020-01-01T00:00:00Z",
        rc_yml_path=str(tmp_path / "missing.yml"),
        terraform_dir=str(tmp_path / "tf"),
        path=registry,
    )

    from remote_compose.cli import cli

    result = CliRunner().invoke(cli, ["reap", "--dry-run"])
    assert result.exit_code == 0
    assert "past-due" in result.output
    assert "nothing destroyed" in result.output
    # Registry untouched
    assert len(ephemeral.list_records(path=registry)) == 1


def test_reap_skips_non_v2_rc_yml_with_clear_failure(tmp_path, monkeypatch):
    from click.testing import CliRunner

    registry = tmp_path / "ephemeral.json"
    monkeypatch.setattr(ephemeral, "DEFAULT_REGISTRY_PATH", registry)

    rc = tmp_path / "rc.yml"
    rc.write_text("cluster: foo\nregion: us-east-1\nproject_name: legacy\n")
    ephemeral.register_stack(
        project="legacy",
        region="us-east-1",
        expires_at="2020-01-01T00:00:00Z",
        rc_yml_path=str(rc),
        terraform_dir=str(tmp_path / "tf"),
        path=registry,
    )

    from remote_compose.cli import cli

    result = CliRunner().invoke(cli, ["reap", "--yes"])
    assert result.exit_code == 1
    assert "not v2" in result.output
    # Registry preserved so the user can investigate.
    assert len(ephemeral.list_records(path=registry)) == 1
