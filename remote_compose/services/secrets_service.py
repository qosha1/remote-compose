"""
Service for AWS Secrets Manager integration for ECS deployments.

Provides functionality for creating, managing, and referencing secrets
that are injected into ECS task definitions as environment variables.
"""

import os
from typing import Optional, Dict, List

from botocore.exceptions import ClientError

from ..models import SecretConfig
from ..exceptions import SecretProvisioningError
from .base import BaseService
from .aws_client_factory import AWSClientFactory, get_aws_client_factory


class SecretsService(BaseService):
    """
    Service for managing AWS Secrets Manager secrets for ECS deployments.

    Handles creating and updating secrets, parsing env files, and building
    ECS task definition secret configurations.
    """

    def __init__(self, aws_factory: Optional[AWSClientFactory] = None, **kwargs):
        super().__init__(**kwargs)
        self.aws_factory = aws_factory or get_aws_client_factory()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get_or_create_secret(
        self,
        cluster,
        name: str,
        value: str,
        region: Optional[str] = None,
        credential=None,
    ) -> str:
        """
        Get or create a secret in AWS Secrets Manager.

        If the secret already exists, its value is updated. The secret name
        is namespaced as ``{cluster.name}/{name}``.

        Args:
            cluster: ECSCluster model instance.
            name: Secret name (will be prefixed with cluster name).
            value: Secret value.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            The ARN of the secret.

        Raises:
            SecretProvisioningError: If creation or update fails.
        """
        sm = self.aws_factory.get_client('secretsmanager', region=region, credential=credential)
        secret_name = f"{cluster.name}/{name}"

        # Try to update existing secret
        try:
            response = sm.describe_secret(SecretId=secret_name)
            secret_arn = response['ARN']

            # Update the value
            sm.put_secret_value(
                SecretId=secret_name,
                SecretString=value,
            )

            self.log_info(f"Updated existing secret: {secret_name}")

            # Ensure database record exists
            SecretConfig.objects.update_or_create(
                cluster=cluster,
                env_var_name=name,
                defaults={
                    'secret_arn': secret_arn,
                    'secret_name': secret_name,
                },
            )

            return secret_arn

        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code != 'ResourceNotFoundException':
                raise SecretProvisioningError(
                    f"Failed to describe secret {secret_name}: {e}",
                    secret_name=secret_name,
                )

        # Create new secret
        try:
            response = sm.create_secret(
                Name=secret_name,
                SecretString=value,
                Tags=[
                    {'Key': 'remote-compose:cluster', 'Value': cluster.name},
                    {'Key': 'remote-compose:managed', 'Value': 'true'},
                    {'Key': 'remote-compose:env-var', 'Value': name},
                ],
            )

            secret_arn = response['ARN']
            self.log_info(f"Created secret: {secret_name}")

            # Persist to database
            SecretConfig.objects.update_or_create(
                cluster=cluster,
                env_var_name=name,
                defaults={
                    'secret_arn': secret_arn,
                    'secret_name': secret_name,
                },
            )

            self.notify_observers(
                'secret_created',
                cluster_name=cluster.name,
                secret_name=secret_name,
            )

            return secret_arn

        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'ResourceExistsException':
                # Race condition -- secret was created between describe and create
                return self.get_or_create_secret(cluster, name, value, region, credential)
            raise SecretProvisioningError(
                f"Failed to create secret {secret_name}: {e}",
                secret_name=secret_name,
            )

    def push_env_file(
        self,
        cluster,
        env_file_path: str,
        region: Optional[str] = None,
        credential=None,
    ) -> Dict[str, str]:
        """
        Read an env file and push each variable as a Secrets Manager secret.

        Args:
            cluster: ECSCluster model instance.
            env_file_path: Path to the .env file.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            Dict mapping environment variable names to their secret ARNs.

        Raises:
            SecretProvisioningError: If the env file cannot be read or secrets fail.
        """
        env_vars = self._parse_env_file(env_file_path)

        if not env_vars:
            self.log_warning(f"No environment variables found in {env_file_path}")
            return {}

        secret_arns: Dict[str, str] = {}

        for name, value in env_vars.items():
            arn = self.get_or_create_secret(
                cluster=cluster,
                name=name,
                value=value,
                region=region,
                credential=credential,
            )
            secret_arns[name] = arn

            # Update source_file on the database record
            try:
                config = SecretConfig.objects.get(cluster=cluster, env_var_name=name)
                config.source_file = env_file_path
                config.save(update_fields=['source_file'])
            except SecretConfig.DoesNotExist:
                pass

        self.log_info(
            f"Pushed {len(secret_arns)} secrets from {env_file_path} "
            f"for cluster {cluster.name}"
        )

        self.notify_observers(
            'env_file_pushed',
            cluster_name=cluster.name,
            env_file_path=env_file_path,
            secret_count=len(secret_arns),
        )

        return secret_arns

    def build_ecs_secrets_config(
        self,
        secrets_arns: Dict[str, str],
    ) -> List[Dict[str, str]]:
        """
        Convert a dict of secret ARNs into ECS task definition secrets config.

        Args:
            secrets_arns: Dict mapping env var names to secret ARNs.

        Returns:
            List of dicts with 'name' and 'valueFrom' keys suitable for
            use in ECS container definition ``secrets`` field.
        """
        return [
            {'name': name, 'valueFrom': arn}
            for name, arn in sorted(secrets_arns.items())
        ]

    def list_managed_secrets(
        self,
        cluster,
        region: Optional[str] = None,
        credential=None,
    ) -> List[Dict]:
        """
        List all secrets managed by remote-compose for a cluster.

        Args:
            cluster: ECSCluster model instance.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            List of dicts with secret metadata.
        """
        sm = self.aws_factory.get_client('secretsmanager', region=region, credential=credential)

        try:
            secrets = []
            paginator = sm.get_paginator('list_secrets')

            for page in paginator.paginate(
                Filters=[
                    {
                        'Key': 'tag-key',
                        'Values': ['remote-compose:cluster'],
                    },
                    {
                        'Key': 'tag-value',
                        'Values': [cluster.name],
                    },
                ],
            ):
                for secret in page.get('SecretList', []):
                    secrets.append({
                        'arn': secret.get('ARN'),
                        'name': secret.get('Name'),
                        'description': secret.get('Description', ''),
                        'created_date': str(secret.get('CreatedDate', '')),
                        'last_changed_date': str(secret.get('LastChangedDate', '')),
                        'tags': {
                            t['Key']: t['Value']
                            for t in secret.get('Tags', [])
                        },
                    })

            return secrets

        except ClientError as e:
            raise SecretProvisioningError(
                f"Failed to list secrets for cluster {cluster.name}: {e}",
            )

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _parse_env_file(self, path: str) -> Dict[str, str]:
        """
        Parse a .env file into key-value pairs.

        Supports KEY=VALUE format. Skips empty lines and lines starting
        with '#'. Strips surrounding quotes from values.

        Args:
            path: Path to the .env file.

        Returns:
            Dict mapping variable names to values.

        Raises:
            SecretProvisioningError: If the file cannot be read.
        """
        if not os.path.isfile(path):
            raise SecretProvisioningError(
                f"Environment file not found: {path}",
            )

        env_vars: Dict[str, str] = {}

        try:
            with open(path, 'r') as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()

                    # Skip empty lines and comments
                    if not line or line.startswith('#'):
                        continue

                    # Split on first '='
                    if '=' not in line:
                        self.log_warning(
                            f"Skipping invalid line {line_num} in {path}: no '=' found"
                        )
                        continue

                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()

                    # Strip surrounding quotes
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                        value = value[1:-1]

                    if key:
                        env_vars[key] = value

        except OSError as e:
            raise SecretProvisioningError(
                f"Failed to read environment file {path}: {e}",
            )

        return env_vars
