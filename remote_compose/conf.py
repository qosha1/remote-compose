"""
Configuration management for remote_compose.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULTS = {
    # SSH Settings
    "SSH_CONNECTION_TIMEOUT": 30,
    "SSH_COMMAND_TIMEOUT": 300,
    "SSH_RETRY_ATTEMPTS": 3,
    "SSH_RETRY_DELAY": 5,
    "SSH_AUTO_ADD_HOSTS": False,  # Auto-add unknown SSH hosts (use only in trusted networks)
    # Deployment Settings
    "DEPLOYMENT_TIMEOUT": 600,
    "MAX_CONCURRENT_DEPLOYMENTS": 5,
    "DEPLOYMENT_LOG_RETENTION_DAYS": 90,
    "ENABLE_ROLLBACK": True,
    # Docker Settings
    "DOCKER_COMPOSE_COMMAND": "docker compose",
    "DOCKER_COMMAND": "docker",
    # AWS Settings
    "AWS_DEFAULT_REGION": "us-east-1",
    "EC2_SYNC_INTERVAL": 3600,
    # Security Settings
    "ENCRYPT_CREDENTIALS": True,
    "MASK_SENSITIVE_LOGS": True,
    # Rate Limiting Settings
    "RATE_LIMIT_ENABLED": True,
    "RATE_LIMIT_DEPLOYMENTS_PER_MINUTE": 10,  # Global limit
    "RATE_LIMIT_DEPLOYMENTS_PER_TARGET": 5,  # Per target limit
    "RATE_LIMIT_DEPLOYMENTS_PER_USER": 20,  # Per user limit (5 min window)
    "RATE_LIMIT_ROLLBACKS_PER_MINUTE": 5,  # Rollback limit per target
    # Audit Logging Settings
    "AUDIT_LOG_ENABLED": True,
    "AUDIT_LOG_TO_DATABASE": True,
    "AUDIT_LOG_FILE": None,  # Optional file path for audit logs
    "AUDIT_LOG_RETENTION_DAYS": 365,
    # Notification Settings
    "NOTIFICATION_CHANNELS": [],  # ['webhook', 'slack', 'email']
    "NOTIFICATION_WEBHOOK_URLS": [],  # List of webhook URLs
    "SLACK_WEBHOOK_URL": None,
    "WEBHOOK_ALLOW_ALL_DOMAINS": False,  # Allow webhooks to any domain
    "WEBHOOK_ALLOWED_DOMAINS": set(),  # Set of allowed webhook domains
    # Health Check Settings
    "HEALTH_CHECK_INTERVAL": 300,  # 5 minutes
    "HEALTH_CHECK_TIMEOUT": 30,
    "HEALTH_CHECK_ENABLED": True,
    # Orchestration Settings
    "ORCHESTRATION_MAX_PARALLEL": 5,  # Max parallel deployments
    "ORCHESTRATION_BATCH_SIZE": 2,  # Default batch size for rolling deploys
    # Celery Settings (for async tasks)
    "CELERY_TASK_QUEUE": "remote_compose",
    "CELERY_TASK_RETRY_DELAY": 30,
    "CELERY_TASK_MAX_RETRIES": 3,
    # Hooks
    "PRE_DEPLOY_HOOK": None,
    "POST_DEPLOY_HOOK": None,
    "ON_FAILURE_HOOK": None,
}

REQUIRED_SETTINGS = []  # ENCRYPTION_KEY is only required if ENCRYPT_CREDENTIALS is True


def get_settings():
    """Get remote_compose settings with defaults applied."""
    user_settings = getattr(settings, "REMOTE_COMPOSE", {})
    merged = {**DEFAULTS, **user_settings}
    return merged


def get_setting(name, default=None):
    """Get a single setting value."""
    all_settings = get_settings()
    return all_settings.get(name, default)


def validate_settings():
    """Validate that all required settings are present and valid."""
    config = get_settings()

    # Check required settings
    for setting in REQUIRED_SETTINGS:
        if setting not in config or config[setting] is None:
            raise ImproperlyConfigured(
                f"REMOTE_COMPOSE['{setting}'] is required but not set."
            )

    # Validate encryption key if encryption is enabled
    if config.get("ENCRYPT_CREDENTIALS", True):
        encryption_key = config.get("ENCRYPTION_KEY")
        if encryption_key:
            # Validate key format (should be 32 url-safe base64-encoded bytes for Fernet)
            try:
                from cryptography.fernet import Fernet

                Fernet(
                    encryption_key.encode()
                    if isinstance(encryption_key, str)
                    else encryption_key
                )
            except Exception as e:
                raise ImproperlyConfigured(
                    f"REMOTE_COMPOSE['ENCRYPTION_KEY'] is not a valid Fernet key: {e}"
                )

    # Validate timeout values
    timeout_settings = [
        "SSH_CONNECTION_TIMEOUT",
        "SSH_COMMAND_TIMEOUT",
        "DEPLOYMENT_TIMEOUT",
    ]
    for setting in timeout_settings:
        value = config.get(setting)
        if value is not None and (not isinstance(value, int) or value <= 0):
            raise ImproperlyConfigured(
                f"REMOTE_COMPOSE['{setting}'] must be a positive integer."
            )

    return True
