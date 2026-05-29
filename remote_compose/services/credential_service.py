"""
Service for managing secure credentials.
"""

import os
import tempfile
from contextlib import contextmanager
from typing import Optional, Generator

from django.utils import timezone

from ..models import SecureCredential
from ..utils.crypto import encrypt_value, decrypt_value
from ..exceptions import CredentialError, ValidationError
from .base import BaseService


class CredentialService(BaseService):
    """
    Service for managing encrypted credentials.
    """

    def create_ssh_key(
        self,
        name: str,
        key_path: Optional[str] = None,
        key_content: Optional[str] = None,
        description: str = "",
        created_by: str = "",
    ) -> SecureCredential:
        """
        Create a new SSH key credential.

        Args:
            name: Unique name for the credential
            key_path: Path to SSH private key file
            key_content: SSH private key content (alternative to key_path)
            description: Optional description
            created_by: User who created the credential

        Returns:
            SecureCredential instance
        """
        if not key_path and not key_content:
            raise ValidationError("Either key_path or key_content is required")

        if key_path:
            if not os.path.exists(key_path):
                raise ValidationError(f"SSH key file not found: {key_path}")
            with open(key_path, "r") as f:
                key_content = f.read()

        # Validate key format
        if not self._validate_ssh_key(key_content):
            raise ValidationError("Invalid SSH private key format")

        encrypted = encrypt_value(key_content)

        credential = SecureCredential.objects.create(
            name=name,
            credential_type=SecureCredential.CredentialType.SSH_PRIVATE_KEY,
            encrypted_value=encrypted,
            description=description or f"SSH key: {name}",
            created_by=created_by,
        )

        self.log_info(f"Created SSH key credential: {name}")
        self.notify_observers("credential_created", credential=credential)

        return credential

    def create_aws_credential(
        self,
        name: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        description: str = "",
        created_by: str = "",
    ) -> SecureCredential:
        """
        Create a new AWS credential.

        Args:
            name: Unique name for the credential
            access_key_id: AWS Access Key ID
            secret_access_key: AWS Secret Access Key
            region: AWS region
            description: Optional description
            created_by: User who created the credential

        Returns:
            SecureCredential instance
        """
        if not access_key_id or not secret_access_key:
            raise ValidationError(
                "Both access_key_id and secret_access_key are required"
            )

        encrypted = encrypt_value(secret_access_key)

        credential = SecureCredential.objects.create(
            name=name,
            credential_type=SecureCredential.CredentialType.AWS_ACCESS_KEY,
            encrypted_value=encrypted,
            aws_access_key_id=access_key_id,
            aws_region=region,
            description=description or f"AWS credential: {name}",
            created_by=created_by,
        )

        self.log_info(f"Created AWS credential: {name}")
        self.notify_observers("credential_created", credential=credential)

        return credential

    def get_credential(self, credential_id: int) -> SecureCredential:
        """Get a credential by ID."""
        try:
            return SecureCredential.objects.get(id=credential_id)
        except SecureCredential.DoesNotExist:
            raise CredentialError(f"Credential not found: {credential_id}")

    def get_credential_by_name(self, name: str) -> SecureCredential:
        """Get a credential by name."""
        try:
            return SecureCredential.objects.get(name=name)
        except SecureCredential.DoesNotExist:
            raise CredentialError(f"Credential not found: {name}")

    def get_decrypted_value(self, credential: SecureCredential) -> str:
        """
        Get the decrypted value of a credential.

        Also updates last_used_at timestamp.
        """
        credential.mark_used()
        return decrypt_value(credential.encrypted_value)

    def get_ssh_key_content(self, credential: SecureCredential) -> str:
        """
        Get SSH key content from a credential.

        Args:
            credential: SecureCredential instance

        Returns:
            Decrypted SSH key content
        """
        if (
            credential.credential_type
            != SecureCredential.CredentialType.SSH_PRIVATE_KEY
        ):
            raise CredentialError(f"Credential {credential.name} is not an SSH key")
        return self.get_decrypted_value(credential)

    @contextmanager
    def get_ssh_key_file(
        self, credential: SecureCredential
    ) -> Generator[str, None, None]:
        """
        Get SSH key as a temporary file using a context manager.

        The file is automatically cleaned up when the context exits,
        even if an exception occurs.

        Args:
            credential: SecureCredential instance

        Yields:
            Path to temporary key file

        Usage:
            with credential_service.get_ssh_key_file(credential) as key_path:
                # Use key_path for SSH connection
                ssh_client = SSHClient(host, username, key_path=key_path)
        """
        key_content = self.get_ssh_key_content(credential)
        path = None

        try:
            # Create temp file with restricted permissions
            fd, path = tempfile.mkstemp(suffix=".pem", prefix="remote_compose_")
            os.chmod(path, 0o600)

            with os.fdopen(fd, "w") as f:
                f.write(key_content)

            yield path
        finally:
            # Always clean up the temp file
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass  # Best effort cleanup

    def get_aws_credentials(self, credential: SecureCredential) -> dict:
        """
        Get AWS credentials as a dictionary.

        Args:
            credential: SecureCredential instance

        Returns:
            Dict with access_key_id, secret_access_key, and region
        """
        if credential.credential_type != SecureCredential.CredentialType.AWS_ACCESS_KEY:
            raise CredentialError(
                f"Credential {credential.name} is not an AWS credential"
            )

        return {
            "access_key_id": credential.aws_access_key_id,
            "secret_access_key": self.get_decrypted_value(credential),
            "region": credential.aws_region,
        }

    def rotate_credential(
        self,
        credential: SecureCredential,
        new_value: str,
        rotated_by: str = "",
    ) -> SecureCredential:
        """
        Rotate a credential with a new value.

        Args:
            credential: SecureCredential instance to rotate
            new_value: New credential value
            rotated_by: User performing the rotation

        Returns:
            Updated SecureCredential instance
        """
        # Validate new value based on type
        if (
            credential.credential_type
            == SecureCredential.CredentialType.SSH_PRIVATE_KEY
        ):
            if not self._validate_ssh_key(new_value):
                raise ValidationError("Invalid SSH private key format")

        credential.encrypted_value = encrypt_value(new_value)
        credential.last_rotated_at = timezone.now()
        credential.save(
            update_fields=["encrypted_value", "last_rotated_at", "updated_at"]
        )

        self.log_info(f"Rotated credential: {credential.name}", rotated_by=rotated_by)
        self.notify_observers("credential_rotated", credential=credential)

        return credential

    def delete_credential(self, credential: SecureCredential) -> bool:
        """
        Delete a credential.

        Args:
            credential: SecureCredential instance to delete

        Returns:
            True if deleted successfully
        """
        # Check if credential is in use
        if credential.targets.exists():
            raise CredentialError(
                f"Credential {credential.name} is in use by deployment targets. "
                "Remove the targets first or update them to use a different credential."
            )

        name = credential.name
        credential.delete()

        self.log_info(f"Deleted credential: {name}")
        self.notify_observers("credential_deleted", credential_name=name)

        return True

    def list_credentials(
        self,
        credential_type: Optional[str] = None,
    ) -> list:
        """
        List all credentials.

        Args:
            credential_type: Optional filter by type

        Returns:
            QuerySet of SecureCredential instances
        """
        qs = SecureCredential.objects.all()

        if credential_type:
            qs = qs.filter(credential_type=credential_type)

        return qs

    def _validate_ssh_key(self, key_content: str) -> bool:
        """Validate SSH private key format."""
        if not key_content:
            return False

        # Check for common key headers
        valid_headers = [
            "-----BEGIN RSA PRIVATE KEY-----",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "-----BEGIN EC PRIVATE KEY-----",
            "-----BEGIN DSA PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----",
        ]

        return any(header in key_content for header in valid_headers)

    def store_ssh_keypair(
        self,
        name: str,
        private_pem: str,
        public_openssh: str,
        description: str = "",
        created_by: str = "",
    ) -> SecureCredential:
        """Store an SSH keypair (private + public) as a single credential.

        Used by `rc dev` for per-host ed25519 keys. The private key is
        Fernet-encrypted; the public key is bundled in the same encrypted
        blob (JSON-packed) so a single credential row holds both halves.
        """
        import json

        if not self._validate_ssh_key(private_pem):
            raise ValidationError("Invalid SSH private key format")

        bundle = json.dumps({"private": private_pem, "public": public_openssh})
        encrypted = encrypt_value(bundle)

        credential = SecureCredential.objects.create(
            name=name,
            credential_type=SecureCredential.CredentialType.SSH_PRIVATE_KEY,
            encrypted_value=encrypted,
            description=description or f"SSH keypair: {name}",
            created_by=created_by,
        )
        self.log_info(f"Stored SSH keypair: {name}")
        self.notify_observers("credential_created", credential=credential)
        return credential

    def get_ssh_keypair(self, credential: SecureCredential) -> tuple[str, str]:
        """Return (private_pem, public_openssh) from a stored keypair credential."""
        import json

        decrypted = self.get_decrypted_value(credential)
        # backwards-compat: if not JSON, treat as plain private key (no public)
        try:
            bundle = json.loads(decrypted)
            return bundle["private"], bundle.get("public", "")
        except (json.JSONDecodeError, KeyError):
            return decrypted, ""
