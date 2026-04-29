"""Revision id must be stable across terraform's own cache artifacts.

Regression for the e2e failure where two back-to-back deploys produced
different revision ids because `.terraform/` provider binaries and
`terraform.tfstate` accumulated between runs.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider.ecs.provider import _revision_id_from_dir


def _seed_emitted(dir: Path) -> None:
    (dir / "main.tf").write_text("# main\n")
    (dir / "variables.tf").write_text('variable "x" { default = "y" }\n')
    (dir / "README.md").write_text("generated\n")


def test_hash_stable_across_terraform_cache_pollution(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    _seed_emitted(a)
    rev_before = _revision_id_from_dir(a)

    # Simulate terraform apply artifacts showing up.
    (a / ".terraform").mkdir()
    (a / ".terraform" / "providers").mkdir()
    (a / ".terraform" / "providers" / "aws.so").write_bytes(b"\x7fELF" + b"A" * 1024)
    (a / ".terraform.lock.hcl").write_text("# lock\n")
    (a / "terraform.tfstate").write_text('{"version": 4}\n')
    (a / "terraform.tfstate.backup").write_text('{"version": 4, "old": true}\n')

    rev_after = _revision_id_from_dir(a)
    assert rev_before == rev_after, (
        "revision id changed after terraform cache artifacts appeared; it must "
        "hash only the emitter's own output"
    )


def test_hash_differs_for_different_emitted_content(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    _seed_emitted(a)
    _seed_emitted(b)
    (b / "main.tf").write_text("# MAIN different\n")
    assert _revision_id_from_dir(a) != _revision_id_from_dir(b)


def test_hash_ignores_subdirectories(tmp_path):
    """Subdirs are terraform's playground; top-level *.tf only."""
    a = tmp_path / "a"
    a.mkdir()
    _seed_emitted(a)
    first = _revision_id_from_dir(a)
    (a / "modules").mkdir()
    (a / "modules" / "extra.tf").write_text("# smuggled in\n")
    second = _revision_id_from_dir(a)
    assert first == second
