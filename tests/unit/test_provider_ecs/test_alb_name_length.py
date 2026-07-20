"""aws_lb.main.name stays within AWS's 32-char ALB limit for long projects (uxuu).

AWS caps ALB names at 32 chars. A bare ``${var.project}-alb`` overflows once the
project passes 28 chars (e.g. ``foundry-tenant-marketing-agents-alb`` = 35). That
FAILED the real terraform apply for the marketing-agents tenant even though the
control-plane dry-run (which only ran plan on shorter fixtures) passed. The
template guards the name with a length check and falls back to a deterministic
``truncate + md5`` name only when the readable name would overflow, so short
projects (mcr, qafoundry) keep ``<project>-alb`` and never churn. Live proof: the
marketing-agents ALB is ``foundry-tenant-mark-aed909a1``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider


def _alb_tf(tmp_path: Path, *, project: str) -> str:
    ctx = DeployContext(
        project=project,
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": {"region": "us-west-2", "cluster": "c", "vpc_cidr": "10.0.0.0/16"}},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            # public service so the provider emits the ALB (aws_lb.main)
            "web": ServiceSpec(
                name="web", cpu=256, memory=512, type="proxy",
                public=True, port=80, health_check_path="/",
            ),
        },
        secrets=[],
    )
    out = tmp_path / "terraform"
    ECSProvider().emit_terraform(ctx, out)
    return (out / "alb.tf").read_text()


def _alb_name_expr(alb_tf: str) -> str:
    """The RHS of `name =` inside the aws_lb.main resource (skips comments and
    the target group's name_prefix)."""
    in_block = False
    for line in alb_tf.splitlines():
        if re.match(r'\s*resource "aws_lb" "main"', line):
            in_block = True
            continue
        if in_block:
            if line.strip() == "}":
                break
            m = re.match(r"\s*name\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip()
    raise AssertionError(f"aws_lb.main name not found in:\n{alb_tf}")


def _substr_bounds(expr: str) -> tuple[int, int]:
    """(A, B) from the fallback substr(project,0,A) + '-' + substr(md5,0,B).

    Fails with a clear message (not a raw AttributeError) when the guarded
    fallback is missing — i.e. someone reverted to a bare `${var.project}-alb`.
    """
    ma = re.search(r"substr\(var\.project,\s*0,\s*(\d+)\)", expr)
    mb = re.search(r"substr\(md5\(var\.project\),\s*0,\s*(\d+)\)", expr)
    assert ma and mb, f"ALB name lost its truncate+md5 fallback: {expr!r}"
    return int(ma.group(1)), int(mb.group(1))


def test_alb_name_is_length_guarded(tmp_path):
    """The name must be wrapped in a 32-char length guard, never a bare
    `${var.project}-alb` (the form that overflowed for marketing-agents)."""
    expr = _alb_name_expr(_alb_tf(tmp_path, project="foundry-tenant-marketing-agents"))
    assert 'length("${var.project}-alb") <= 32' in expr
    assert "substr(md5(var.project)" in expr


def test_alb_fallback_name_fits_32_by_construction(tmp_path):
    """The truncate+md5 fallback is substr(project,0,A) + '-' + substr(md5,0,B);
    read A and B straight from the template and assert A+1+B <= 32, so bumping a
    substr bound into overflow territory fails here."""
    a, b = _substr_bounds(_alb_name_expr(_alb_tf(tmp_path, project="foundry-tenant-marketing-agents")))
    assert a + 1 + b <= 32, f"fallback ALB name can reach {a + 1 + b} chars > 32"


def test_every_representative_slug_yields_a_valid_alb_name(tmp_path):
    """Mirror the terraform ternary (using the template's own A/B bounds) and
    assert every representative tenant slug — including a 40-char slug that
    provision's _SLUG_RE still admits — produces a <= 32 char name. Also pin the
    known-good live marketing-agents value so md5/substr behaviour can't drift."""
    # template is project-independent, so parse the substr bounds once
    a, b = _substr_bounds(_alb_name_expr(_alb_tf(tmp_path, project="x")))

    def alb_name(project: str) -> str:
        base = f"{project}-alb"
        if len(base) <= 32:
            return base
        return f"{project[:a]}-{hashlib.md5(project.encode()).hexdigest()[:b]}"

    for slug in ["mcr", "qafoundry", "marketing-agents", "a" * 40]:
        project = f"foundry-tenant-{slug}"
        name = alb_name(project)
        assert len(name) <= 32, f"{project} -> {name} ({len(name)} chars)"

    # short slugs keep the readable name (no ALB churn on existing tenants)
    assert alb_name("foundry-tenant-mcr") == "foundry-tenant-mcr-alb"
    # long slug matches the live marketing-agents ALB exactly
    assert alb_name("foundry-tenant-marketing-agents") == "foundry-tenant-mark-aed909a1"
