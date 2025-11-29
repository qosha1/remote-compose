"""
AWS Client Factory for centralized boto3 client management.

Provides a single point of creation for all AWS service clients with:
- Credential management integration
- Client caching for performance
- Consistent error handling
- Region management
"""

from typing import Optional, Dict, Any, Set
from threading import Lock
import time

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from ..models import SecureCredential
from ..conf import get_setting
from ..exceptions import AWSCredentialError, AWSError
from .base import BaseService
from .credential_service import CredentialService


class AWSClientFactory(BaseService):
    """
    Factory for creating and caching AWS service clients.

    Centralizes all AWS client creation to ensure consistent credential
    handling, caching, and error management across the application.

    Usage:
        factory = AWSClientFactory()

        # Get a client with default/environment credentials
        ecs = factory.get_client('ecs', region='us-east-1')

        # Get a client with stored credentials
        ecr = factory.get_client('ecr', region='us-east-1', credential=aws_credential)

        # Get multiple clients efficiently (cached)
        ec2 = factory.get_client('ec2', region='us-east-1')
        efs = factory.get_client('efs', region='us-east-1')
    """

    # Supported AWS services
    SUPPORTED_SERVICES: Set[str] = {
        'ecs',      # Elastic Container Service
        'ecr',      # Elastic Container Registry
        'efs',      # Elastic File System
        'ec2',      # Elastic Compute Cloud (for VPC, subnets, security groups)
        'iam',      # Identity and Access Management
        'sts',      # Security Token Service
        'logs',     # CloudWatch Logs
        'cloudwatch',  # CloudWatch Metrics
        's3',       # Simple Storage Service
        'ssm',      # Systems Manager (for Parameter Store)
        'elbv2',    # Elastic Load Balancing v2 (ALB/NLB)
        'secretsmanager',  # Secrets Manager
        'servicediscovery',  # Cloud Map (Service Connect)
    }

    # Client cache TTL (clients are refreshed after this duration)
    CACHE_TTL_SECONDS = 3600  # 1 hour

    def __init__(
        self,
        credential_service: Optional[CredentialService] = None,
        default_region: Optional[str] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.default_region = default_region or get_setting('AWS_DEFAULT_REGION', 'us-east-1')

        # Thread-safe client cache
        self._client_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = Lock()

    def _get_cache_key(
        self,
        service: str,
        region: str,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """Generate a unique cache key for client lookup."""
        credential_id = credential.id if credential else 'default'
        return f"{service}:{region}:{credential_id}"

    def _is_cache_valid(self, cache_entry: Dict[str, Any]) -> bool:
        """Check if a cached client is still valid."""
        if not cache_entry:
            return False
        created_at = cache_entry.get('created_at', 0)
        return (time.time() - created_at) < self.CACHE_TTL_SECONDS

    def get_client(
        self,
        service: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        force_new: bool = False,
    ):
        """
        Get a boto3 client for the specified AWS service.

        Args:
            service: AWS service name (e.g., 'ecs', 'ecr', 'efs')
            region: AWS region (defaults to configured default region)
            credential: SecureCredential with AWS access keys (optional)
            force_new: If True, bypass cache and create a new client

        Returns:
            boto3 service client

        Raises:
            AWSCredentialError: If credentials are invalid or missing
            AWSError: If client creation fails
            ValueError: If service is not supported
        """
        if service not in self.SUPPORTED_SERVICES:
            raise ValueError(
                f"Unsupported AWS service: {service}. "
                f"Supported services: {', '.join(sorted(self.SUPPORTED_SERVICES))}"
            )

        region = region or self.default_region
        cache_key = self._get_cache_key(service, region, credential)

        # Check cache first (unless force_new)
        if not force_new:
            with self._cache_lock:
                cache_entry = self._client_cache.get(cache_key)
                if cache_entry and self._is_cache_valid(cache_entry):
                    self.log_debug(f"Using cached {service} client for region {region}")
                    return cache_entry['client']

        # Create new client
        client = self._create_client(service, region, credential)

        # Cache the client
        with self._cache_lock:
            self._client_cache[cache_key] = {
                'client': client,
                'created_at': time.time(),
            }

        return client

    def _create_client(
        self,
        service: str,
        region: str,
        credential: Optional[SecureCredential] = None,
    ):
        """Create a new boto3 client."""
        try:
            if credential and credential.credential_type == SecureCredential.CredentialType.AWS_ACCESS_KEY:
                # Use stored AWS credentials
                aws_creds = self.credential_service.get_aws_credentials(credential)
                self.log_debug(f"Creating {service} client with stored credentials")
                return boto3.client(
                    service,
                    region_name=region,
                    aws_access_key_id=aws_creds['access_key_id'],
                    aws_secret_access_key=aws_creds['secret_access_key'],
                )
            else:
                # Use default credential chain (env vars, IAM role, etc.)
                self.log_debug(f"Creating {service} client with default credentials")
                return boto3.client(service, region_name=region)

        except NoCredentialsError:
            raise AWSCredentialError(
                f"No AWS credentials found for {service} client. "
                "Configure credentials via environment variables, AWS credentials file, "
                "IAM role, or create an AWS credential in remote_compose."
            )
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            if error_code in ('InvalidClientTokenId', 'SignatureDoesNotMatch', 'AccessDenied'):
                raise AWSCredentialError(f"Invalid AWS credentials: {e}")
            raise AWSError(f"Failed to create {service} client: {e}")
        except Exception as e:
            raise AWSError(f"Unexpected error creating {service} client: {e}")

    def clear_cache(self, service: Optional[str] = None, region: Optional[str] = None):
        """
        Clear cached clients.

        Args:
            service: If specified, only clear clients for this service
            region: If specified, only clear clients for this region
        """
        with self._cache_lock:
            if service is None and region is None:
                # Clear all
                count = len(self._client_cache)
                self._client_cache.clear()
                self.log_info(f"Cleared all {count} cached AWS clients")
            else:
                # Selective clear
                keys_to_remove = []
                for key in self._client_cache.keys():
                    parts = key.split(':')
                    if len(parts) >= 2:
                        key_service, key_region = parts[0], parts[1]
                        if (service is None or key_service == service) and \
                           (region is None or key_region == region):
                            keys_to_remove.append(key)

                for key in keys_to_remove:
                    del self._client_cache[key]

                if keys_to_remove:
                    self.log_info(f"Cleared {len(keys_to_remove)} cached AWS clients")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get statistics about the client cache."""
        with self._cache_lock:
            now = time.time()
            stats = {
                'total_cached': len(self._client_cache),
                'by_service': {},
                'by_region': {},
                'expired_count': 0,
            }

            for key, entry in self._client_cache.items():
                parts = key.split(':')
                if len(parts) >= 2:
                    service, region = parts[0], parts[1]

                    # Count by service
                    stats['by_service'][service] = stats['by_service'].get(service, 0) + 1

                    # Count by region
                    stats['by_region'][region] = stats['by_region'].get(region, 0) + 1

                    # Check if expired
                    if not self._is_cache_valid(entry):
                        stats['expired_count'] += 1

            return stats

    def validate_credentials(
        self,
        credential: Optional[SecureCredential] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate AWS credentials by making a test API call.

        Returns information about the authenticated identity.

        Args:
            credential: SecureCredential to validate (or None for default)
            region: AWS region to use

        Returns:
            Dict with account, arn, and user_id from STS GetCallerIdentity

        Raises:
            AWSCredentialError: If credentials are invalid
        """
        region = region or self.default_region

        try:
            sts = self.get_client('sts', region, credential, force_new=True)
            response = sts.get_caller_identity()

            return {
                'account': response.get('Account'),
                'arn': response.get('Arn'),
                'user_id': response.get('UserId'),
                'valid': True,
            }

        except AWSCredentialError:
            raise
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            raise AWSCredentialError(
                f"Credential validation failed ({error_code}): {e}"
            )


# Singleton instance for global access
# Note: singleton pattern — not safe for multi-tenant Django with per-request credentials
_factory_instance: Optional[AWSClientFactory] = None
_factory_lock = Lock()


def get_aws_client_factory() -> AWSClientFactory:
    """
    Get the global AWS client factory instance.

    This provides a convenient way to access the factory from anywhere
    in the application without needing to pass it around.

    Returns:
        The global AWSClientFactory instance
    """
    global _factory_instance

    if _factory_instance is None:
        with _factory_lock:
            # Double-check locking
            if _factory_instance is None:
                _factory_instance = AWSClientFactory()

    return _factory_instance


def reset_aws_client_factory():
    """
    Reset the global factory instance.

    Useful for testing or when credentials change.
    """
    global _factory_instance

    with _factory_lock:
        if _factory_instance is not None:
            _factory_instance.clear_cache()
        _factory_instance = None
