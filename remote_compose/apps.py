from django.apps import AppConfig


class RemoteComposeConfig(AppConfig):
    """Django app configuration for remote_compose."""

    name = 'remote_compose'
    verbose_name = 'Remote Compose'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        """Validate configuration on app startup."""
        from .conf import get_settings, validate_settings
        try:
            validate_settings()
        except Exception:
            pass  # Allow app to load even with incomplete settings
