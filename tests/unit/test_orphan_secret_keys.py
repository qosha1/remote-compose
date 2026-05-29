"""rc-e5u.38: detect orphan keys in SM blobs that no task def references.

Repro: user adds DJANGO_ALLOWED_HOSTS to .envs/.foo/.django, then runs
rc secrets push. The new key is uploaded to SM, but the task def's
secrets[] block doesn't reference it (task def was emitted before the
key existed). Containers never see the value until rc deploy --no-build
re-emits the task def.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


from remote_compose.cli_commands._dispatchers import (
    _detect_orphan_secret_keys_v2,
)

_DEFAULT_SVC = object()


def _v2(project="myproj", services=_DEFAULT_SVC) -> SimpleNamespace:
    if services is _DEFAULT_SVC:
        services = {"django": object()}
    return SimpleNamespace(
        project=project,
        services=services,
    )


def _file_secret(name: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, path=path, source="file")


def _make_ecs_mock(*, td_secrets):
    """ecs.describe_services returns a single service whose task-def
    contains the supplied secrets[] entries."""
    ecs = MagicMock()
    ecs.describe_services.return_value = {
        "services": [{"taskDefinition": "arn:aws:ecs:...:task-definition/django:42"}],
    }
    ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [
                {
                    "name": "django",
                    "secrets": td_secrets,
                }
            ],
        },
    }
    return ecs


def _td_secret(env_name: str, sm_simple: str, key: str) -> dict:
    return {
        "name": env_name,
        "valueFrom": (
            f"arn:aws:secretsmanager:us-west-2:111:secret:"
            f"{sm_simple}-AbC123:{key}::"
        ),
    }


class TestDetectOrphanKeys:
    def test_no_orphans_when_all_keys_referenced(self, tmp_path):
        env_file = tmp_path / ".django"
        env_file.write_text("FOO=bar\nBAZ=qux\n")
        ecs = _make_ecs_mock(
            td_secrets=[
                _td_secret("FOO", "myproj/django", "FOO"),
                _td_secret("BAZ", "myproj/django", "BAZ"),
            ]
        )
        orphans = _detect_orphan_secret_keys_v2(
            ecs,
            "cluster",
            _v2(),
            [_file_secret("django", str(env_file))],
            tmp_path,
        )
        assert orphans == {}

    def test_detects_new_key_in_sm_not_in_task_def(self, tmp_path):
        env_file = tmp_path / ".django"
        env_file.write_text("FOO=bar\nNEWKEY=newval\n")
        # Task def only references FOO — NEWKEY is orphan.
        ecs = _make_ecs_mock(
            td_secrets=[
                _td_secret("FOO", "myproj/django", "FOO"),
            ]
        )
        orphans = _detect_orphan_secret_keys_v2(
            ecs,
            "cluster",
            _v2(),
            [_file_secret("django", str(env_file))],
            tmp_path,
        )
        assert orphans == {"myproj/django": {"NEWKEY"}}

    def test_multiple_orphan_keys(self, tmp_path):
        env_file = tmp_path / ".django"
        env_file.write_text("A=1\nB=2\nC=3\n")
        ecs = _make_ecs_mock(
            td_secrets=[
                _td_secret("A", "myproj/django", "A"),
            ]
        )
        orphans = _detect_orphan_secret_keys_v2(
            ecs,
            "cluster",
            _v2(),
            [_file_secret("django", str(env_file))],
            tmp_path,
        )
        assert orphans == {"myproj/django": {"B", "C"}}

    def test_multiple_secrets_each_with_orphans(self, tmp_path):
        django_env = tmp_path / ".django"
        django_env.write_text("D_OLD=1\nD_NEW=2\n")
        postgres_env = tmp_path / ".postgres"
        postgres_env.write_text("P_OLD=1\nP_NEW=2\n")
        ecs = _make_ecs_mock(
            td_secrets=[
                _td_secret("D_OLD", "myproj/django", "D_OLD"),
                _td_secret("P_OLD", "myproj/postgres", "P_OLD"),
            ]
        )
        orphans = _detect_orphan_secret_keys_v2(
            ecs,
            "cluster",
            _v2(),
            [
                _file_secret("django", str(django_env)),
                _file_secret("postgres", str(postgres_env)),
            ],
            tmp_path,
        )
        assert orphans == {
            "myproj/django": {"D_NEW"},
            "myproj/postgres": {"P_NEW"},
        }

    def test_describe_services_failure_returns_empty(self, tmp_path):
        env_file = tmp_path / ".django"
        env_file.write_text("FOO=bar\n")
        ecs = MagicMock()
        ecs.describe_services.side_effect = RuntimeError("network")
        # Best-effort — does not raise.
        orphans = _detect_orphan_secret_keys_v2(
            ecs,
            "cluster",
            _v2(),
            [_file_secret("django", str(env_file))],
            tmp_path,
        )
        assert orphans == {}

    def test_no_services_returns_empty(self, tmp_path):
        env_file = tmp_path / ".django"
        env_file.write_text("FOO=bar\n")
        orphans = _detect_orphan_secret_keys_v2(
            MagicMock(),
            "cluster",
            _v2(services={}),
            [_file_secret("django", str(env_file))],
            tmp_path,
        )
        assert orphans == {}

    def test_no_file_secrets_returns_empty(self, tmp_path):
        orphans = _detect_orphan_secret_keys_v2(
            MagicMock(),
            "cluster",
            _v2(),
            [],
            tmp_path,
        )
        assert orphans == {}
