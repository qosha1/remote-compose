"""
SSH utilities for remote command execution.
"""

import io
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

import paramiko

from ..conf import get_setting
from ..exceptions import (
    SSHConnectionError,
    SSHAuthenticationError,
    SSHTimeoutError,
    SSHHostKeyError,
)

logger = logging.getLogger(__name__)


class StrictHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """
    Strict host key policy that rejects unknown hosts.

    This policy refuses to connect to hosts not in the known_hosts file
    unless explicitly configured to accept new hosts.
    """

    def __init__(self, auto_add_new: bool = False):
        """
        Initialize the policy.

        Args:
            auto_add_new: If True, automatically add new hosts to known_hosts.
                         This should only be used in controlled environments.
        """
        self.auto_add_new = auto_add_new

    def missing_host_key(self, client, hostname, key):
        if self.auto_add_new:
            # Add the key and log a warning
            logger.warning(
                f"Adding new host key for {hostname}: {key.get_name()} "
                f"{key.get_base64()[:20]}..."
            )
            client.get_host_keys().add(hostname, key.get_name(), key)
            return

        raise SSHHostKeyError(
            f"Host key verification failed for {hostname}. "
            f"Key type: {key.get_name()}, fingerprint: {key.get_base64()[:20]}... "
            "Add the host to known_hosts or set auto_add_hosts=True for trusted networks.",
            host=hostname,
        )


@dataclass
class CommandResult:
    """Result of a remote command execution."""

    stdout: str
    stderr: str
    exit_code: int
    command: str

    @property
    def success(self):
        return self.exit_code == 0


class SSHClient:
    """
    SSH client for executing commands on remote hosts.

    Usage:
        client = SSHClient(
            host='54.123.45.67',
            username='ubuntu',
            key_path='/path/to/key.pem'
        )
        result = client.execute('docker ps')
        print(result.stdout)
    """

    def __init__(
        self,
        host: str,
        username: str,
        key_path: Optional[str] = None,
        key_content: Optional[str] = None,
        port: int = 22,
        password: Optional[str] = None,
        connect_timeout: Optional[int] = None,
        command_timeout: Optional[int] = None,
        known_hosts_path: Optional[Union[str, Path]] = None,
        auto_add_hosts: bool = False,
    ):
        """
        Initialize SSH client.

        Args:
            host: Remote host address
            username: SSH username
            key_path: Path to SSH private key file
            key_content: SSH private key content (alternative to key_path)
            port: SSH port (default: 22)
            password: SSH password (if not using key)
            connect_timeout: Connection timeout in seconds
            command_timeout: Command execution timeout in seconds
            known_hosts_path: Path to known_hosts file. If None, uses
                ~/.ssh/known_hosts. Set to False to disable host key checking
                (NOT RECOMMENDED for production).
            auto_add_hosts: If True, automatically add new host keys to known_hosts.
                Only use in trusted network environments (e.g., private VPC).
        """
        self.host = host
        self.username = username
        self.key_path = key_path
        self.key_content = key_content
        self.port = port
        self.password = password
        self.connect_timeout = connect_timeout or get_setting(
            "SSH_CONNECTION_TIMEOUT", 30
        )
        self.command_timeout = command_timeout or get_setting(
            "SSH_COMMAND_TIMEOUT", 300
        )
        self.known_hosts_path = known_hosts_path
        self.auto_add_hosts = auto_add_hosts or get_setting("SSH_AUTO_ADD_HOSTS", False)

        self._client: Optional[paramiko.SSHClient] = None
        self._temp_key_file: Optional[str] = None

    def _get_pkey(self) -> Optional[paramiko.PKey]:
        """Load SSH private key."""
        key_source = None

        if self.key_content:
            key_source = io.StringIO(self.key_content)
        elif self.key_path:
            if not os.path.exists(self.key_path):
                raise SSHAuthenticationError(
                    f"SSH key file not found: {self.key_path}",
                    host=self.host,
                    port=self.port,
                )
            key_source = self.key_path

        if not key_source:
            return None

        # Try different key types
        key_classes = [
            paramiko.RSAKey,
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.DSSKey,
        ]

        for key_class in key_classes:
            try:
                if isinstance(key_source, io.StringIO):
                    key_source.seek(0)
                    return key_class.from_private_key(key_source)
                else:
                    return key_class.from_private_key_file(key_source)
            except paramiko.SSHException:
                continue

        raise SSHAuthenticationError(
            "Unable to load SSH key. Unsupported key format.",
            host=self.host,
            port=self.port,
        )

    def connect(self):
        """Establish SSH connection."""
        if self._client and self._client.get_transport():
            transport = self._client.get_transport()
            if transport and transport.is_active():
                return  # Already connected

        self._client = paramiko.SSHClient()

        # Load known hosts for host key verification
        if self.known_hosts_path is not False:
            known_hosts = self.known_hosts_path
            if known_hosts is None:
                # Use default known_hosts location
                known_hosts = Path.home() / ".ssh" / "known_hosts"

            if isinstance(known_hosts, (str, Path)) and Path(known_hosts).exists():
                try:
                    self._client.load_host_keys(str(known_hosts))
                    logger.debug(f"Loaded known hosts from {known_hosts}")
                except Exception as e:
                    logger.warning(f"Failed to load known hosts: {e}")

        # Set host key policy - strict by default
        self._client.set_missing_host_key_policy(
            StrictHostKeyPolicy(auto_add_new=self.auto_add_hosts)
        )

        try:
            pkey = self._get_pkey()

            connect_kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": self.connect_timeout,
                "allow_agent": False,
                "look_for_keys": False,
            }

            if pkey:
                connect_kwargs["pkey"] = pkey
            elif self.password:
                connect_kwargs["password"] = self.password
            else:
                # Allow agent and system keys if no explicit auth
                connect_kwargs["allow_agent"] = True
                connect_kwargs["look_for_keys"] = True

            logger.debug(f"Connecting to {self.username}@{self.host}:{self.port}")
            self._client.connect(**connect_kwargs)
            logger.info(f"Connected to {self.host}")

        except paramiko.AuthenticationException as e:
            raise SSHAuthenticationError(
                f"Authentication failed: {e}", host=self.host, port=self.port
            )
        except paramiko.SSHException as e:
            raise SSHConnectionError(f"SSH error: {e}", host=self.host, port=self.port)
        except TimeoutError:
            raise SSHTimeoutError(
                f"Connection timed out after {self.connect_timeout}s",
                host=self.host,
                port=self.port,
            )
        except Exception as e:
            raise SSHConnectionError(
                f"Failed to connect: {e}", host=self.host, port=self.port
            )

    def disconnect(self):
        """Close SSH connection."""
        if self._client:
            self._client.close()
            self._client = None
            logger.debug(f"Disconnected from {self.host}")

        # Clean up temporary key file if any
        if self._temp_key_file and os.path.exists(self._temp_key_file):
            os.unlink(self._temp_key_file)
            self._temp_key_file = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def execute(self, command: str, timeout: Optional[int] = None) -> CommandResult:
        """
        Execute a command on the remote host.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds (overrides default)

        Returns:
            CommandResult with stdout, stderr, and exit code
        """
        self.connect()

        timeout = timeout or self.command_timeout

        try:
            logger.debug(f"Executing command: {command[:100]}...")

            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)

            exit_code = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")

            logger.debug(f"Command exit code: {exit_code}")

            return CommandResult(
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=exit_code,
                command=command,
            )

        except paramiko.SSHException as e:
            raise SSHConnectionError(
                f"Command execution failed: {e}", host=self.host, port=self.port
            )

    def execute_commands(self, commands: list) -> list:
        """
        Execute multiple commands sequentially.

        Args:
            commands: List of commands to execute

        Returns:
            List of CommandResult objects
        """
        results = []
        for command in commands:
            result = self.execute(command)
            results.append(result)
            if not result.success:
                break  # Stop on first failure
        return results

    def test_connection(self) -> Tuple[bool, str]:
        """
        Test SSH connection to the remote host.

        Returns:
            Tuple of (success, message)
        """
        try:
            self.connect()
            result = self.execute('echo "connection_test"')
            if result.success and "connection_test" in result.stdout:
                return True, "Connection successful"
            return False, f"Connection test failed: {result.stderr}"
        except Exception as e:
            return False, str(e)
        finally:
            self.disconnect()

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        Upload a file to the remote host.

        Args:
            local_path: Local file path
            remote_path: Remote destination path

        Returns:
            True if successful
        """
        self.connect()

        try:
            sftp = self._client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            logger.debug(f"Uploaded {local_path} to {remote_path}")
            return True
        except Exception as e:
            raise SSHConnectionError(
                f"File upload failed: {e}", host=self.host, port=self.port
            )

    def upload_content(self, content: str, remote_path: str) -> bool:
        """
        Upload string content to a remote file.

        Args:
            content: String content to upload
            remote_path: Remote destination path

        Returns:
            True if successful
        """
        self.connect()

        try:
            sftp = self._client.open_sftp()
            with sftp.file(remote_path, "w") as f:
                f.write(content)
            sftp.close()
            logger.debug(f"Uploaded content to {remote_path}")
            return True
        except Exception as e:
            raise SSHConnectionError(
                f"Content upload failed: {e}", host=self.host, port=self.port
            )

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """
        Download a file from the remote host.

        Args:
            remote_path: Remote file path
            local_path: Local destination path

        Returns:
            True if successful
        """
        self.connect()

        try:
            sftp = self._client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            logger.debug(f"Downloaded {remote_path} to {local_path}")
            return True
        except Exception as e:
            raise SSHConnectionError(
                f"File download failed: {e}", host=self.host, port=self.port
            )


@contextmanager
def ssh_connection(
    host,
    username,
    key_path=None,
    key_content=None,
    port=22,
    known_hosts_path=None,
    auto_add_hosts=False,
):
    """
    Context manager for SSH connections.

    Usage:
        with ssh_connection('host', 'user', key_path='/path/to/key') as client:
            result = client.execute('ls')

    Args:
        host: Remote host address
        username: SSH username
        key_path: Path to SSH private key file
        key_content: SSH private key content
        port: SSH port (default: 22)
        known_hosts_path: Path to known_hosts file
        auto_add_hosts: If True, auto-add unknown hosts (use only in trusted networks)
    """
    client = SSHClient(
        host=host,
        username=username,
        key_path=key_path,
        key_content=key_content,
        port=port,
        known_hosts_path=known_hosts_path,
        auto_add_hosts=auto_add_hosts,
    )
    try:
        client.connect()
        yield client
    finally:
        client.disconnect()
