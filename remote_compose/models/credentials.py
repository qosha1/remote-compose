"""
Secure credential storage models.
"""

from django.db import models
from .base import TimestampedModel


class SecureCredential(TimestampedModel):
    """
    Stores encrypted credentials (SSH keys, AWS credentials, etc.)
    """

    class CredentialType(models.TextChoices):
        SSH_PRIVATE_KEY = 'ssh_private_key', 'SSH Private Key'
        AWS_ACCESS_KEY = 'aws_access_key', 'AWS Access Key'
        API_TOKEN = 'api_token', 'API Token'

    # Identification
    name = models.CharField(max_length=255, unique=True, db_index=True)
    description = models.TextField(blank=True)

    # Credential details
    credential_type = models.CharField(
        max_length=50,
        choices=CredentialType.choices
    )
    encrypted_value = models.TextField(help_text='Fernet encrypted value')

    # For AWS credentials, store access key ID separately (not sensitive)
    aws_access_key_id = models.CharField(
        max_length=255,
        blank=True,
        help_text='AWS Access Key ID (for AWS credentials only)'
    )
    aws_region = models.CharField(
        max_length=50,
        blank=True,
        help_text='AWS Region (for AWS credentials only)'
    )

    # Tracking
    created_by = models.CharField(max_length=255, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_rotated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'remote_compose_secure_credentials'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.credential_type})"

    def get_value(self):
        """Decrypt and return the credential value."""
        from ..utils.crypto import decrypt_value
        return decrypt_value(self.encrypted_value)

    def set_value(self, plaintext):
        """Encrypt and store the credential value."""
        from ..utils.crypto import encrypt_value
        self.encrypted_value = encrypt_value(plaintext)

    def mark_used(self):
        """Update last_used_at timestamp."""
        from django.utils import timezone
        self.last_used_at = timezone.now()
        self.save(update_fields=['last_used_at', 'updated_at'])

    def rotate(self, new_value):
        """Rotate credential with a new value."""
        from django.utils import timezone
        self.set_value(new_value)
        self.last_rotated_at = timezone.now()
        self.save(update_fields=['encrypted_value', 'last_rotated_at', 'updated_at'])
