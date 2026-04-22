"""Jinja2-based renderer for terraform HCL templates.

Providers keep their HCL as ``.tf.j2`` files beside the provider module:

    remote_compose/provider/ecs/templates/main.tf.j2

The emitter walks a template directory, renders every file with the
supplied context, and writes the output to a target directory. Rendering
is deterministic — the same inputs always produce byte-identical output —
to satisfy the FR-7 idempotency contract test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2


class TerraformEmitter:
    """Render HCL templates from a template directory to a target directory.

    Parameters
    ----------
    template_dir:
        Directory holding ``*.tf.j2`` and other passthrough files.
    strict_undefined:
        If True (default), undefined template variables raise an error
        rather than rendering as empty strings. Keeps silent data loss from
        sneaking into generated HCL.
    """

    def __init__(self, template_dir: Path, strict_undefined: bool = True) -> None:
        self.template_dir = Path(template_dir)
        undefined = jinja2.StrictUndefined if strict_undefined else jinja2.Undefined
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.template_dir)),
            undefined=undefined,
            keep_trailing_newline=True,
            autoescape=False,
        )

    def render(self, context: dict[str, Any], out_dir: Path) -> list[Path]:
        """Render every template in the directory. Returns list of written files."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for src in sorted(self.template_dir.rglob("*")):
            if src.is_dir() or src.name.startswith("."):
                continue
            rel = src.relative_to(self.template_dir)
            if src.name.endswith(".j2"):
                target = out_dir / rel.with_name(rel.name[:-3])
                content = self._render_template(str(rel), context)
            else:
                target = out_dir / rel
                content = src.read_text()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            written.append(target)
        return written

    def render_string(self, template_name: str, context: dict[str, Any]) -> str:
        """Render a single named template to a string — useful in tests."""
        return self._render_template(template_name, context)

    def _render_template(self, name: str, context: dict[str, Any]) -> str:
        tmpl = self.env.get_template(name)
        return tmpl.render(**context)
