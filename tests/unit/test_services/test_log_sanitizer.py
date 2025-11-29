"""
Unit tests for LogSanitizer.
"""

import pytest

from remote_compose.services import LogSanitizer, sanitize_for_logging


class TestLogSanitizer:
    """Tests for the LogSanitizer."""

    @pytest.fixture
    def sanitizer(self):
        return LogSanitizer()

    def test_sanitize_sensitive_field_names(self, sanitizer):
        """Test that sensitive field names are masked."""
        data = {
            'username': 'john',
            'password': 'secret123',
            'api_key': 'abc123xyz',
            'token': 'mytoken',
            'host': 'localhost',
        }

        result = sanitizer.sanitize_dict(data)

        assert result['username'] == 'john'
        assert result['password'] == '***'
        assert result['api_key'] == '***'
        assert result['token'] == '***'
        assert result['host'] == 'localhost'

    def test_sanitize_nested_dicts(self, sanitizer):
        """Test sanitization of nested dictionaries."""
        data = {
            'database': {
                'host': 'localhost',
                'password': 'dbpass',
            },
            'api_key': 'nested_key',
        }

        result = sanitizer.sanitize_dict(data)

        assert result['database']['host'] == 'localhost'
        assert result['database']['password'] == '***'
        assert result['api_key'] == '***'

    def test_sanitize_ssh_key(self, sanitizer):
        """Test that SSH private keys are redacted."""
        text = """
        Here is my key:
        -----BEGIN RSA PRIVATE KEY-----
        MIIEowIBAAKCAQEA...
        -----END RSA PRIVATE KEY-----
        And some more text.
        """

        result = sanitizer.sanitize_string(text)

        assert '-----BEGIN RSA PRIVATE KEY-----' not in result
        assert '[REDACTED SSH KEY]' in result
        assert 'And some more text' in result

    def test_sanitize_bearer_token(self, sanitizer):
        """Test that Bearer tokens are redacted."""
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"

        result = sanitizer.sanitize_string(text)

        assert 'eyJ' not in result
        assert 'Bearer [REDACTED]' in result

    def test_sanitize_password_in_connection_string(self, sanitizer):
        """Test that passwords in connection strings are redacted."""
        text = "mysql://user:password=secretpass&host=localhost"

        result = sanitizer.sanitize_string(text)

        assert 'secretpass' not in result
        assert '[REDACTED]' in result

    def test_sanitize_url_with_credentials(self, sanitizer):
        """Test that credentials in URLs are redacted."""
        text = "https://user:password123@example.com/api"

        result = sanitizer.sanitize_string(text)

        assert 'password123' not in result
        assert '[REDACTED]' in result

    def test_sanitize_jwt_token(self, sanitizer):
        """Test that JWT tokens are redacted."""
        text = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"

        result = sanitizer.sanitize_string(text)

        assert 'eyJhbGciOi' not in result
        assert '[REDACTED JWT]' in result

    def test_sanitize_aws_access_key(self, sanitizer):
        """Test that AWS access key IDs are redacted."""
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"

        result = sanitizer.sanitize_string(text)

        assert 'AKIAIOSFODNN7EXAMPLE' not in result

    def test_sanitize_list_values(self, sanitizer):
        """Test sanitization of lists."""
        data = [
            {'password': 'secret1'},
            {'password': 'secret2'},
            'normal string',
        ]

        result = sanitizer.sanitize(data)

        assert result[0]['password'] == '***'
        assert result[1]['password'] == '***'
        assert result[2] == 'normal string'

    def test_sanitize_empty_values(self, sanitizer):
        """Test handling of empty values."""
        data = {
            'password': '',
            'token': None,
        }

        result = sanitizer.sanitize_dict(data)

        # Empty values should remain empty
        assert result['password'] == ''
        assert result['token'] is None

    def test_disabled_sanitizer(self):
        """Test that disabled sanitizer passes through values."""
        sanitizer = LogSanitizer(enabled=False)
        data = {'password': 'secret'}

        result = sanitizer.sanitize_dict(data)

        assert result['password'] == 'secret'

    def test_add_sensitive_field(self, sanitizer):
        """Test adding custom sensitive field."""
        sanitizer.add_sensitive_field('custom_secret')

        data = {'custom_secret': 'myvalue'}
        result = sanitizer.sanitize_dict(data)

        assert result['custom_secret'] == '***'

    def test_add_custom_pattern(self, sanitizer):
        """Test adding custom pattern."""
        sanitizer.add_pattern(r'CUSTOM_\d{4}', '[REDACTED_CUSTOM]')

        text = "Code: CUSTOM_1234"
        result = sanitizer.sanitize_string(text)

        assert 'CUSTOM_1234' not in result
        assert '[REDACTED_CUSTOM]' in result

    def test_partial_field_match(self, sanitizer):
        """Test that partial field name matches work."""
        data = {
            'db_password_prod': 'secret',
            'my_api_key_v2': 'key123',
            'auth_token_refresh': 'token456',
        }

        result = sanitizer.sanitize_dict(data)

        assert result['db_password_prod'] == '***'
        assert result['my_api_key_v2'] == '***'
        assert result['auth_token_refresh'] == '***'


class TestSanitizeForLogging:
    """Tests for the convenience function."""

    def test_sanitize_for_logging_dict(self):
        """Test convenience function with dict."""
        data = {'password': 'secret', 'name': 'test'}

        result = sanitize_for_logging(data)

        assert result['password'] == '***'
        assert result['name'] == 'test'

    def test_sanitize_for_logging_string(self):
        """Test convenience function with string."""
        text = "password=secret123"

        result = sanitize_for_logging(text)

        assert 'secret123' not in result

    def test_sanitize_for_logging_non_sensitive(self):
        """Test that non-sensitive data passes through."""
        data = {'host': 'localhost', 'port': 5432}

        result = sanitize_for_logging(data)

        assert result == data
