"""
Cryptographic utilities for secure credential storage.
"""

import base64
import logging

from django.conf import settings
from cryptography.fernet import Fernet, InvalidToken

from ..exceptions import EncryptionError

logger = logging.getLogger(__name__)


def _get_fernet_key():
    """
    Get a Fernet key from Django settings.

    IMPORTANT: REMOTE_COMPOSE['ENCRYPTION_KEY'] is REQUIRED for production use.
    The key must be a valid Fernet key (URL-safe base64-encoded 32-byte key).

    Generate a key using: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    Raises:
        EncryptionError: If ENCRYPTION_KEY is not configured.
    """
    remote_compose_settings = getattr(settings, 'REMOTE_COMPOSE', {})
    encryption_key = remote_compose_settings.get('ENCRYPTION_KEY')

    if not encryption_key:
        raise EncryptionError(
            "REMOTE_COMPOSE['ENCRYPTION_KEY'] is required for credential encryption. "
            "Generate a key using: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    if isinstance(encryption_key, str):
        encryption_key = encryption_key.encode()

    return encryption_key


def _get_fernet():
    """Get a Fernet instance."""
    try:
        key = _get_fernet_key()
        return Fernet(key)
    except Exception as e:
        raise EncryptionError(f"Failed to initialize encryption: {e}")


def encrypt_value(plaintext):
    """
    Encrypt a plaintext string using Fernet symmetric encryption.

    Args:
        plaintext: String to encrypt

    Returns:
        Base64-encoded encrypted string
    """
    if not plaintext:
        return ''

    try:
        fernet = _get_fernet()
        if isinstance(plaintext, str):
            plaintext = plaintext.encode()
        encrypted = fernet.encrypt(plaintext)
        return base64.b64encode(encrypted).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise EncryptionError(f"Failed to encrypt value: {e}")


def decrypt_value(ciphertext):
    """
    Decrypt a Fernet-encrypted string.

    Args:
        ciphertext: Base64-encoded encrypted string

    Returns:
        Decrypted plaintext string
    """
    if not ciphertext:
        return ''

    try:
        fernet = _get_fernet()
        encrypted = base64.b64decode(ciphertext.encode())
        decrypted = fernet.decrypt(encrypted)
        return decrypted.decode()
    except InvalidToken:
        raise EncryptionError(
            "Failed to decrypt value: Invalid token. "
            "The encryption key may have changed."
        )
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        raise EncryptionError(f"Failed to decrypt value: {e}")


def generate_fernet_key():
    """
    Generate a new Fernet key.

    Returns:
        URL-safe base64-encoded 32-byte key
    """
    return Fernet.generate_key().decode()


def rotate_encryption_key(old_key, new_key, encrypted_values):
    """
    Re-encrypt values with a new key.

    Args:
        old_key: The current encryption key
        new_key: The new encryption key
        encrypted_values: List of encrypted values to re-encrypt

    Returns:
        List of re-encrypted values
    """
    old_fernet = Fernet(old_key.encode() if isinstance(old_key, str) else old_key)
    new_fernet = Fernet(new_key.encode() if isinstance(new_key, str) else new_key)

    re_encrypted = []
    for ciphertext in encrypted_values:
        if not ciphertext:
            re_encrypted.append('')
            continue

        try:
            encrypted = base64.b64decode(ciphertext.encode())
            decrypted = old_fernet.decrypt(encrypted)
            new_encrypted = new_fernet.encrypt(decrypted)
            re_encrypted.append(base64.b64encode(new_encrypted).decode())
        except Exception as e:
            raise EncryptionError(f"Failed to rotate encryption for value: {e}")

    return re_encrypted
