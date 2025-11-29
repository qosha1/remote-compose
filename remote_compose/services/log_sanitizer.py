"""
Log sanitization for removing sensitive data from logs.
"""

import re
import logging
from typing import Any, Dict, List, Optional, Pattern, Set

from ..conf import get_setting

logger = logging.getLogger(__name__)


class LogSanitizer:
    """
    Sanitizes sensitive data from logs and messages.

    Handles:
    - Passwords and secrets
    - API keys and tokens
    - SSH private keys
    - Credit card numbers
    - Personal identifiable information
    - Custom patterns
    """

    # Default sensitive field names (case-insensitive)
    DEFAULT_SENSITIVE_FIELDS: Set[str] = frozenset({
        'password',
        'passwd',
        'pwd',
        'secret',
        'token',
        'api_key',
        'apikey',
        'api-key',
        'auth_token',
        'access_token',
        'refresh_token',
        'bearer',
        'credential',
        'private_key',
        'privatekey',
        'ssh_key',
        'sshkey',
        'aws_secret',
        'aws_secret_access_key',
        'encryption_key',
        'db_password',
        'database_password',
        'mysql_password',
        'postgres_password',
        'redis_password',
        'auth',
        'authorization',
    })

    # Regex patterns to detect and mask sensitive data in strings.
    # Each tuple is (pattern, replacement).
    # Patterns are applied in order - more specific patterns should come first.
    DEFAULT_PATTERNS: List[tuple] = [
        # SSH private keys - matches PEM-formatted private keys
        # Handles RSA, EC, DSA, and OpenSSH key formats
        # Uses [\s\S]*? for non-greedy match across newlines
        (r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----',
         '[REDACTED SSH KEY]'),

        # AWS Access Key ID - starts with specific prefixes followed by 16 alphanumeric chars
        # AKIA = long-term access key, ASIA = temporary STS credentials
        # A3T, AGPA, AIDA, AROA, AIPA, ANPA, ANVA are other AWS credential types
        (r'(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}',
         '[REDACTED AWS KEY ID]'),

        # AWS Secret Key - 40 character base64-encoded string
        # Uses negative lookbehind/ahead to avoid matching within longer strings
        (r'(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])',
         '[REDACTED AWS SECRET]'),

        # Generic API keys - matches common key assignment patterns
        # Handles api_key=xxx, apikey: "xxx", api-key='xxx' etc.
        (r'(?:api[_-]?key|apikey|api_token)["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-]{20,})["\']?',
         r'api_key=[REDACTED]'),

        # Bearer tokens in Authorization headers
        # Matches "Bearer <token>" pattern used in OAuth2/JWT auth
        (r'[Bb]earer\s+[A-Za-z0-9\-_\.]+',
         'Bearer [REDACTED]'),

        # Basic auth credentials embedded in URLs
        # Matches https://user:password@host format, preserves URL structure
        (r'(https?://)([^:]+):([^@]+)@',
         r'\1[REDACTED]:[REDACTED]@'),

        # Password in connection strings and config
        # Matches password=xxx, passwd: "xxx", pwd='xxx' patterns
        (r'(password|passwd|pwd)\s*[=:]\s*["\']?[^"\'\s&;]+["\']?',
         r'\1=[REDACTED]'),

        # Credit card numbers - basic pattern for 13-16 digit numbers
        # Allows spaces or hyphens between digit groups
        # Note: May have false positives on other long numbers
        (r'\b(?:\d[ -]*?){13,16}\b',
         '[REDACTED CARD]'),

        # US Social Security Numbers - XXX-XX-XXXX format
        # Allows hyphens or spaces as separators
        (r'\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b',
         '[REDACTED SSN]'),

        # Email addresses (commented out - may be too aggressive for some use cases)
        # Uncomment if you want to redact email addresses
        # (r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        #  '[REDACTED EMAIL]'),

        # JWT tokens - three base64url-encoded segments separated by dots
        # Header starts with eyJ (base64 of '{"'), payload also starts with eyJ
        (r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',
         '[REDACTED JWT]'),

        # GitHub personal access tokens and other GitHub tokens
        # gh[p|o|u|s|r]_ prefix identifies token type (personal, oauth, user-to-server, etc.)
        (r'gh[pousr]_[A-Za-z0-9_]{36,}',
         '[REDACTED GITHUB TOKEN]'),

        # Slack API tokens - xoxb (bot), xoxp (user), xoxa (app), xoxr (refresh), xoxs (legacy)
        (r'xox[baprs]-[0-9]{10,}-[A-Za-z0-9]+',
         '[REDACTED SLACK TOKEN]'),
    ]

    def __init__(
        self,
        sensitive_fields: Optional[Set[str]] = None,
        additional_patterns: Optional[List[tuple]] = None,
        mask_string: str = '***',
        enabled: bool = True,
    ):
        """
        Initialize log sanitizer.

        Args:
            sensitive_fields: Set of field names to consider sensitive
            additional_patterns: Additional regex patterns to match and mask
            mask_string: String to use for masking simple values
            enabled: Whether sanitization is enabled
        """
        self.sensitive_fields = sensitive_fields or self.DEFAULT_SENSITIVE_FIELDS
        self.mask_string = mask_string
        self.enabled = enabled and get_setting('MASK_SENSITIVE_LOGS', True)

        # Compile patterns
        self.patterns: List[tuple] = []
        for pattern, replacement in self.DEFAULT_PATTERNS:
            try:
                self.patterns.append((re.compile(pattern, re.IGNORECASE), replacement))
            except re.error as e:
                logger.warning(f"Invalid sanitization pattern: {pattern}, error: {e}")

        if additional_patterns:
            for pattern, replacement in additional_patterns:
                try:
                    self.patterns.append((re.compile(pattern, re.IGNORECASE), replacement))
                except re.error as e:
                    logger.warning(f"Invalid additional pattern: {pattern}, error: {e}")

    def sanitize(self, value: Any) -> Any:
        """
        Sanitize a value, recursively handling dicts and lists.

        Args:
            value: Value to sanitize

        Returns:
            Sanitized value
        """
        if not self.enabled:
            return value

        if isinstance(value, dict):
            return self.sanitize_dict(value)
        elif isinstance(value, list):
            return [self.sanitize(item) for item in value]
        elif isinstance(value, str):
            return self.sanitize_string(value)
        else:
            return value

    def sanitize_dict(
        self,
        data: Dict[str, Any],
        parent_key: str = '',
    ) -> Dict[str, Any]:
        """
        Sanitize a dictionary, masking sensitive fields.

        Args:
            data: Dictionary to sanitize
            parent_key: Parent key for nested dicts

        Returns:
            Sanitized dictionary
        """
        if not self.enabled:
            return data

        sanitized = {}

        for key, value in data.items():
            full_key = f"{parent_key}.{key}" if parent_key else key

            if self._is_sensitive_key(key):
                # Mask the entire value
                if value:
                    sanitized[key] = self.mask_string
                else:
                    sanitized[key] = value
            elif isinstance(value, dict):
                sanitized[key] = self.sanitize_dict(value, full_key)
            elif isinstance(value, list):
                sanitized[key] = [self.sanitize(item) for item in value]
            elif isinstance(value, str):
                sanitized[key] = self.sanitize_string(value)
            else:
                sanitized[key] = value

        return sanitized

    def sanitize_string(self, text: str) -> str:
        """
        Sanitize a string by applying all patterns.

        Args:
            text: String to sanitize

        Returns:
            Sanitized string
        """
        if not self.enabled or not text:
            return text

        result = text

        for pattern, replacement in self.patterns:
            try:
                result = pattern.sub(replacement, result)
            except Exception as e:
                logger.debug(f"Pattern substitution failed: {e}")

        return result

    def _is_sensitive_key(self, key: str) -> bool:
        """Check if a key name indicates sensitive data."""
        key_lower = key.lower().replace('-', '_')

        # Direct match
        if key_lower in self.sensitive_fields:
            return True

        # Partial match
        for sensitive in self.sensitive_fields:
            if sensitive in key_lower:
                return True

        return False

    def add_sensitive_field(self, field_name: str) -> None:
        """Add a field name to the sensitive fields set."""
        self.sensitive_fields = self.sensitive_fields | {field_name.lower()}

    def add_pattern(self, pattern: str, replacement: str) -> bool:
        """
        Add a custom pattern for sanitization.

        Args:
            pattern: Regex pattern to match
            replacement: Replacement string

        Returns:
            True if pattern was added successfully
        """
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
            self.patterns.append((compiled, replacement))
            return True
        except re.error as e:
            logger.error(f"Invalid pattern: {pattern}, error: {e}")
            return False


class SanitizingLogHandler(logging.Handler):
    """
    Log handler that sanitizes log records before emitting.
    """

    def __init__(self, base_handler: logging.Handler, sanitizer: Optional[LogSanitizer] = None):
        """
        Initialize sanitizing handler.

        Args:
            base_handler: Handler to wrap
            sanitizer: LogSanitizer instance
        """
        super().__init__()
        self.base_handler = base_handler
        self.sanitizer = sanitizer or LogSanitizer()

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a sanitized log record."""
        # Sanitize the message
        if record.msg:
            if isinstance(record.msg, str):
                record.msg = self.sanitizer.sanitize_string(record.msg)

        # Sanitize args
        if record.args:
            if isinstance(record.args, dict):
                record.args = self.sanitizer.sanitize_dict(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self.sanitizer.sanitize(arg) for arg in record.args
                )

        # Pass to base handler
        self.base_handler.emit(record)

    def setLevel(self, level):
        super().setLevel(level)
        self.base_handler.setLevel(level)

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        self.base_handler.setFormatter(fmt)


def setup_sanitized_logging(logger_name: str = 'remote_compose') -> None:
    """
    Configure a logger to use sanitized handlers.

    Args:
        logger_name: Name of logger to configure
    """
    target_logger = logging.getLogger(logger_name)
    sanitizer = LogSanitizer()

    # Wrap existing handlers
    new_handlers = []
    for handler in target_logger.handlers[:]:
        sanitized_handler = SanitizingLogHandler(handler, sanitizer)
        target_logger.removeHandler(handler)
        new_handlers.append(sanitized_handler)

    for handler in new_handlers:
        target_logger.addHandler(handler)

    logger.info(f"Configured sanitized logging for {logger_name}")


def sanitize_for_logging(data: Any) -> Any:
    """
    Convenience function to sanitize data for logging.

    Args:
        data: Data to sanitize

    Returns:
        Sanitized data
    """
    sanitizer = LogSanitizer()
    return sanitizer.sanitize(data)
