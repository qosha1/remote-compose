"""Tests for ComposeToECSConverter._convert_command (remote-compose-l9o).

Compose accepts ``command:`` in both string and list form. The list form
maps to ECS task-def ``command``/``entryPoint`` directly; the string form
needs to be split into a list. Earlier behavior used ``str.split()`` which
broke on quoted args (``sh -c "echo hello"`` → ``['sh', '-c', '"echo',
'hello"']``). The fix uses ``shlex.split`` so quoting round-trips correctly.
"""

from __future__ import annotations

import pytest

from remote_compose.services.compose_converter import ComposeToECSConverter


@pytest.fixture
def converter():
    return ComposeToECSConverter()


class TestConvertCommandStringForm:
    def test_simple_string_splits_on_whitespace(self, converter):
        assert converter._convert_command("python manage.py runserver") == [
            "python",
            "manage.py",
            "runserver",
        ]

    def test_quoted_arg_round_trips(self, converter):
        # The remote-compose-l9o failure mode: ``sh -c "echo hello"``
        # MUST produce ``['sh', '-c', 'echo hello']`` so the third arg
        # is a single string the shell can interpret. str.split() would
        # split it into 4 tokens with stray quotes.
        assert converter._convert_command('sh -c "echo hello"') == [
            "sh",
            "-c",
            "echo hello",
        ]

    def test_single_quotes_also_round_trip(self, converter):
        assert converter._convert_command("sh -c 'echo hi there'") == [
            "sh",
            "-c",
            "echo hi there",
        ]

    def test_celery_command_with_long_flag_value(self, converter):
        # Real-world celery command with a multi-word --schedule arg.
        cmd = (
            "celery -A config beat --loglevel=info "
            '--scheduler "celery.beat.PersistentScheduler"'
        )
        assert converter._convert_command(cmd) == [
            "celery",
            "-A",
            "config",
            "beat",
            "--loglevel=info",
            "--scheduler",
            "celery.beat.PersistentScheduler",
        ]

    def test_empty_string_returns_empty_list(self, converter):
        assert converter._convert_command("") == []


class TestConvertCommandListForm:
    def test_list_passes_through_unchanged(self, converter):
        assert converter._convert_command(["python", "manage.py", "migrate"]) == [
            "python",
            "manage.py",
            "migrate",
        ]

    def test_list_coerces_non_str_entries(self, converter):
        # YAML can produce ints / bools — coerce to str so ECS task def
        # serializer doesn't choke.
        assert converter._convert_command(["sleep", 60]) == ["sleep", "60"]


class TestConvertCommandOther:
    def test_none_returns_empty_list(self, converter):
        assert converter._convert_command(None) == []

    def test_unsupported_type_returns_empty_list(self, converter):
        # Robustness: caller may have mangled the value upstream; better
        # to emit no command than crash the whole conversion.
        assert converter._convert_command({"weird": "shape"}) == []
        assert converter._convert_command(42) == []
