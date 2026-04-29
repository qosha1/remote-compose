"""Unit tests for remote_compose.terraform.emitter."""

from __future__ import annotations

import pytest

from remote_compose.terraform.emitter import TerraformEmitter


@pytest.fixture
def template_dir(tmp_path):
    d = tmp_path / "tmpl"
    d.mkdir()
    (d / "main.tf.j2").write_text(
        'resource "aws_vpc" "main" {\n'
        '  cidr_block = "{{ vpc_cidr }}"\n'
        '  tags = { Name = "{{ project }}" }\n'
        '}\n'
    )
    (d / "variables.tf.j2").write_text(
        'variable "region" { default = "{{ region }}" }\n'
    )
    (d / "README.md").write_text("Passthrough README\n")
    return d


class TestRender:
    def test_renders_templates_into_out_dir(self, template_dir, tmp_path):
        out = tmp_path / "out"
        em = TerraformEmitter(template_dir)
        written = em.render({
            "vpc_cidr": "10.0.0.0/16",
            "project": "myapp",
            "region": "us-west-2",
        }, out)

        assert (out / "main.tf").exists()
        assert (out / "variables.tf").exists()
        assert (out / "README.md").exists()
        assert len(written) == 3

    def test_j2_suffix_stripped(self, template_dir, tmp_path):
        out = tmp_path / "out"
        em = TerraformEmitter(template_dir)
        em.render({"vpc_cidr": "10.0.0.0/16", "project": "p", "region": "r"}, out)
        assert not (out / "main.tf.j2").exists()

    def test_passthrough_files_copied_verbatim(self, template_dir, tmp_path):
        out = tmp_path / "out"
        em = TerraformEmitter(template_dir)
        em.render({"vpc_cidr": "1", "project": "p", "region": "r"}, out)
        assert (out / "README.md").read_text() == "Passthrough README\n"

    def test_render_is_deterministic(self, template_dir, tmp_path):
        """Same context → byte-identical output. FR-7 gate."""
        em = TerraformEmitter(template_dir)
        ctx = {"vpc_cidr": "10.0.0.0/16", "project": "myapp", "region": "us-west-2"}
        em.render(ctx, tmp_path / "a")
        em.render(ctx, tmp_path / "b")
        for name in ["main.tf", "variables.tf", "README.md"]:
            assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()

    def test_strict_undefined_raises(self, template_dir, tmp_path):
        em = TerraformEmitter(template_dir, strict_undefined=True)
        with pytest.raises(Exception):
            em.render({"vpc_cidr": "1", "project": "p"}, tmp_path / "out")

    def test_render_string(self, template_dir):
        em = TerraformEmitter(template_dir)
        out = em.render_string("variables.tf.j2", {"region": "eu-central-1"})
        assert '"eu-central-1"' in out
