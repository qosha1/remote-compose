"""
Utility functions for remote_compose.
"""

import re

from .crypto import encrypt_value, decrypt_value
from .ssh import SSHClient
from .polling import poll_until


def sanitize_name(name: str) -> str:
    """
    Sanitize a name for use in AWS resource names (ECS, ECR, etc.).

    Replaces invalid characters with hyphens, collapses consecutive hyphens,
    strips leading/trailing hyphens, lowercases, and truncates to 128 chars.
    Safe for ECR repositories (which are case-sensitive and lowercase).

    Args:
        name: Original name

    Returns:
        Sanitized name
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
    sanitized = re.sub(r"-+", "-", sanitized)
    sanitized = sanitized.strip("-")
    sanitized = sanitized.lower()
    return sanitized[:128]


__all__ = [
    "encrypt_value",
    "decrypt_value",
    "SSHClient",
    "poll_until",
    "sanitize_name",
]
