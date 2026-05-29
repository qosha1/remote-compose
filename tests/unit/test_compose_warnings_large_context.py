"""rc-2v8: detect_large_build_context warns when a service's build
context exceeds size thresholds. Sentinal repro: backend/ was 6.8GB
because backend/backend/media held 5.8GB of Django uploads. First build
hung 25+ min uploading context.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


from remote_compose.compose_warnings import detect_large_build_context


def _scaffold(
    tmp_path: Path,
    ctx_dir: str = "backend",
    file_sizes: dict[str, int] = None,
) -> Path:
    """Lay out a compose project with a buildable service and known
    file sizes under the build context."""
    file_sizes = file_sizes or {}
    ctx_path = tmp_path / ctx_dir
    ctx_path.mkdir()
    (ctx_path / "Dockerfile").write_text("FROM alpine\n")
    for rel, size in file_sizes.items():
        f = ctx_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        # Use seek to make a sparse file — actual disk usage is small but
        # st_size returns the configured size.
        with open(f, "wb") as fh:
            fh.seek(size - 1)
            fh.write(b"\0")
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(textwrap.dedent(f"""
        services:
          api:
            build:
              context: {ctx_dir}
              dockerfile: Dockerfile
            ports: ['80:80']
    """).strip())
    return compose


class TestThresholds:
    def test_no_warning_under_1gb(self, tmp_path):
        compose = _scaffold(tmp_path, file_sizes={"app/main.py": 100})
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert warnings == []

    def test_warn_at_1gb(self, tmp_path):
        # 1.2GB total: trigger WARN
        compose = _scaffold(
            tmp_path,
            file_sizes={
                "media/uploads.bin": 1200 * 1024 * 1024,
            },
        )
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert len(warnings) == 1
        w = warnings[0]
        assert w.startswith("WARN:")
        assert "1.2GB" in w
        assert "media" in w  # heaviest dir named
        assert ".dockerignore" in w

    def test_error_at_5gb(self, tmp_path):
        # 5.5GB total: trigger ERROR
        compose = _scaffold(
            tmp_path,
            file_sizes={
                "media/big.bin": 5500 * 1024 * 1024,
            },
        )
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert len(warnings) == 1
        assert warnings[0].startswith("ERROR:")


class TestDirSummary:
    def test_top_3_dirs_named(self, tmp_path):
        compose = _scaffold(
            tmp_path,
            file_sizes={
                "media/big.bin": 700 * 1024 * 1024,
                "venv/lib.bin": 400 * 1024 * 1024,
                "node_modules/blah.bin": 200 * 1024 * 1024,
                "docs/extra.bin": 50 * 1024 * 1024,
            },
        )
        # 1.35 GB total → WARN
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert len(warnings) == 1
        w = warnings[0]
        # Top 3 should be media, venv, node_modules.
        assert "media" in w
        assert "venv" in w
        assert "node_modules" in w


class TestDedup:
    def test_shared_context_only_warned_once(self, tmp_path):
        # Two services sharing a build context → one warning.
        ctx = tmp_path / "shared"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM alpine\n")
        big = ctx / "data.bin"
        with open(big, "wb") as fh:
            fh.seek((1100 * 1024 * 1024) - 1)
            fh.write(b"\0")
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              api:
                build:
                  context: shared
                  dockerfile: Dockerfile
              worker:
                build:
                  context: shared
                  dockerfile: Dockerfile
        """).strip())
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert len(warnings) == 1


class TestNoBuildContext:
    def test_image_only_service_skipped(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(textwrap.dedent("""
            services:
              redis:
                image: redis:6
        """).strip())
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert warnings == []


class TestDockerignoreFiltering:
    def test_dockerignore_excludes_named_dirs(self, tmp_path):
        # 6GB pre-ignore, but .dockerignore excludes media → should be small.
        compose = _scaffold(
            tmp_path,
            file_sizes={
                "media/big.bin": 6 * 1024 * 1024 * 1024,
                "src/main.py": 100,
            },
        )
        # Add .dockerignore to the build context.
        ctx = tmp_path / "backend"
        (ctx / ".dockerignore").write_text("media\n")
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        # media excluded → context is tiny → no warning.
        assert warnings == []

    def test_dockerignore_excludes_nested_path(self, tmp_path):
        # Mimic sentinal: backend/backend/media is the heavy dir.
        compose = _scaffold(
            tmp_path,
            file_sizes={
                "backend/media/uploads.bin": 6 * 1024 * 1024 * 1024,
            },
        )
        ctx = tmp_path / "backend"
        (ctx / ".dockerignore").write_text("backend/media\n")
        import yaml as _y

        compose_obj = _y.safe_load(compose.read_text())
        warnings = detect_large_build_context(compose_obj, compose)
        assert warnings == []
