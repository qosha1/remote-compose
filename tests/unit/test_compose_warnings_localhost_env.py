"""rc-7qq: detect_localhost_host_in_env_file warns when an env_file declares
<SVC>_HOST=localhost while a compose service of that name exists.

Sentinal repro: .test/.postgres had POSTGRES_HOST=localhost. ECS services
talk to each other via Cloud Map DNS (postgres.<project>.local), so the
correct value is the bare service name (postgres), not localhost.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from remote_compose.compose_warnings import detect_localhost_host_in_env_file


def _scaffold(tmp_path: Path, env_content: str, compose_yaml: str = None) -> Path:
    env_dir = tmp_path / ".envs"
    env_dir.mkdir()
    (env_dir / ".postgres").write_text(env_content)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(compose_yaml or textwrap.dedent("""
        services:
          django:
            image: django:test
            env_file: [.envs/.postgres]
          postgres:
            image: postgres:15
    """).strip())
    return compose


def test_warns_when_postgres_host_localhost_and_postgres_service_exists(tmp_path):
    compose_path = _scaffold(
        tmp_path,
        "POSTGRES_HOST=localhost\nPOSTGRES_PORT=5434\n",
    )
    import yaml as _y
    compose = _y.safe_load(compose_path.read_text())
    warnings = detect_localhost_host_in_env_file(compose, compose_path)
    assert len(warnings) == 1
    w = warnings[0]
    assert "POSTGRES_HOST=localhost" in w
    assert "postgres" in w  # mentions the target service
    assert "Cloud Map" in w


def test_warns_for_127_0_0_1_too(tmp_path):
    compose_path = _scaffold(tmp_path, "POSTGRES_HOST=127.0.0.1\n")
    import yaml as _y
    compose = _y.safe_load(compose_path.read_text())
    warnings = detect_localhost_host_in_env_file(compose, compose_path)
    assert len(warnings) == 1
    assert "127.0.0.1" in warnings[0]


def test_no_warning_when_no_matching_service(tmp_path):
    # REDIS_HOST=localhost but no redis service in compose → don't warn.
    compose_yaml = textwrap.dedent("""
        services:
          django:
            image: django:test
            env_file: [.envs/.postgres]
    """).strip()
    compose_path = _scaffold(
        tmp_path,
        "REDIS_HOST=localhost\n",
        compose_yaml=compose_yaml,
    )
    import yaml as _y
    compose = _y.safe_load(compose_path.read_text())
    warnings = detect_localhost_host_in_env_file(compose, compose_path)
    assert warnings == []


def test_no_warning_for_correct_postgres_host_value(tmp_path):
    compose_path = _scaffold(
        tmp_path,
        "POSTGRES_HOST=postgres\nPOSTGRES_PORT=5434\n",
    )
    import yaml as _y
    compose = _y.safe_load(compose_path.read_text())
    warnings = detect_localhost_host_in_env_file(compose, compose_path)
    assert warnings == []


def test_no_warning_for_bare_HOST_key(tmp_path):
    # Bare HOST=localhost is too generic to flag (could be SSH, mail, etc.)
    compose_path = _scaffold(tmp_path, "HOST=localhost\n")
    import yaml as _y
    compose = _y.safe_load(compose_path.read_text())
    warnings = detect_localhost_host_in_env_file(compose, compose_path)
    assert warnings == []


def test_handles_quoted_values(tmp_path):
    compose_path = _scaffold(tmp_path, 'POSTGRES_HOST="localhost"\n')
    import yaml as _y
    compose = _y.safe_load(compose_path.read_text())
    warnings = detect_localhost_host_in_env_file(compose, compose_path)
    assert len(warnings) == 1


def test_handles_missing_env_file_path(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          django:
            image: django:test
            env_file: [.envs/.does-not-exist]
          postgres:
            image: postgres:15
    """).strip())
    import yaml as _y
    compose_obj = _y.safe_load(compose.read_text())
    # No crash; just empty warnings.
    warnings = detect_localhost_host_in_env_file(compose_obj, compose)
    assert warnings == []
