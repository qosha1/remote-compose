"""rc-878: rc db psql wrapper. Auto-discovers postgres task, reads
POSTGRES_USER/DB/PORT from the running task def's env, builds the
correct `aws ecs execute-command` invocation with PAGER=cat + psql
-P pager=off so output isn't mangled by less.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from remote_compose.cli import cli


def _scaffold_v2(tmp_path: Path) -> Path:
    rc_yml = tmp_path / "rc.yml"
    rc_yml.write_text(textwrap.dedent("""
        version: 2
        project: rc-test
        compose_file: docker-compose.yml
        provider: ecs
        provider_config:
          ecs:
            region: us-west-1
            cluster: rc-test-cluster
            vpc_cidr: 10.0.0.0/16
            aws_profile: my-profile
        services:
          postgres:
            type: infrastructure
        backup:
          service: postgres
    """).strip())
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  postgres:\n    image: postgres:15\n"
    )
    return rc_yml


def _make_session_factory(env_overrides: dict = None):
    env_overrides = env_overrides or {}

    def factory(profile_name=None, region_name=None):
        sess = mock.MagicMock()
        ecs = mock.MagicMock()
        ecs.list_tasks.return_value = {
            "taskArns": ["arn:aws:ecs:us-west-1:111:task/rc-test-cluster/abc123"],
        }
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "postgres",
                        "environment": [
                            {"name": "POSTGRES_USER", "value": "appuser"},
                            {"name": "POSTGRES_DB", "value": "backend"},
                            {"name": "POSTGRES_PORT", "value": "5434"},
                            *[
                                {"name": k, "value": v}
                                for k, v in env_overrides.items()
                            ],
                        ],
                    }
                ],
            },
        }
        ecs.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskDefinitionArn": "arn:aws:ecs:us-west-1:111:task-definition/rc-test-postgres:1",
                }
            ],
        }
        sess.client.return_value = ecs
        return sess

    return factory


class TestDbPsql:
    def test_one_shot_command_invokes_aws_with_correct_psql_args(self, tmp_path):
        rc_yml = _scaffold_v2(tmp_path)
        runner = CliRunner()
        captured: dict = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="42\n", stderr="")

        with (
            mock.patch("boto3.Session", side_effect=_make_session_factory()),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            result = runner.invoke(
                cli,
                ["-c", str(rc_yml), "db", "psql", "-c", "SELECT count(*) FROM x"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        cmd = captured["cmd"]
        # Outer aws ecs execute-command shape
        assert cmd[0] == "aws"
        assert "ecs" in cmd
        assert "execute-command" in cmd
        # Cluster + container wired
        assert "rc-test-cluster" in cmd
        assert "postgres" in cmd
        # Profile wired from rc.yml
        assert "my-profile" in cmd
        # Inner psql command has the right flags
        inner_idx = cmd.index("--command")
        inner = cmd[inner_idx + 1]
        assert "PAGER=cat" in inner
        assert "psql" in inner
        assert "-P pager=off" in inner
        assert "-p 5434" in inner
        assert "appuser" in inner
        assert "backend" in inner
        assert "SELECT count(*) FROM x" in inner

    def test_db_override(self, tmp_path):
        rc_yml = _scaffold_v2(tmp_path)
        runner = CliRunner()
        captured: dict = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch("boto3.Session", side_effect=_make_session_factory()),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            result = runner.invoke(
                cli,
                ["-c", str(rc_yml), "db", "psql", "-d", "otherdb", "-c", "\\dt"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        inner = captured["cmd"][captured["cmd"].index("--command") + 1]
        # -d flag should override the env-detected DB
        assert "-d 'otherdb'" in inner

    def test_no_running_task_errors_clearly(self, tmp_path):
        rc_yml = _scaffold_v2(tmp_path)
        runner = CliRunner()

        def factory(profile_name=None, region_name=None):
            sess = mock.MagicMock()
            ecs = mock.MagicMock()
            ecs.list_tasks.return_value = {"taskArns": []}
            sess.client.return_value = ecs
            return sess

        with mock.patch("boto3.Session", side_effect=factory):
            result = runner.invoke(
                cli,
                ["-c", str(rc_yml), "db", "psql", "-c", "select 1"],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert "no running task" in result.output.lower()

    def test_v1_rcyml_rejected(self, tmp_path):
        rc_yml = tmp_path / "rc.yml"
        rc_yml.write_text("cluster: legacy\nproject_name: x\n")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(rc_yml), "db", "psql", "-c", "x"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "v2" in result.output.lower()

    def test_nonzero_psql_exit_propagates(self, tmp_path):
        rc_yml = _scaffold_v2(tmp_path)
        runner = CliRunner()

        def fake_run(cmd, *args, **kwargs):
            return mock.Mock(returncode=3, stdout="", stderr="psql: error")

        with (
            mock.patch("boto3.Session", side_effect=_make_session_factory()),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            result = runner.invoke(
                cli,
                ["-c", str(rc_yml), "db", "psql", "-c", "broken sql"],
                catch_exceptions=False,
            )
        assert result.exit_code == 3
