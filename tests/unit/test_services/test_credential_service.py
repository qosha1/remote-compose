"""
Unit tests for CredentialService.
"""

import pytest
from unittest.mock import patch, MagicMock
import tempfile
import os

from remote_compose.services import CredentialService
from remote_compose.models import SecureCredential
from remote_compose.exceptions import CredentialError, ValidationError


@pytest.mark.django_db
class TestCredentialService:

    @pytest.fixture
    def service(self):
        return CredentialService()

    @pytest.fixture
    def valid_ssh_key(self):
        return """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AKk8KnME0iFLHFEP0mXn
FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake
-----END RSA PRIVATE KEY-----"""

    def test_create_ssh_key_from_content(self, service, valid_ssh_key):
        """Test creating SSH key credential from content."""
        credential = service.create_ssh_key(
            name='test-key',
            key_content=valid_ssh_key,
            description='Test SSH key',
            created_by='test-user',
        )

        assert credential.id is not None
        assert credential.name == 'test-key'
        assert credential.credential_type == SecureCredential.CredentialType.SSH_PRIVATE_KEY
        assert credential.encrypted_value != valid_ssh_key  # Should be encrypted

    def test_create_ssh_key_from_file(self, service, valid_ssh_key):
        """Test creating SSH key credential from file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
            f.write(valid_ssh_key)
            key_path = f.name

        try:
            credential = service.create_ssh_key(
                name='test-key-file',
                key_path=key_path,
            )

            assert credential.id is not None
            assert credential.name == 'test-key-file'
        finally:
            os.unlink(key_path)

    def test_create_ssh_key_invalid_format(self, service):
        """Test creating SSH key with invalid format fails."""
        with pytest.raises(ValidationError):
            service.create_ssh_key(
                name='invalid-key',
                key_content='not a valid key',
            )

    def test_create_aws_credential(self, service):
        """Test creating AWS credential."""
        credential = service.create_aws_credential(
            name='aws-test',
            access_key_id='AKIAIOSFODNN7EXAMPLE',
            secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            region='us-west-2',
        )

        assert credential.id is not None
        assert credential.name == 'aws-test'
        assert credential.credential_type == SecureCredential.CredentialType.AWS_ACCESS_KEY
        assert credential.aws_access_key_id == 'AKIAIOSFODNN7EXAMPLE'
        assert credential.aws_region == 'us-west-2'

    def test_get_decrypted_value(self, service, valid_ssh_key):
        """Test decrypting credential value."""
        credential = service.create_ssh_key(
            name='decrypt-test',
            key_content=valid_ssh_key,
        )

        decrypted = service.get_decrypted_value(credential)

        assert decrypted == valid_ssh_key

    def test_get_ssh_key_content(self, service, valid_ssh_key):
        """Test getting SSH key content."""
        credential = service.create_ssh_key(
            name='ssh-content-test',
            key_content=valid_ssh_key,
        )

        content = service.get_ssh_key_content(credential)

        assert content == valid_ssh_key

    def test_get_ssh_key_content_wrong_type(self, service):
        """Test getting SSH key content from non-SSH credential fails."""
        credential = service.create_aws_credential(
            name='aws-wrong-type',
            access_key_id='AKIATEST',
            secret_access_key='secret',
        )

        with pytest.raises(CredentialError):
            service.get_ssh_key_content(credential)

    def test_get_aws_credentials(self, service):
        """Test getting AWS credentials as dictionary."""
        credential = service.create_aws_credential(
            name='aws-dict-test',
            access_key_id='AKIATEST123',
            secret_access_key='supersecret',
            region='eu-west-1',
        )

        aws_creds = service.get_aws_credentials(credential)

        assert aws_creds['access_key_id'] == 'AKIATEST123'
        assert aws_creds['secret_access_key'] == 'supersecret'
        assert aws_creds['region'] == 'eu-west-1'

    def test_rotate_credential(self, service, valid_ssh_key):
        """Test rotating credential."""
        credential = service.create_ssh_key(
            name='rotate-test',
            key_content=valid_ssh_key,
        )
        original_encrypted = credential.encrypted_value

        new_key = valid_ssh_key.replace('Fake', 'NewKey')
        service.rotate_credential(credential, new_key)
        credential.refresh_from_db()

        assert credential.encrypted_value != original_encrypted
        assert credential.last_rotated_at is not None

    def test_delete_credential(self, service, valid_ssh_key):
        """Test deleting credential."""
        credential = service.create_ssh_key(
            name='delete-test',
            key_content=valid_ssh_key,
        )
        credential_id = credential.id

        result = service.delete_credential(credential)

        assert result is True
        assert not SecureCredential.objects.filter(id=credential_id).exists()

    def test_list_credentials(self, service, valid_ssh_key):
        """Test listing credentials."""
        service.create_ssh_key(name='list-ssh-1', key_content=valid_ssh_key)
        service.create_ssh_key(name='list-ssh-2', key_content=valid_ssh_key)
        service.create_aws_credential(
            name='list-aws-1',
            access_key_id='AKIA1',
            secret_access_key='secret1',
        )

        all_creds = service.list_credentials()
        ssh_creds = service.list_credentials(
            credential_type=SecureCredential.CredentialType.SSH_PRIVATE_KEY
        )
        aws_creds = service.list_credentials(
            credential_type=SecureCredential.CredentialType.AWS_ACCESS_KEY
        )

        assert all_creds.count() >= 3
        assert ssh_creds.count() >= 2
        assert aws_creds.count() >= 1
