"""
Service for AWS ECR (Elastic Container Registry) integration.

Provides functionality for managing ECR repositories, authentication,
and container image operations.
"""

import base64
from datetime import datetime
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from ..models import SecureCredential
from ..conf import get_setting
from ..exceptions import (
    AWSError,
    AWSCredentialError,
    ECRRepositoryError,
    ECRAuthenticationError,
    ECRImageError,
)
from .base import BaseService
from .credential_service import CredentialService


class ECRService(BaseService):
    """
    Service for AWS ECR operations.

    Handles repository management, authentication token retrieval,
    and container image operations.
    """

    def __init__(
        self, credential_service: Optional[CredentialService] = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.default_region = get_setting("AWS_DEFAULT_REGION", "us-east-1")

    def _get_ecr_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """Get boto3 ECR client with optional credentials."""
        region = region or self.default_region

        try:
            if (
                credential
                and credential.credential_type
                == SecureCredential.CredentialType.AWS_ACCESS_KEY
            ):
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    "ecr",
                    region_name=region,
                    aws_access_key_id=aws_creds["access_key_id"],
                    aws_secret_access_key=aws_creds["secret_access_key"],
                )
            else:
                return boto3.client("ecr", region_name=region)

        except NoCredentialsError:
            raise AWSCredentialError(
                "No AWS credentials found. Configure credentials via environment variables, "
                "AWS credentials file, or create an AWS credential in remote_compose."
            )
        except Exception as e:
            raise AWSError(f"Failed to create ECR client: {e}")

    def _get_sts_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """Get boto3 STS client with optional credentials."""
        region = region or self.default_region

        try:
            if (
                credential
                and credential.credential_type
                == SecureCredential.CredentialType.AWS_ACCESS_KEY
            ):
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    "sts",
                    region_name=region,
                    aws_access_key_id=aws_creds["access_key_id"],
                    aws_secret_access_key=aws_creds["secret_access_key"],
                )
            else:
                return boto3.client("sts", region_name=region)
        except Exception as e:
            raise AWSError(f"Failed to create STS client: {e}")

    # -------------------------------------------------------------------------
    # Repository Management
    # -------------------------------------------------------------------------

    def get_or_create_repository(
        self,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        image_tag_mutability: str = "MUTABLE",
        scan_on_push: bool = False,
        encryption_type: str = "AES256",
    ) -> Dict[str, Any]:
        """
        Get an existing ECR repository or create it if it doesn't exist.

        Args:
            name: Repository name
            region: AWS region
            credential: AWS credential
            image_tag_mutability: MUTABLE or IMMUTABLE
            scan_on_push: Enable image scanning on push
            encryption_type: AES256 or KMS

        Returns:
            Dict with repository details including repositoryArn, repositoryUri, etc.
        """
        client = self._get_ecr_client(region, credential)

        try:
            response = client.describe_repositories(repositoryNames=[name])
            repositories = response.get("repositories", [])
            if repositories:
                self.log_info(f"Found existing ECR repository: {name}")
                return self._format_repository(repositories[0])

        except ClientError as e:
            if e.response["Error"]["Code"] != "RepositoryNotFoundException":
                raise ECRRepositoryError(
                    f"Failed to describe repository: {e}",
                    repository_name=name,
                    region=region,
                )

        try:
            create_params = {
                "repositoryName": name,
                "imageTagMutability": image_tag_mutability,
                "imageScanningConfiguration": {
                    "scanOnPush": scan_on_push,
                },
                "encryptionConfiguration": {
                    "encryptionType": encryption_type,
                },
            }

            response = client.create_repository(**create_params)
            repository = response.get("repository", {})

            self.log_info(f"Created ECR repository: {name}")
            self.notify_observers("ecr_repository_created", repository_name=name)

            return self._format_repository(repository)

        except ClientError as e:
            raise ECRRepositoryError(
                f"Failed to create repository: {e}", repository_name=name, region=region
            )

    def delete_repository(
        self,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        force: bool = False,
    ) -> bool:
        """
        Delete an ECR repository.

        Args:
            name: Repository name
            region: AWS region
            credential: AWS credential
            force: Force deletion even if repository contains images

        Returns:
            True if deleted successfully
        """
        client = self._get_ecr_client(region, credential)

        try:
            client.delete_repository(
                repositoryName=name,
                force=force,
            )

            self.log_info(f"Deleted ECR repository: {name}")
            self.notify_observers("ecr_repository_deleted", repository_name=name)

            return True

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "RepositoryNotFoundException":
                self.log_warning(f"Repository not found for deletion: {name}")
                return False
            elif error_code == "RepositoryNotEmptyException":
                raise ECRRepositoryError(
                    f"Repository {name} is not empty. Use force=True to delete with images.",
                    repository_name=name,
                    region=region,
                )
            raise ECRRepositoryError(
                f"Failed to delete repository: {e}", repository_name=name, region=region
            )

    def list_repositories(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        List all ECR repositories in the account.

        Args:
            region: AWS region
            credential: AWS credential
            max_results: Maximum number of repositories to return

        Returns:
            List of repository dictionaries
        """
        client = self._get_ecr_client(region, credential)

        try:
            repositories = []
            paginator = client.get_paginator("describe_repositories")

            paginate_params = {}
            if max_results:
                paginate_params["PaginationConfig"] = {"MaxItems": max_results}

            for page in paginator.paginate(**paginate_params):
                for repo in page.get("repositories", []):
                    repositories.append(self._format_repository(repo))

            return repositories

        except ClientError as e:
            raise ECRRepositoryError(f"Failed to list repositories: {e}")

    def _format_repository(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        """Format repository response to consistent structure."""
        return {
            "repository_arn": repo.get("repositoryArn"),
            "repository_name": repo.get("repositoryName"),
            "repository_uri": repo.get("repositoryUri"),
            "registry_id": repo.get("registryId"),
            "created_at": repo.get("createdAt"),
            "image_tag_mutability": repo.get("imageTagMutability"),
            "image_scanning_enabled": repo.get("imageScanningConfiguration", {}).get(
                "scanOnPush", False
            ),
            "encryption_type": repo.get("encryptionConfiguration", {}).get(
                "encryptionType"
            ),
        }

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    def get_authorization_token(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """
        Get Docker login credentials for ECR.

        Args:
            region: AWS region
            credential: AWS credential

        Returns:
            Dict with username, password, proxy_endpoint, and expires_at
        """
        client = self._get_ecr_client(region, credential)

        try:
            response = client.get_authorization_token()
            auth_data = response.get("authorizationData", [])

            if not auth_data:
                raise ECRAuthenticationError(
                    "No authorization data returned from ECR", region=region
                )

            auth = auth_data[0]
            token = auth.get("authorizationToken", "")

            decoded = base64.b64decode(token).decode("utf-8")
            username, password = decoded.split(":", 1)

            expires_at = auth.get("expiresAt")
            if isinstance(expires_at, datetime):
                expires_at = expires_at.isoformat()

            self.log_info("Retrieved ECR authorization token")

            return {
                "username": username,
                "password": password,
                "proxy_endpoint": auth.get("proxyEndpoint"),
                "expires_at": expires_at,
            }

        except ClientError as e:
            raise ECRAuthenticationError(
                f"Failed to get authorization token: {e}", region=region
            )
        except (ValueError, UnicodeDecodeError) as e:
            raise ECRAuthenticationError(
                f"Failed to decode authorization token: {e}", region=region
            )

    def docker_login(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        execute: bool = False,
    ) -> str:
        """
        Get or execute Docker login command for ECR.

        Args:
            region: AWS region
            credential: AWS credential
            execute: If True, execute the login command; if False, return the command

        Returns:
            Docker login command string (if execute=False) or success message (if execute=True)
        """
        auth = self.get_authorization_token(region, credential)

        login_command = (
            f"docker login --username {auth['username']} "
            f"--password-stdin {auth['proxy_endpoint']}"
        )

        if not execute:
            return login_command

        import subprocess

        try:
            subprocess.run(
                [
                    "docker",
                    "login",
                    "--username",
                    auth["username"],
                    "--password-stdin",
                    auth["proxy_endpoint"],
                ],
                input=auth["password"].encode(),
                capture_output=True,
                check=True,
            )

            self.log_info(f"Docker login successful to {auth['proxy_endpoint']}")
            return f"Login Succeeded to {auth['proxy_endpoint']}"

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            raise ECRAuthenticationError(
                f"Docker login failed: {error_msg}", region=region
            )
        except FileNotFoundError:
            raise ECRAuthenticationError(
                "Docker CLI not found. Ensure Docker is installed and in PATH.",
                region=region,
            )

    # -------------------------------------------------------------------------
    # Image Management
    # -------------------------------------------------------------------------

    def image_exists(
        self,
        repository: str,
        tag: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> bool:
        """
        Check if an image with a specific tag exists in a repository.

        Args:
            repository: Repository name
            tag: Image tag
            region: AWS region
            credential: AWS credential

        Returns:
            True if image exists, False otherwise
        """
        client = self._get_ecr_client(region, credential)

        try:
            response = client.describe_images(
                repositoryName=repository,
                imageIds=[{"imageTag": tag}],
            )

            images = response.get("imageDetails", [])
            return len(images) > 0

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("ImageNotFoundException", "RepositoryNotFoundException"):
                return False
            raise ECRImageError(
                f"Failed to check image existence: {e}",
                repository_name=repository,
                image_tag=tag,
            )

    def get_image_uri(
        self,
        account_id: str,
        region: str,
        repository: str,
        tag: str = "latest",
    ) -> str:
        """
        Build the full ECR image URI.

        Args:
            account_id: AWS account ID
            region: AWS region
            repository: Repository name
            tag: Image tag (default: latest)

        Returns:
            Full ECR image URI (e.g., 123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo:latest)
        """
        return f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{tag}"

    def list_images(
        self,
        repository: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        filter_tag_status: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        List images in a repository.

        Args:
            repository: Repository name
            region: AWS region
            credential: AWS credential
            filter_tag_status: Filter by tag status (TAGGED, UNTAGGED, or ANY)
            max_results: Maximum number of images to return

        Returns:
            List of image dictionaries
        """
        client = self._get_ecr_client(region, credential)

        try:
            images = []
            paginator = client.get_paginator("describe_images")

            paginate_params = {"repositoryName": repository}
            if filter_tag_status:
                paginate_params["filter"] = {"tagStatus": filter_tag_status}
            if max_results:
                paginate_params["PaginationConfig"] = {"MaxItems": max_results}

            for page in paginator.paginate(**paginate_params):
                for image in page.get("imageDetails", []):
                    images.append(self._format_image(image))

            return images

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "RepositoryNotFoundException":
                raise ECRRepositoryError(
                    f"Repository not found: {repository}",
                    repository_name=repository,
                    region=region,
                )
            raise ECRImageError(
                f"Failed to list images: {e}", repository_name=repository
            )

    def delete_image(
        self,
        repository: str,
        image_ids: List[Dict[str, str]],
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """
        Delete specific images from a repository.

        Args:
            repository: Repository name
            image_ids: List of image IDs to delete. Each dict should have either
                       'imageDigest' and/or 'imageTag' keys
            region: AWS region
            credential: AWS credential

        Returns:
            Dict with 'deleted' and 'failed' lists
        """
        client = self._get_ecr_client(region, credential)

        try:
            response = client.batch_delete_image(
                repositoryName=repository,
                imageIds=image_ids,
            )

            deleted = response.get("imageIds", [])
            failures = response.get("failures", [])

            if deleted:
                self.log_info(f"Deleted {len(deleted)} image(s) from {repository}")

            if failures:
                failed_tags = [
                    f.get("imageId", {}).get(
                        "imageTag", f.get("imageId", {}).get("imageDigest", "unknown")
                    )
                    for f in failures
                ]
                self.log_warning(f"Failed to delete images: {failed_tags}")

            self.notify_observers(
                "ecr_images_deleted",
                repository_name=repository,
                deleted_count=len(deleted),
            )

            return {
                "deleted": deleted,
                "failed": [
                    {
                        "image_id": f.get("imageId"),
                        "failure_code": f.get("failureCode"),
                        "failure_reason": f.get("failureReason"),
                    }
                    for f in failures
                ],
            }

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "RepositoryNotFoundException":
                raise ECRRepositoryError(
                    f"Repository not found: {repository}",
                    repository_name=repository,
                    region=region,
                )
            raise ECRImageError(
                f"Failed to delete images: {e}", repository_name=repository
            )

    def _format_image(self, image: Dict[str, Any]) -> Dict[str, Any]:
        """Format image response to consistent structure."""
        pushed_at = image.get("imagePushedAt")
        if isinstance(pushed_at, datetime):
            pushed_at = pushed_at.isoformat()

        return {
            "image_digest": image.get("imageDigest"),
            "image_tags": image.get("imageTags", []),
            "image_size_bytes": image.get("imageSizeInBytes"),
            "pushed_at": pushed_at,
            "registry_id": image.get("registryId"),
            "repository_name": image.get("repositoryName"),
            "image_scan_status": image.get("imageScanStatus", {}).get("status"),
            "image_manifest_media_type": image.get("imageManifestMediaType"),
            "artifact_media_type": image.get("artifactMediaType"),
        }

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def get_account_id(
        self,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """
        Get the AWS account ID using STS.

        Args:
            credential: AWS credential

        Returns:
            AWS account ID
        """
        sts_client = self._get_sts_client(credential=credential)

        try:
            response = sts_client.get_caller_identity()
            account_id = response.get("Account")

            if not account_id:
                raise AWSError("Failed to retrieve account ID from STS")

            return account_id

        except ClientError as e:
            raise AWSError(f"Failed to get account ID: {e}")

    def get_registry_id(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """
        Get the ECR registry ID (same as account ID).

        Args:
            region: AWS region
            credential: AWS credential

        Returns:
            ECR registry ID
        """
        return self.get_account_id(credential)

    def get_login_password(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """
        Get the ECR login password (convenience method).

        Args:
            region: AWS region
            credential: AWS credential

        Returns:
            ECR password for Docker login
        """
        auth = self.get_authorization_token(region, credential)
        return auth["password"]

    def tag_image(
        self,
        repository: str,
        source_image_digest: str,
        target_tag: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> bool:
        """
        Add a tag to an existing image.

        Args:
            repository: Repository name
            source_image_digest: Digest of the source image
            target_tag: New tag to apply
            region: AWS region
            credential: AWS credential

        Returns:
            True if successful
        """
        client = self._get_ecr_client(region, credential)

        try:
            client.put_image(
                repositoryName=repository,
                imageManifest=self._get_image_manifest(
                    repository, source_image_digest, region, credential
                ),
                imageTag=target_tag,
            )

            self.log_info(
                f"Tagged image {source_image_digest[:12]} as {target_tag} in {repository}"
            )
            return True

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ImageAlreadyExistsException":
                self.log_info(f"Image already has tag {target_tag}")
                return True
            raise ECRImageError(
                f"Failed to tag image: {e}",
                repository_name=repository,
                image_tag=target_tag,
            )

    def _get_image_manifest(
        self,
        repository: str,
        image_digest: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """Get the manifest for an image by digest."""
        client = self._get_ecr_client(region, credential)

        try:
            response = client.batch_get_image(
                repositoryName=repository,
                imageIds=[{"imageDigest": image_digest}],
            )

            images = response.get("images", [])
            if not images:
                raise ECRImageError(
                    f"Image not found: {image_digest}", repository_name=repository
                )

            return images[0].get("imageManifest", "")

        except ClientError as e:
            raise ECRImageError(
                f"Failed to get image manifest: {e}", repository_name=repository
            )

    def set_lifecycle_policy(
        self,
        repository: str,
        policy: Dict[str, Any],
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> bool:
        """
        Set a lifecycle policy for a repository.

        Args:
            repository: Repository name
            policy: Lifecycle policy document
            region: AWS region
            credential: AWS credential

        Returns:
            True if successful
        """
        import json

        client = self._get_ecr_client(region, credential)

        try:
            client.put_lifecycle_policy(
                repositoryName=repository,
                lifecyclePolicyText=json.dumps(policy),
            )

            self.log_info(f"Set lifecycle policy for repository: {repository}")
            return True

        except ClientError as e:
            raise ECRRepositoryError(
                f"Failed to set lifecycle policy: {e}",
                repository_name=repository,
                region=region,
            )

    def create_default_lifecycle_policy(
        self,
        repository: str,
        keep_last_n: int = 10,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> bool:
        """
        Create a default lifecycle policy that keeps the last N images.

        Args:
            repository: Repository name
            keep_last_n: Number of images to keep (default: 10)
            region: AWS region
            credential: AWS credential

        Returns:
            True if successful
        """
        policy = {
            "rules": [
                {
                    "rulePriority": 1,
                    "description": f"Keep last {keep_last_n} images",
                    "selection": {
                        "tagStatus": "any",
                        "countType": "imageCountMoreThan",
                        "countNumber": keep_last_n,
                    },
                    "action": {
                        "type": "expire",
                    },
                }
            ]
        }

        return self.set_lifecycle_policy(repository, policy, region, credential)
