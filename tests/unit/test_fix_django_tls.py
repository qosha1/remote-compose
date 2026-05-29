"""rc-j08: rc fix django-tls — patch Django settings so it consumes
CSRF_TRUSTED_ORIGINS env var (set by rc up --domain) + tells Django
the ALB terminates TLS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_compose.fix_django_tls import (
    _RC_J08_MARKER,
    _autodetect_settings,
    _resolve_settings_path,
    fix_django_tls,
    has_rc_j08_marker,
)


def _scaffold(
    tmp_path: Path,
    settings_rel: str = "backend/config/settings/local.py",
    settings_content: str = None,
) -> Path:
    p = tmp_path / settings_rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(settings_content or "ALLOWED_HOSTS = ['*']\nDEBUG = True\n")
    return tmp_path


class TestAutodetect:
    def test_finds_backend_config_settings_local(self, tmp_path):
        _scaffold(tmp_path, "backend/config/settings/local.py")
        path = _autodetect_settings(tmp_path)
        assert path == tmp_path / "backend/config/settings/local.py"

    def test_finds_config_settings_local(self, tmp_path):
        _scaffold(tmp_path, "config/settings/local.py")
        path = _autodetect_settings(tmp_path)
        assert path == tmp_path / "config/settings/local.py"

    def test_finds_flat_config_settings_py(self, tmp_path):
        _scaffold(tmp_path, "config/settings.py")
        path = _autodetect_settings(tmp_path)
        assert path == tmp_path / "config/settings.py"

    def test_returns_none_when_nothing_matches(self, tmp_path):
        # Empty dir.
        assert _autodetect_settings(tmp_path) is None


class TestResolveSettingsPath:
    def test_dotted_module(self, tmp_path):
        _scaffold(tmp_path, "backend/config/settings/local.py")
        path = _resolve_settings_path(tmp_path, "config.settings.local")
        assert path == (tmp_path / "backend/config/settings/local.py").resolve()

    def test_relative_path(self, tmp_path):
        _scaffold(tmp_path, "myapp/settings.py")
        path = _resolve_settings_path(tmp_path, "myapp/settings.py")
        assert path == (tmp_path / "myapp/settings.py").resolve()


class TestPatchAppend:
    def test_appends_block_with_marker(self, tmp_path):
        _scaffold(tmp_path)
        result = fix_django_tls(tmp_path)
        assert result.appended is True
        assert result.skipped_reason is None
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        assert _RC_J08_MARKER in content
        assert "CSRF_TRUSTED_ORIGINS" in content
        assert "SECURE_PROXY_SSL_HEADER" in content
        assert "USE_X_FORWARDED_HOST" in content
        # Original content preserved.
        assert "ALLOWED_HOSTS = ['*']" in content
        assert "DEBUG = True" in content

    def test_idempotent_skips_on_rerun(self, tmp_path):
        _scaffold(tmp_path)
        first = fix_django_tls(tmp_path)
        assert first.appended is True
        second = fix_django_tls(tmp_path)
        assert second.appended is False
        assert "marker already present" in second.skipped_reason
        # Marker only appears once.
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        assert content.count(_RC_J08_MARKER) == 1

    def test_force_re_appends(self, tmp_path):
        _scaffold(tmp_path)
        fix_django_tls(tmp_path)
        result = fix_django_tls(tmp_path, force=True)
        assert result.appended is True
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        assert content.count(_RC_J08_MARKER) == 2

    def test_secure_cookies_opt_in(self, tmp_path):
        _scaffold(tmp_path)
        fix_django_tls(tmp_path, secure_cookies=True)
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        assert "SESSION_COOKIE_SECURE = True" in content
        assert "CSRF_COOKIE_SECURE = True" in content

    def test_secure_cookies_default_off(self, tmp_path):
        _scaffold(tmp_path)
        fix_django_tls(tmp_path)
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        assert "SESSION_COOKIE_SECURE" not in content
        assert "CSRF_COOKIE_SECURE" not in content

    def test_explicit_settings_module_dotted(self, tmp_path):
        _scaffold(tmp_path, "backend/config/settings/production.py")
        result = fix_django_tls(
            tmp_path,
            settings_module="config.settings.production",
        )
        assert result.appended is True
        prod = (tmp_path / "backend/config/settings/production.py").read_text()
        assert _RC_J08_MARKER in prod


class TestErrorPaths:
    def test_unknown_module_raises(self, tmp_path):
        _scaffold(tmp_path)
        with pytest.raises(ValueError, match="could not resolve"):
            fix_django_tls(tmp_path, settings_module="nonexistent.module")

    def test_no_settings_anywhere_raises(self, tmp_path):
        # Empty project_dir.
        with pytest.raises(ValueError, match="could not auto-detect"):
            fix_django_tls(tmp_path)

    def test_nonexistent_project_dir_raises(self, tmp_path):
        ghost = tmp_path / "ghost"
        with pytest.raises(ValueError, match="not found"):
            fix_django_tls(ghost)


class TestRendersValidPython:
    """The appended block must be syntactically valid Python so Django
    can import the patched settings without SyntaxError."""

    def test_patched_settings_imports_clean(self, tmp_path):
        import ast

        _scaffold(tmp_path)
        fix_django_tls(tmp_path)
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        # Should parse cleanly as Python.
        ast.parse(content)

    def test_patched_settings_with_secure_cookies_imports_clean(self, tmp_path):
        import ast

        _scaffold(tmp_path)
        fix_django_tls(tmp_path, secure_cookies=True)
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        ast.parse(content)


class TestHasRcJ08Marker:
    """rc-frx: deploy preflight uses has_rc_j08_marker to detect when a
    Django service has a domain set (CSRF env vars being injected) but
    its settings module does not contain the rc-j08 patch — admin POST
    would 403."""

    def test_returns_path_when_marker_present(self, tmp_path):
        _scaffold(tmp_path)
        fix_django_tls(tmp_path)
        path = has_rc_j08_marker(tmp_path)
        assert path == tmp_path / "backend/config/settings/local.py"

    def test_returns_none_when_marker_absent(self, tmp_path):
        _scaffold(tmp_path)  # local.py exists but no patch
        assert has_rc_j08_marker(tmp_path) is None

    def test_returns_none_when_no_settings_file(self, tmp_path):
        # Empty project dir.
        assert has_rc_j08_marker(tmp_path) is None

    def test_returns_none_when_project_dir_missing(self, tmp_path):
        ghost = tmp_path / "ghost"
        assert has_rc_j08_marker(ghost) is None

    def test_explicit_settings_module_with_marker(self, tmp_path):
        _scaffold(tmp_path, "backend/config/settings/production.py")
        fix_django_tls(tmp_path, settings_module="config.settings.production")
        path = has_rc_j08_marker(tmp_path, settings_module="config.settings.production")
        assert path == (tmp_path / "backend/config/settings/production.py").resolve()

    def test_explicit_settings_module_without_marker(self, tmp_path):
        _scaffold(tmp_path, "backend/config/settings/production.py")
        # Don't apply the patch.
        assert (
            has_rc_j08_marker(
                tmp_path,
                settings_module="config.settings.production",
            )
            is None
        )

    def test_unknown_settings_module_returns_none_silently(self, tmp_path):
        _scaffold(tmp_path)
        # Unknown module → return None rather than raising. Caller decides
        # whether this counts as 'not Django' vs 'broken config'.
        assert has_rc_j08_marker(tmp_path, settings_module="bogus.path") is None


class TestPatchExecutionApplied:
    """When the patched settings is exec'd with CSRF_TRUSTED_ORIGINS in
    the env, the resulting module's CSRF_TRUSTED_ORIGINS attribute is
    populated."""

    def test_env_var_propagates_to_settings_at_import(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "CSRF_TRUSTED_ORIGINS",
            "https://app.example.com,https://www.example.com",
        )
        _scaffold(tmp_path, settings_content="DEBUG = False\n")
        fix_django_tls(tmp_path)
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        ns: dict = {}
        exec(content, ns)
        assert ns["CSRF_TRUSTED_ORIGINS"] == [
            "https://app.example.com",
            "https://www.example.com",
        ]
        assert ns["SECURE_PROXY_SSL_HEADER"] == (
            "HTTP_X_FORWARDED_PROTO",
            "https",
        )
        assert ns["USE_X_FORWARDED_HOST"] is True

    def test_no_env_var_means_no_csrf_attr(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CSRF_TRUSTED_ORIGINS", raising=False)
        _scaffold(tmp_path, settings_content="DEBUG = False\n")
        fix_django_tls(tmp_path)
        content = (tmp_path / "backend/config/settings/local.py").read_text()
        ns: dict = {}
        exec(content, ns)
        # Without the env var set, the patch leaves CSRF_TRUSTED_ORIGINS
        # at whatever Django's default would be (not set in the patched
        # block). The other two settings always apply.
        assert (
            "CSRF_TRUSTED_ORIGINS" not in ns or ns.get("CSRF_TRUSTED_ORIGINS") is None
        )
        assert ns["SECURE_PROXY_SSL_HEADER"] is not None
