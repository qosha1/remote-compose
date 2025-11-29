"""
Django settings for tests.
"""

import os

SECRET_KEY = 'test-secret-key-for-testing-only-do-not-use-in-production'

DEBUG = True

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'remote_compose',
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

USE_TZ = True

REMOTE_COMPOSE = {
    'SSH_CONNECTION_TIMEOUT': 10,
    'SSH_COMMAND_TIMEOUT': 30,
    'DEPLOYMENT_TIMEOUT': 60,
    'MAX_CONCURRENT_DEPLOYMENTS': 2,
    # Test encryption key - valid Fernet key for testing only
    # Generated with: from cryptography.fernet import Fernet; Fernet.generate_key()
    # DO NOT use this key in production!
    'ENCRYPTION_KEY': '1CmzEroNx-g2mQxeYrdSuZEO9BOLXiQTYoWlmlu-Mp0=',
    # Allow auto-adding hosts in tests (mocked anyway)
    'SSH_AUTO_ADD_HOSTS': True,
}
