"""
Service for executing Docker Compose commands on remote hosts.
"""

import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from typing import Optional, List, Dict

import yaml

from ..models import DockerContext, DeploymentTarget
from ..conf import get_setting
from ..utils.ssh import SSHClient
from ..exceptions import (
    ComposeFileError,
    DockerComposeError,
    ValidationError,
)
from .base import BaseService
from .credential_service import CredentialService

# Pattern for valid project names (Docker Compose standard)
PROJECT_NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_.-]*$')

# Pattern for valid remote paths (no shell metacharacters)
SAFE_PATH_PATTERN = re.compile(r'^[a-zA-Z0-9/_.-]+$')

# Pattern for valid environment variable names
ENV_VAR_NAME_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

# Dangerous environment variables that should not be overridden
PROTECTED_ENV_VARS = frozenset({
    'PATH', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH',
    'HOME', 'USER', 'SHELL', 'PWD',
})


@dataclass
class ComposeResult:
    """Result of a compose command execution."""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    command: str


class ComposeService(BaseService):
    """
    Service for executing Docker Compose commands on remote hosts.
    """

    def __init__(self, credential_service: Optional[CredentialService] = None, **kwargs):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.compose_command = get_setting('DOCKER_COMPOSE_COMMAND', 'docker compose')

    def validate_compose_file(self, compose_path: str) -> dict:
        """
        Validate a docker-compose.yml file.

        Args:
            compose_path: Path to compose file

        Returns:
            Dict with validation result and parsed content
        """
        if not os.path.exists(compose_path):
            raise ComposeFileError(f"Compose file not found: {compose_path}")

        try:
            with open(compose_path, 'r') as f:
                content = f.read()

            # Parse YAML
            parsed = yaml.safe_load(content)

            if not parsed:
                raise ComposeFileError("Compose file is empty")

            # Basic validation
            if 'services' not in parsed:
                raise ComposeFileError("Compose file must contain 'services' key")

            services = list(parsed.get('services', {}).keys())

            return {
                'valid': True,
                'content': content,
                'parsed': parsed,
                'services': services,
            }

        except yaml.YAMLError as e:
            raise ComposeFileError(f"Invalid YAML syntax: {e}")

    def read_compose_file(self, compose_path: str) -> str:
        """
        Read and return compose file content.

        Args:
            compose_path: Path to compose file

        Returns:
            Compose file content as string
        """
        if not os.path.exists(compose_path):
            raise ComposeFileError(f"Compose file not found: {compose_path}")

        with open(compose_path, 'r') as f:
            return f.read()

    def upload_compose_files(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        env_file_path: Optional[str] = None,
        remote_dir: str = '/tmp/remote-compose',
    ) -> dict:
        """
        Upload compose file and optional env file to remote host.

        Args:
            ssh_client: Connected SSHClient
            compose_path: Local path to compose file
            env_file_path: Optional local path to .env file
            remote_dir: Remote directory to upload to

        Returns:
            Dict with remote file paths
        """
        # Validate remote directory path
        remote_dir = self._validate_path(remote_dir)

        # Ensure remote directory exists using validated path
        ssh_client.execute(f'mkdir -p {shlex.quote(remote_dir)}')

        # Read and upload compose file
        compose_content = self.read_compose_file(compose_path)
        remote_compose_path = f"{remote_dir}/docker-compose.yml"
        ssh_client.upload_content(compose_content, remote_compose_path)

        result = {
            'compose_path': remote_compose_path,
            'env_path': None,
            'remote_dir': remote_dir,
        }

        # Upload env file if provided
        if env_file_path and os.path.exists(env_file_path):
            with open(env_file_path, 'r') as f:
                env_content = f.read()
            remote_env_path = f"{remote_dir}/.env"
            ssh_client.upload_content(env_content, remote_env_path)
            result['env_path'] = remote_env_path

        return result

    def execute_compose(
        self,
        ssh_client: SSHClient,
        command: str,
        compose_path: str,
        project_name: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ComposeResult:
        """
        Execute a docker compose command on remote host.

        Args:
            ssh_client: Connected SSHClient
            command: Compose command (up, down, ps, etc.)
            compose_path: Remote path to compose file
            project_name: Optional project name
            env_vars: Optional environment variables
            timeout: Command timeout in seconds

        Returns:
            ComposeResult with command output
        """
        # Build command
        cmd_parts = []

        # Add environment variables (validated)
        if env_vars:
            for key, value in env_vars.items():
                # Validate environment variable name and value
                self._validate_env_var(key, value)
                # Use shlex.quote for proper shell escaping
                escaped_value = shlex.quote(value)
                cmd_parts.append(f'export {key}={escaped_value};')

        # Build compose command
        compose_cmd = [self.compose_command]

        if project_name:
            # Validate project name
            self._validate_project_name(project_name)
            compose_cmd.extend(['-p', project_name])

        compose_cmd.extend(['-f', compose_path, command])

        cmd_parts.append(' '.join(compose_cmd))

        full_command = ' '.join(cmd_parts)

        self.log_debug(f"Executing compose command: {command}")

        result = ssh_client.execute(full_command, timeout=timeout)

        return ComposeResult(
            success=result.success,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            command=full_command,
        )

    def up(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        detached: bool = True,
        build: bool = False,
        remove_orphans: bool = True,
        timeout: Optional[int] = None,
    ) -> ComposeResult:
        """
        Run docker compose up on remote host.

        Args:
            ssh_client: Connected SSHClient
            compose_path: Remote path to compose file
            project_name: Optional project name
            env_vars: Optional environment variables
            detached: Run in detached mode
            build: Build images before starting
            remove_orphans: Remove orphaned containers
            timeout: Command timeout

        Returns:
            ComposeResult
        """
        cmd = 'up'

        if detached:
            cmd += ' -d'
        if build:
            cmd += ' --build'
        if remove_orphans:
            cmd += ' --remove-orphans'

        return self.execute_compose(
            ssh_client=ssh_client,
            command=cmd,
            compose_path=compose_path,
            project_name=project_name,
            env_vars=env_vars,
            timeout=timeout,
        )

    def down(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
        remove_volumes: bool = False,
        remove_images: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ComposeResult:
        """
        Run docker compose down on remote host.

        Args:
            ssh_client: Connected SSHClient
            compose_path: Remote path to compose file
            project_name: Optional project name
            remove_volumes: Remove named volumes
            remove_images: Remove images ('all' or 'local')
            timeout: Command timeout

        Returns:
            ComposeResult
        """
        cmd = 'down'

        if remove_volumes:
            cmd += ' -v'
        if remove_images:
            cmd += f' --rmi {remove_images}'

        return self.execute_compose(
            ssh_client=ssh_client,
            command=cmd,
            compose_path=compose_path,
            project_name=project_name,
            timeout=timeout,
        )

    def ps(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
    ) -> ComposeResult:
        """
        Run docker compose ps on remote host.

        Returns:
            ComposeResult with container status
        """
        return self.execute_compose(
            ssh_client=ssh_client,
            command='ps',
            compose_path=compose_path,
            project_name=project_name,
        )

    def logs(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
        service: Optional[str] = None,
        tail: Optional[int] = 100,
        timeout: Optional[int] = None,
    ) -> ComposeResult:
        """
        Get logs from docker compose services.

        Args:
            ssh_client: Connected SSHClient
            compose_path: Remote path to compose file
            project_name: Optional project name
            service: Optional specific service name
            tail: Number of lines to tail
            timeout: Command timeout

        Returns:
            ComposeResult with logs
        """
        cmd = 'logs'

        if tail:
            cmd += f' --tail {tail}'

        if service:
            cmd += f' {service}'

        return self.execute_compose(
            ssh_client=ssh_client,
            command=cmd,
            compose_path=compose_path,
            project_name=project_name,
            timeout=timeout,
        )

    def pull(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ComposeResult:
        """
        Pull images for docker compose services.

        Returns:
            ComposeResult
        """
        return self.execute_compose(
            ssh_client=ssh_client,
            command='pull',
            compose_path=compose_path,
            project_name=project_name,
            timeout=timeout,
        )

    def restart(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
        service: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ComposeResult:
        """
        Restart docker compose services.

        Returns:
            ComposeResult
        """
        cmd = 'restart'
        if service:
            cmd += f' {service}'

        return self.execute_compose(
            ssh_client=ssh_client,
            command=cmd,
            compose_path=compose_path,
            project_name=project_name,
            timeout=timeout,
        )

    def get_container_ids(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
    ) -> List[str]:
        """
        Get container IDs for running compose services.

        Returns:
            List of container IDs
        """
        result = self.execute_compose(
            ssh_client=ssh_client,
            command='ps -q',
            compose_path=compose_path,
            project_name=project_name,
        )

        if not result.success:
            return []

        return [cid.strip() for cid in result.stdout.strip().split('\n') if cid.strip()]

    def get_service_status(
        self,
        ssh_client: SSHClient,
        compose_path: str,
        project_name: Optional[str] = None,
    ) -> Dict[str, dict]:
        """
        Get status of all services.

        Returns:
            Dict mapping service names to status info
        """
        result = self.ps(ssh_client, compose_path, project_name)

        if not result.success:
            return {}

        # Parse ps output
        status = {}
        lines = result.stdout.strip().split('\n')

        # Skip header line
        for line in lines[1:]:
            if not line.strip():
                continue

            # Parse line (format varies by compose version)
            parts = line.split()
            if len(parts) >= 4:
                name = parts[0]
                state = parts[3] if len(parts) > 3 else 'unknown'
                status[name] = {
                    'name': name,
                    'state': state,
                    'raw': line,
                }

        return status

    def cleanup_remote_files(
        self,
        ssh_client: SSHClient,
        remote_dir: str,
    ) -> bool:
        """
        Clean up uploaded files from remote host.

        Args:
            ssh_client: Connected SSHClient
            remote_dir: Remote directory to remove

        Returns:
            True if successful
        """
        # Validate path before executing
        remote_dir = self._validate_path(remote_dir)
        result = ssh_client.execute(f'rm -rf {shlex.quote(remote_dir)}')
        return result.success

    def _validate_project_name(self, project_name: str) -> str:
        """
        Validate Docker Compose project name.

        Args:
            project_name: Project name to validate

        Returns:
            Validated project name

        Raises:
            ValidationError: If project name is invalid
        """
        if not project_name:
            raise ValidationError("Project name cannot be empty")

        if len(project_name) > 100:
            raise ValidationError("Project name too long (max 100 characters)")

        if not PROJECT_NAME_PATTERN.match(project_name):
            raise ValidationError(
                f"Invalid project name: {project_name}. "
                "Must start with lowercase letter or digit and contain only "
                "lowercase letters, digits, underscores, periods, or hyphens."
            )

        return project_name

    def _validate_path(self, path: str) -> str:
        """
        Validate remote path to prevent command injection.

        Args:
            path: Path to validate

        Returns:
            Validated path

        Raises:
            ValidationError: If path is invalid or potentially dangerous
        """
        if not path:
            raise ValidationError("Path cannot be empty")

        # Check for path traversal
        if '..' in path:
            raise ValidationError("Path traversal not allowed")

        # Check for shell metacharacters
        if not SAFE_PATH_PATTERN.match(path):
            raise ValidationError(
                f"Invalid path characters: {path}. "
                "Only alphanumeric, slash, underscore, period, and hyphen allowed."
            )

        # Ensure path is absolute and reasonable
        if not path.startswith('/'):
            raise ValidationError("Path must be absolute")

        if len(path) > 500:
            raise ValidationError("Path too long (max 500 characters)")

        return path

    def _validate_env_var(self, name: str, value: str) -> None:
        """
        Validate environment variable name and value.

        Args:
            name: Environment variable name
            value: Environment variable value

        Raises:
            ValidationError: If name or value is invalid
        """
        if not name:
            raise ValidationError("Environment variable name cannot be empty")

        if not ENV_VAR_NAME_PATTERN.match(name):
            raise ValidationError(
                f"Invalid environment variable name: {name}. "
                "Must start with letter or underscore, contain only "
                "letters, digits, and underscores."
            )

        if name in PROTECTED_ENV_VARS:
            raise ValidationError(
                f"Cannot override protected environment variable: {name}"
            )

        if len(value) > 32768:  # 32KB limit
            raise ValidationError(
                f"Environment variable value too long: {name}"
            )

    def _mask_sensitive_env_vars(self, env_vars: Dict[str, str]) -> Dict[str, str]:
        """Mask sensitive environment variable values for logging."""
        sensitive_patterns = [
            r'password',
            r'secret',
            r'token',
            r'key',
            r'credential',
            r'auth',
        ]

        masked = {}
        for key, value in env_vars.items():
            key_lower = key.lower()
            is_sensitive = any(
                re.search(pattern, key_lower)
                for pattern in sensitive_patterns
            )
            masked[key] = '***' if is_sensitive else value

        return masked
