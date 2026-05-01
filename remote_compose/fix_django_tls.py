"""rc fix django-tls — append env-reading TLS / CSRF / proxy settings
to a Django settings module so the app correctly handles requests
behind an HTTPS-terminating ALB (or other reverse proxy).

Without this, even after `rc up --domain` injects CSRF_TRUSTED_ORIGINS
into the container env, Django's settings.py never reads the env var
and the admin login (and any CSRF-protected POST) returns 403
"Origin checking failed".

Three universally-correct settings get appended:

  CSRF_TRUSTED_ORIGINS       — read from env so rc-32x's injection works
  SECURE_PROXY_SSL_HEADER    — tells Django the ALB-terminated TLS via
                               X-Forwarded-Proto, so request.is_secure()
                               returns True under HTTPS
  USE_X_FORWARDED_HOST       — so build_absolute_uri() returns the real
                               public host (needed for email links,
                               OAuth callbacks, etc.)

Two opt-in via --secure-cookies (only when the user can guarantee
HTTPS-only access — typical for ALB-fronted prod stacks):

  SESSION_COOKIE_SECURE
  CSRF_COOKIE_SECURE

Idempotent via the rc-j08 marker line at the top of the appended block.
Re-running is a no-op unless --force is passed.

Mirrors the rc fix nginx-conf / rc fix bake-bind-mount-source patterns.
rc-j08.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Idempotency marker — re-runs detect this line and skip unless --force.
_RC_J08_MARKER = "# rc-j08: TLS / CSRF / proxy settings auto-appended by rc fix django-tls"


@dataclass
class FixResult:
    settings_path: Optional[Path] = None
    appended: bool = False
    skipped_reason: Optional[str] = None


def has_rc_j08_marker(
    project_dir: Path,
    settings_module: Optional[str] = None,
) -> Optional[Path]:
    """rc-frx: return the settings path when the rc-j08 marker is present,
    None when the marker is missing (or no settings file can be located).

    Used by the deploy preflight to detect drift: if a Django service has
    a domain set (CSRF_TRUSTED_ORIGINS env var is being injected by
    rc-32x) but settings.py never reads the env var, /admin POST will
    return 403 even though everything else is wired up correctly.

    Best-effort: returns None silently when the project lacks a Django
    layout — the caller decides whether that counts as a problem.
    """
    if not project_dir.exists() or not project_dir.is_dir():
        return None
    if settings_module:
        try:
            path = _resolve_settings_path(project_dir, settings_module)
        except Exception:
            return None
    else:
        path = _autodetect_settings(project_dir)
    if path is None or not path.exists():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return path if _RC_J08_MARKER in text else None


def _resolve_settings_path(project_dir: Path, settings_module: Optional[str]) -> Optional[Path]:
    """Resolve a Django settings module to a settings.py path.

    Accepts:
      - Dotted module ("config.settings.local") → project_dir/config/settings/local.py
      - Relative path ("config/settings/local.py")
      - Absolute path

    Returns None when the path doesn't exist.
    """
    if settings_module is None:
        return None
    if "/" in settings_module or "\\" in settings_module:
        # Path-like — treat as relative to project_dir.
        p = Path(settings_module)
        if not p.is_absolute():
            p = (project_dir / p).resolve()
        return p if p.exists() else None
    # Dotted module path → file path.
    rel = Path(*settings_module.split("."))
    candidates = [
        project_dir / rel.with_suffix(".py"),
        project_dir / "backend" / rel.with_suffix(".py"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _autodetect_settings(project_dir: Path) -> Optional[Path]:
    """Walk common Django layouts to find the active settings module.

    Looks for:
      backend/config/settings/local.py
      backend/config/settings.py
      config/settings/local.py
      config/settings.py
      <project>/settings.py

    Returns the first existing match, or None.
    """
    candidates = [
        project_dir / "backend" / "config" / "settings" / "local.py",
        project_dir / "backend" / "config" / "settings.py",
        project_dir / "config" / "settings" / "local.py",
        project_dir / "config" / "settings.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Last-resort: any settings.py one level deep.
    for entry in project_dir.iterdir():
        if not entry.is_dir():
            continue
        cand = entry / "settings.py"
        if cand.exists():
            return cand
    return None


def _build_patch(secure_cookies: bool) -> str:
    """Return the block of Python to append to settings.py.

    Always-on: CSRF_TRUSTED_ORIGINS reader, SECURE_PROXY_SSL_HEADER,
    USE_X_FORWARDED_HOST.

    Opt-in (secure_cookies=True): SESSION_COOKIE_SECURE,
    CSRF_COOKIE_SECURE.
    """
    base = (
        "\n\n"
        f"{_RC_J08_MARKER}\n"
        "# DO NOT EDIT — re-run `rc fix django-tls` to update.\n"
        "import os as _rc_j08_os\n"
        "_rc_csrf = _rc_j08_os.environ.get('CSRF_TRUSTED_ORIGINS', '').strip()\n"
        "if _rc_csrf:\n"
        "    CSRF_TRUSTED_ORIGINS = [\n"
        "        o.strip() for o in _rc_csrf.split(',') if o.strip()\n"
        "    ]\n"
        "# ALB terminates TLS; tell Django to trust X-Forwarded-Proto so\n"
        "# request.is_secure() returns True under HTTPS.\n"
        "SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')\n"
        "# build_absolute_uri() must return the public ALB hostname\n"
        "# (not the task-internal IP) for email links / OAuth callbacks.\n"
        "USE_X_FORWARDED_HOST = True\n"
    )
    if secure_cookies:
        base += (
            "# Opt-in via --secure-cookies: cookies only travel over HTTPS.\n"
            "SESSION_COOKIE_SECURE = True\n"
            "CSRF_COOKIE_SECURE = True\n"
        )
    return base


def fix_django_tls(
    project_dir: Path,
    settings_module: Optional[str] = None,
    secure_cookies: bool = False,
    force: bool = False,
) -> FixResult:
    """Append the TLS / CSRF / proxy settings block to the active Django
    settings module.

    Args:
        project_dir: the rc.yml's parent dir (the django repo root).
        settings_module: explicit override — dotted module or relative
            path. When None, auto-detected from common layouts.
        secure_cookies: opt in to SESSION_COOKIE_SECURE +
            CSRF_COOKIE_SECURE. Off by default since some users have
            paths reachable over plain HTTP (e.g. internal health
            checks via service-to-service hostnames).
        force: re-append the block even when the marker is present.

    Returns a :class:`FixResult` with skipped_reason set when no edit
    was made (file not found, marker already present, etc.).
    """
    if not project_dir.exists() or not project_dir.is_dir():
        raise ValueError(f"project_dir not found: {project_dir}")

    if settings_module:
        path = _resolve_settings_path(project_dir, settings_module)
        if path is None:
            raise ValueError(
                f"could not resolve settings module {settings_module!r} "
                f"under {project_dir}"
            )
    else:
        path = _autodetect_settings(project_dir)
        if path is None:
            raise ValueError(
                f"could not auto-detect Django settings under {project_dir}. "
                f"Pass --settings <module> to specify "
                f"(e.g. config.settings.local)."
            )

    existing = path.read_text()
    if _RC_J08_MARKER in existing and not force:
        return FixResult(
            settings_path=path,
            appended=False,
            skipped_reason="rc-j08 marker already present (use --force to re-append)",
        )

    patch = _build_patch(secure_cookies=secure_cookies)
    path.write_text(existing.rstrip() + patch)
    return FixResult(settings_path=path, appended=True)
