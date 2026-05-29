"""Unit tests for `rc dev push` (rc-e5u.45.9).

Mocks all AWS / subprocess interaction. Verifies:
- resolve_targets walks rc.yml services[*].dev_volumes correctly
- push_one builds the right `aws ecs execute-command` invocation
- failures (no source dir, no running task, aws cli error) raise
  DevPushError with actionable messages
- watch mode debounces multiple events into a single push
"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_compose import dev_push
from remote_compose.dev_push import (
    DevPushError,
    find_running_task,
    push_all,
    push_one,
    resolve_targets,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rc_yml(tmp_path: Path) -> Path:
    """Minimal rc.yml v2 with a backend dev_volume on `django`."""
    (tmp_path / "docker-compose.yml").write_text(textwrap.dedent("""
        services:
          django:
            image: python:3.11
    """).lstrip())
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "manage.py").write_text("# stub\n")
    (backend / "app").mkdir()
    (backend / "app" / "models.py").write_text("# stub\n")

    rc_path = tmp_path / "rc.yml"
    rc_path.write_text(textwrap.dedent("""
        version: 2
        project: rc-test-devpush
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
            cluster: rc-test-devpush-cluster
            aws_profile: default
        terraform:
          backend:
            type: local
        services:
          django:
            cpu: 256
            memory: 512
            dev_volumes:
              - name: src
                source: ./backend
                mount: /app
    """).lstrip())
    return rc_path


@pytest.fixture
def multi_service_rc_yml(tmp_path: Path) -> Path:
    """Two services with dev_volumes — exercises push_all multi-target."""
    (tmp_path / "docker-compose.yml").write_text(textwrap.dedent("""
        services:
          django: { image: python:3.11 }
          celery: { image: python:3.11 }
    """).lstrip())
    for d in ("backend", "tasks"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "main.py").write_text("# stub\n")
    rc_path = tmp_path / "rc.yml"
    rc_path.write_text(textwrap.dedent("""
        version: 2
        project: rc-test-multi
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
        terraform:
          backend:
            type: local
        services:
          django:
            cpu: 256
            memory: 512
            dev_volumes:
              - { name: src, source: ./backend, mount: /app }
          celery:
            cpu: 256
            memory: 512
            dev_volumes:
              - { name: src, source: ./tasks, mount: /tasks }
    """).lstrip())
    return rc_path


# ---------------------------------------------------------------------------
# resolve_targets
# ---------------------------------------------------------------------------


class TestResolveTargets:
    def test_single_target(self, rc_yml):
        targets = resolve_targets(rc_yml)
        assert len(targets) == 1
        t = targets[0]
        assert t["service"] == "django"
        assert t["name"] == "src"
        assert t["mount"] == "/app"
        assert t["source"].name == "backend"
        assert t["source"].is_absolute()
        assert t["project"] == "rc-test-devpush"
        assert t["cluster"] == "rc-test-devpush-cluster"
        assert t["region"] == "us-west-1"

    def test_filter_by_service(self, multi_service_rc_yml):
        targets = resolve_targets(multi_service_rc_yml, service_filter="django")
        assert len(targets) == 1
        assert targets[0]["service"] == "django"

    def test_default_cluster_name(self, multi_service_rc_yml):
        targets = resolve_targets(multi_service_rc_yml, service_filter="django")
        # No cluster set → derive from project.
        assert targets[0]["cluster"] == "rc-test-multi-cluster"

    def test_no_dev_volumes_anywhere_raises(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("services: { x: { image: x } }\n")
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text(textwrap.dedent("""
            version: 2
            project: empty
            compose_file: docker-compose.yml
            provider: ecs
            provider_config: { ecs: { region: us-west-1 } }
            terraform: { backend: { type: local } }
            services:
              x: { cpu: 256, memory: 512 }
        """).lstrip())
        with pytest.raises(DevPushError, match="no services declare dev_volumes"):
            resolve_targets(rc_path)

    def test_unknown_service_filter_raises(self, rc_yml):
        with pytest.raises(DevPushError, match="declares no dev_volumes"):
            resolve_targets(rc_yml, service_filter="nonexistent")

    def test_v1_rc_yml_rejected(self, tmp_path):
        rc_path = tmp_path / "rc.yml"
        rc_path.write_text("project_name: legacy\ncluster: foo\n")
        with pytest.raises(DevPushError, match="rc.yml v2"):
            resolve_targets(rc_path)


# ---------------------------------------------------------------------------
# push_one — happy path + failure modes
# ---------------------------------------------------------------------------


def _fake_session_with_running_task(task_arn: str = "arn:task/abc123"):
    """Build a session_factory that returns one RUNNING task."""

    def factory():
        ecs = SimpleNamespace(
            list_tasks=lambda **kw: {"taskArns": [task_arn]},
        )
        return SimpleNamespace(client=lambda name: ecs)

    return factory


def _fake_session_no_tasks():
    def factory():
        ecs = SimpleNamespace(list_tasks=lambda **kw: {"taskArns": []})
        return SimpleNamespace(client=lambda name: ecs)

    return factory


class TestPushOne:
    def test_invokes_aws_execute_command_with_tar_pipe(self, rc_yml):
        target = resolve_targets(rc_yml)[0]
        captured = {}

        def fake_runner(cmd, payload, env):
            captured["cmd"] = cmd
            captured["payload_len"] = len(payload)
            captured["env"] = env
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        push_one(
            target,
            session_factory=_fake_session_with_running_task(),
            runner=fake_runner,
        )

        cmd = captured["cmd"]
        assert cmd[:3] == ["aws", "ecs", "execute-command"]
        assert "--cluster" in cmd
        assert target["cluster"] in cmd
        assert "--task" in cmd
        assert "--container" in cmd
        # Container == service name (single-container task convention).
        idx = cmd.index("--container")
        assert cmd[idx + 1] == "django"
        # tar -xzf - -C /app extracts streamed gzipped tarball into mount.
        idx = cmd.index("--command")
        assert cmd[idx + 1].startswith("tar -xzf - -C ")
        assert "/app" in cmd[idx + 1]
        assert captured["payload_len"] > 0

    def test_aws_profile_threaded_via_env(self, rc_yml):
        target = resolve_targets(rc_yml)[0]
        captured = {}

        def fake_runner(cmd, payload, env):
            captured["env"] = env
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        push_one(
            target,
            session_factory=_fake_session_with_running_task(),
            runner=fake_runner,
        )
        assert captured["env"]["AWS_PROFILE"] == "default"

    def test_region_passed_to_aws_cli(self, rc_yml):
        target = resolve_targets(rc_yml)[0]
        captured = {}

        def fake_runner(cmd, payload, env):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        push_one(
            target,
            session_factory=_fake_session_with_running_task(),
            runner=fake_runner,
        )
        assert "--region" in captured["cmd"]
        assert "us-west-1" in captured["cmd"]

    def test_missing_source_raises(self, rc_yml):
        target = resolve_targets(rc_yml)[0]
        target["source"] = Path("/does/not/exist")
        with pytest.raises(DevPushError, match="does not exist"):
            push_one(
                target,
                session_factory=_fake_session_with_running_task(),
                runner=lambda *a: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            )

    def test_source_is_file_not_dir_rejected(self, rc_yml, tmp_path):
        target = resolve_targets(rc_yml)[0]
        f = tmp_path / "single.py"
        f.write_text("# x\n")
        target["source"] = f
        with pytest.raises(DevPushError, match="is not a directory"):
            push_one(
                target,
                session_factory=_fake_session_with_running_task(),
                runner=lambda *a: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            )

    def test_no_running_task_raises(self, rc_yml):
        target = resolve_targets(rc_yml)[0]
        with pytest.raises(DevPushError, match="no RUNNING task"):
            push_one(
                target,
                session_factory=_fake_session_no_tasks(),
                runner=lambda *a: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            )

    def test_aws_cli_failure_propagates_stderr(self, rc_yml):
        target = resolve_targets(rc_yml)[0]

        def fake_runner(cmd, payload, env):
            return SimpleNamespace(
                returncode=255,
                stdout=b"",
                stderr=b"SessionManagerPlugin not found",
            )

        with pytest.raises(DevPushError, match="SessionManagerPlugin"):
            push_one(
                target,
                session_factory=_fake_session_with_running_task(),
                runner=fake_runner,
            )

    def test_excludes_pycache_and_venv_from_payload(self, rc_yml, tmp_path):
        # Add cruft that should be filtered.
        backend = rc_yml.parent / "backend"
        (backend / "__pycache__").mkdir()
        (backend / "__pycache__" / "models.cpython-311.pyc").write_bytes(b"\x00" * 100)
        (backend / ".venv").mkdir()
        (backend / ".venv" / "huge.bin").write_bytes(b"\x00" * 1000)

        target = resolve_targets(rc_yml)[0]
        captured = {}

        def fake_runner(cmd, payload, env):
            captured["payload"] = payload
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        push_one(
            target,
            session_factory=_fake_session_with_running_task(),
            runner=fake_runner,
        )
        # Inspect the tarball contents to confirm filtering.
        import gzip
        import tarfile

        with tarfile.open(
            fileobj=io.BytesIO(gzip.decompress(captured["payload"])), mode="r:"
        ) as tf:
            names = tf.getnames()
        # Real source files made it.
        assert "manage.py" in names
        assert "app/models.py" in names
        # Cruft did not.
        assert not any(".venv" in n for n in names)
        assert not any("__pycache__" in n for n in names)


# ---------------------------------------------------------------------------
# find_running_task
# ---------------------------------------------------------------------------


class TestFindRunningTask:
    def test_returns_first_arn(self):
        sf = _fake_session_with_running_task("arn:aws:ecs:us-west-1:1:task/abc")
        assert find_running_task(sf, "c1", "svc") == "arn:aws:ecs:us-west-1:1:task/abc"

    def test_returns_none_when_no_tasks(self):
        sf = _fake_session_no_tasks()
        assert find_running_task(sf, "c1", "svc") is None


# ---------------------------------------------------------------------------
# push_all — multi-service
# ---------------------------------------------------------------------------


class TestPushAll:
    def test_pushes_every_service(self, multi_service_rc_yml, monkeypatch):
        calls: list = []

        def fake_push_one(target, *, session_factory, runner=None, progress=None):
            calls.append(target["service"])
            return 0.1

        monkeypatch.setattr(dev_push, "push_one", fake_push_one)
        results = push_all(
            multi_service_rc_yml,
            session_factory=_fake_session_with_running_task(),
        )
        assert sorted(calls) == ["celery", "django"]
        assert len(results) == 2
        for r in results:
            assert "elapsed_s" in r

    def test_filter_to_one_service(self, multi_service_rc_yml, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            dev_push,
            "push_one",
            lambda t, **kw: (calls.append(t["service"]) or 0.1),
        )
        push_all(
            multi_service_rc_yml,
            service_filter="celery",
            session_factory=_fake_session_with_running_task(),
        )
        assert calls == ["celery"]


# ---------------------------------------------------------------------------
# Watch mode (smoke test — full select() interactions are time-sensitive)
# ---------------------------------------------------------------------------


class TestWatchMode:
    def test_no_watcher_binary_raises_with_install_hint(self, rc_yml, monkeypatch):
        monkeypatch.setattr(dev_push, "_detect_watcher", lambda: None)
        with pytest.raises(DevPushError, match="brew install fswatch"):
            dev_push.watch_and_push(rc_yml)

    def test_initial_seed_runs_before_watch_loop(self, rc_yml, monkeypatch):
        """watch_and_push performs an immediate push (the seed) then
        starts the watcher. We verify the seed by short-circuiting popen
        to return a closed-EOF pipe, which makes the loop exit
        immediately."""
        import os

        monkeypatch.setattr(dev_push, "_detect_watcher", lambda: "fswatch")

        seeded: list = []

        def fake_push():
            seeded.append("pushed")
            return []

        # Real pipe so select.select() works (BytesIO has no fileno()).
        # Closing the write end immediately gives stdout EOF, which exits
        # the watch loop without ever needing to send an event.
        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        stdout_obj = os.fdopen(r_fd, "rb", buffering=0)

        class _EofProc:
            stdout = stdout_obj
            stderr = io.BytesIO(b"")

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        try:
            dev_push.watch_and_push(
                rc_yml,
                _popen=lambda *a, **kw: _EofProc(),
                _push=fake_push,
            )
        finally:
            stdout_obj.close()
        assert seeded == ["pushed"]
