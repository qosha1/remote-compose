"""
Service for AWS EFS (Elastic File System) integration.

Provides functionality for managing EFS file systems, access points,
mount targets, and security groups for persistent volumes in ECS Fargate.
"""

import hashlib
import time
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from ..models import SecureCredential
from ..conf import get_setting
from ..exceptions import (
    AWSError,
    AWSCredentialError,
    EFSError,
    EFSFileSystemError,
    EFSAccessPointError,
    EFSMountTargetError,
)
from .base import BaseService
from .credential_service import CredentialService


class EFSService(BaseService):
    """
    Service for AWS EFS operations.

    Handles file system management, access point creation,
    mount target configuration, and security group setup
    for persistent volumes in ECS Fargate deployments.
    """

    # Default POSIX user settings for access points (remote-compose-29w).
    # Standard non-root unprivileged user (1000/1000) with mode 0755:
    # owner rwx, group/other rx. Most container images run as a
    # non-root user; matching that surface in the EFS access point
    # avoids accidentally writing files as root that the application
    # then can't read. Applications that need permissive defaults (e.g.
    # legacy postgres images that want world-writable) should set the
    # values explicitly via the create_access_point() kwargs or use
    # ``EFSService.PERMISSIVE_*`` constants below.
    DEFAULT_UID = 1000
    DEFAULT_GID = 1000
    DEFAULT_PERMISSIONS = "0755"

    # Opt-in permissive defaults for the unusual case where the
    # application performs its own chown/chmod on startup and needs
    # ECS to mount as root + world-writable. Pre-fix code unwittingly
    # used these for every volume; new code must opt in explicitly.
    PERMISSIVE_UID = 0
    PERMISSIVE_GID = 0
    PERMISSIVE_PERMISSIONS = "0777"

    def __init__(
        self, credential_service: Optional[CredentialService] = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.default_region = get_setting("AWS_DEFAULT_REGION", "us-east-1")

    # -------------------------------------------------------------------------
    # Client Factory Methods
    # -------------------------------------------------------------------------

    def _get_efs_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """Get boto3 EFS client with optional credentials."""
        region = region or self.default_region

        try:
            if (
                credential
                and credential.credential_type
                == SecureCredential.CredentialType.AWS_ACCESS_KEY
            ):
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    "efs",
                    region_name=region,
                    aws_access_key_id=aws_creds["access_key_id"],
                    aws_secret_access_key=aws_creds["secret_access_key"],
                )
            else:
                return boto3.client("efs", region_name=region)

        except NoCredentialsError:
            raise AWSCredentialError(
                "No AWS credentials found. Configure credentials via environment variables, "
                "AWS credentials file, or create an AWS credential in remote_compose."
            )
        except Exception as e:
            raise AWSError(f"Failed to create EFS client: {e}")

    def _get_ec2_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """Get boto3 EC2 client for VPC and security group operations."""
        region = region or self.default_region

        try:
            if (
                credential
                and credential.credential_type
                == SecureCredential.CredentialType.AWS_ACCESS_KEY
            ):
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    "ec2",
                    region_name=region,
                    aws_access_key_id=aws_creds["access_key_id"],
                    aws_secret_access_key=aws_creds["secret_access_key"],
                )
            else:
                return boto3.client("ec2", region_name=region)
        except Exception as e:
            raise AWSError(f"Failed to create EC2 client: {e}")

    # -------------------------------------------------------------------------
    # File System Management
    # -------------------------------------------------------------------------

    def get_or_create_file_system(
        self,
        name: str,
        region: Optional[str] = None,
        vpc_id: Optional[str] = None,
        subnet_ids: Optional[List[str]] = None,
        security_group_ids: Optional[List[str]] = None,
        credential: Optional[SecureCredential] = None,
        performance_mode: str = "generalPurpose",
        throughput_mode: str = "bursting",
        encrypted: bool = True,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Get an existing EFS file system or create it if it doesn't exist.

        Uses Name tag for identification. Creates mount targets in each
        provided subnet if creating a new file system.

        Args:
            name: File system name (used as Name tag)
            region: AWS region
            vpc_id: VPC ID for security group creation (required for new file systems)
            subnet_ids: Subnet IDs for mount targets (required for new file systems)
            security_group_ids: Security group IDs for mount targets (optional, will create if not provided)
            credential: AWS credential
            performance_mode: 'generalPurpose' or 'maxIO'
            throughput_mode: 'bursting', 'provisioned', or 'elastic'
            encrypted: Enable encryption at rest
            tags: Additional tags to apply

        Returns:
            Dict with file_system_id, dns_name, arn, status, mount_target_ids, etc.
        """
        region = region or self.default_region
        client = self._get_efs_client(region, credential)

        # Try to find existing file system by Name tag
        try:
            existing = self._find_file_system_by_name(name, region, credential)
            if existing:
                self.log_info(f"Found existing EFS file system: {name}")
                return existing
        except EFSFileSystemError:
            pass  # No existing file system, will create

        # Validate required parameters for creation
        if not subnet_ids:
            raise EFSFileSystemError(
                "subnet_ids are required when creating a new file system",
                file_system_name=name,
            )

        # Prepare tags
        all_tags = {
            "Name": name,
            "CreatedBy": "remote-compose",
        }
        if tags:
            all_tags.update(tags)

        try:
            # Create the file system
            create_params = {
                "CreationToken": f"remote-compose-{name}",
                "PerformanceMode": performance_mode,
                "ThroughputMode": throughput_mode,
                "Encrypted": encrypted,
                "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()],
            }

            response = client.create_file_system(**create_params)
            file_system_id = response["FileSystemId"]

            self.log_info(f"Created EFS file system: {file_system_id} ({name})")
            self.notify_observers(
                "efs_file_system_created", file_system_id=file_system_id, name=name
            )

            # Wait for file system to be available before creating mount targets
            self._wait_for_file_system_available(file_system_id, region, credential)

            # Get or create security group for EFS
            if not security_group_ids and vpc_id:
                sg = self.get_or_create_efs_security_group(
                    vpc_id=vpc_id,
                    name=f"{name}-efs-sg",
                    region=region,
                    credential=credential,
                )
                security_group_ids = [sg["security_group_id"]]

            # Create mount targets
            mount_target_ids = []
            if subnet_ids and security_group_ids:
                mount_target_ids = self.create_mount_targets(
                    file_system_id=file_system_id,
                    subnet_ids=subnet_ids,
                    security_group_ids=security_group_ids,
                    region=region,
                    credential=credential,
                )

            # Wait for mount targets to be available
            if mount_target_ids:
                self.wait_for_mount_targets_available(
                    file_system_id, region, credential
                )

            return self._format_file_system(response, mount_target_ids, region)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "FileSystemAlreadyExists":
                # Race condition: another process created it
                existing = self._find_file_system_by_name(name, region, credential)
                if existing:
                    return existing
            raise EFSFileSystemError(
                f"Failed to create file system: {e}", file_system_name=name
            )

    def _find_file_system_by_name(
        self,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find a file system by its Name tag.

        remote-compose-4rm: Earlier behavior made an extra
        ``describe_tags`` call for EVERY file system in the account
        (N+1 API), AND ``describe_tags`` is deprecated (AWS replaced
        it with ``list_tags_for_resource``). Tags are already included
        in the ``describe_file_systems`` response under each fs's
        ``Tags`` key — read them inline. Drops one API call per fs and
        eliminates the deprecated-API dependency.
        """
        client = self._get_efs_client(region, credential)

        try:
            paginator = client.get_paginator("describe_file_systems")
            for page in paginator.paginate():
                for fs in page.get("FileSystems", []):
                    # Tags are already in the describe_file_systems
                    # response — no extra round-trip needed.
                    tags = {
                        t["Key"]: t["Value"]
                        for t in (fs.get("Tags") or [])
                        if isinstance(t, dict) and "Key" in t and "Value" in t
                    }
                    if tags.get("Name") == name:
                        mount_targets = self._get_mount_target_ids(
                            fs["FileSystemId"], region, credential
                        )
                        return self._format_file_system(fs, mount_targets, region)
            return None

        except ClientError as e:
            raise EFSFileSystemError(f"Failed to search for file system: {e}")

    def describe_file_system(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """
        Get details of a specific EFS file system.

        Args:
            file_system_id: EFS file system ID
            region: AWS region
            credential: AWS credential

        Returns:
            Dict with file system details
        """
        client = self._get_efs_client(region, credential)

        try:
            response = client.describe_file_systems(FileSystemId=file_system_id)
            file_systems = response.get("FileSystems", [])

            if not file_systems:
                raise EFSFileSystemError(
                    f"File system not found: {file_system_id}",
                    file_system_id=file_system_id,
                )

            fs = file_systems[0]
            mount_targets = self._get_mount_target_ids(
                file_system_id, region, credential
            )
            return self._format_file_system(fs, mount_targets, region)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "FileSystemNotFound":
                raise EFSFileSystemError(
                    f"File system not found: {file_system_id}",
                    file_system_id=file_system_id,
                )
            raise EFSFileSystemError(
                f"Failed to describe file system: {e}", file_system_id=file_system_id
            )

    def list_file_systems(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        List all EFS file systems in the region.

        Args:
            region: AWS region
            credential: AWS credential
            max_results: Maximum number of file systems to return

        Returns:
            List of file system dictionaries
        """
        client = self._get_efs_client(region, credential)

        try:
            file_systems = []
            paginator = client.get_paginator("describe_file_systems")

            paginate_params = {}
            if max_results:
                paginate_params["PaginationConfig"] = {"MaxItems": max_results}

            for page in paginator.paginate(**paginate_params):
                for fs in page.get("FileSystems", []):
                    mount_targets = self._get_mount_target_ids(
                        fs["FileSystemId"], region, credential
                    )
                    file_systems.append(
                        self._format_file_system(fs, mount_targets, region)
                    )

            return file_systems

        except ClientError as e:
            raise EFSFileSystemError(f"Failed to list file systems: {e}")

    def delete_file_system(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        delete_mount_targets: bool = True,
        delete_access_points: bool = True,
    ) -> bool:
        """
        Delete an EFS file system.

        Must delete all mount targets and access points before deleting
        the file system.

        Args:
            file_system_id: EFS file system ID
            region: AWS region
            credential: AWS credential
            delete_mount_targets: Delete mount targets first
            delete_access_points: Delete access points first

        Returns:
            True if deleted successfully
        """
        client = self._get_efs_client(region, credential)

        try:
            # Delete access points first
            if delete_access_points:
                access_points = self.list_access_points(
                    file_system_id, region, credential
                )
                for ap in access_points:
                    self.delete_access_point(ap["access_point_id"], region, credential)

            # Delete mount targets
            if delete_mount_targets:
                self.delete_mount_targets(file_system_id, region, credential)

            # Wait for mount targets to be fully deleted
            self._wait_for_no_mount_targets(file_system_id, region, credential)

            # Delete the file system
            client.delete_file_system(FileSystemId=file_system_id)

            self.log_info(f"Deleted EFS file system: {file_system_id}")
            self.notify_observers(
                "efs_file_system_deleted", file_system_id=file_system_id
            )

            return True

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "FileSystemNotFound":
                self.log_warning(
                    f"File system not found for deletion: {file_system_id}"
                )
                return False
            elif error_code == "FileSystemInUse":
                raise EFSFileSystemError(
                    f"File system {file_system_id} is still in use. "
                    "Delete mount targets and access points first.",
                    file_system_id=file_system_id,
                )
            raise EFSFileSystemError(
                f"Failed to delete file system: {e}", file_system_id=file_system_id
            )

    def _wait_for_file_system_available(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> None:
        """Wait for a file system to reach 'available' status."""
        client = self._get_efs_client(region, credential)
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = client.describe_file_systems(FileSystemId=file_system_id)
                file_systems = response.get("FileSystems", [])

                if file_systems:
                    status = file_systems[0].get("LifeCycleState")
                    if status == "available":
                        return
                    elif status == "error":
                        raise EFSFileSystemError(
                            f"File system entered error state: {file_system_id}",
                            file_system_id=file_system_id,
                        )

                time.sleep(poll_interval)

            except ClientError as e:
                self.log_warning(f"Error polling file system status: {e}")
                time.sleep(poll_interval)

        raise EFSFileSystemError(
            f"Timeout waiting for file system {file_system_id} to become available",
            file_system_id=file_system_id,
        )

    def _format_file_system(
        self,
        fs: Dict[str, Any],
        mount_target_ids: List[str],
        region: str,
    ) -> Dict[str, Any]:
        """Format file system response to consistent structure."""
        file_system_id = fs.get("FileSystemId", "")
        return {
            "file_system_id": file_system_id,
            "file_system_arn": fs.get("FileSystemArn"),
            "creation_token": fs.get("CreationToken"),
            "life_cycle_state": fs.get("LifeCycleState"),
            "name": fs.get("Name"),
            "number_of_mount_targets": fs.get("NumberOfMountTargets", 0),
            "size_in_bytes": fs.get("SizeInBytes", {}).get("Value", 0),
            "performance_mode": fs.get("PerformanceMode"),
            "throughput_mode": fs.get("ThroughputMode"),
            "encrypted": fs.get("Encrypted", False),
            "kms_key_id": fs.get("KmsKeyId"),
            "dns_name": f"{file_system_id}.efs.{region}.amazonaws.com",
            "mount_target_ids": mount_target_ids,
            "tags": {t["Key"]: t["Value"] for t in fs.get("Tags", [])},
        }

    # -------------------------------------------------------------------------
    # Access Point Management
    # -------------------------------------------------------------------------

    def create_access_point(
        self,
        file_system_id: str,
        path: str,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        uid: int = DEFAULT_UID,
        gid: int = DEFAULT_GID,
        permissions: str = DEFAULT_PERMISSIONS,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Create an access point for a specific path on the EFS file system.

        Access points provide application-specific entry points into EFS,
        enforcing a user identity and an optional root directory for all
        file system requests.

        Args:
            file_system_id: EFS file system ID
            path: Root directory path (e.g., '/postgres-data')
            name: Access point name
            region: AWS region
            credential: AWS credential
            uid: POSIX user ID (default: 1000)
            gid: POSIX group ID (default: 1000)
            permissions: Root directory permissions (default: '0755')
            tags: Additional tags to apply

        Returns:
            Dict with access_point_id, access_point_arn, path, etc.
        """
        client = self._get_efs_client(region, credential)

        # Ensure path starts with /
        if not path.startswith("/"):
            path = f"/{path}"

        # Prepare tags
        all_tags = {
            "Name": name,
            "CreatedBy": "remote-compose",
        }
        if tags:
            all_tags.update(tags)

        try:
            # ClientToken must be <= 64 characters
            # Use a hash of name + file_system_id for uniqueness
            token_hash = hashlib.sha256(
                f"{name}-{file_system_id}".encode()
            ).hexdigest()[:16]
            client_token = f"rc-{token_hash}"  # 3 + 16 = 19 chars, safely under 64

            response = client.create_access_point(
                ClientToken=client_token,
                FileSystemId=file_system_id,
                PosixUser={
                    "Uid": uid,
                    "Gid": gid,
                },
                RootDirectory={
                    "Path": path,
                    "CreationInfo": {
                        "OwnerUid": uid,
                        "OwnerGid": gid,
                        "Permissions": permissions,
                    },
                },
                Tags=[{"Key": k, "Value": v} for k, v in all_tags.items()],
            )

            self.log_info(
                f"Created EFS access point: {response['AccessPointId']} for path {path}"
            )
            self.notify_observers(
                "efs_access_point_created",
                access_point_id=response["AccessPointId"],
                file_system_id=file_system_id,
                path=path,
            )

            return self._format_access_point(response)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "AccessPointAlreadyExists":
                # Try to find existing access point
                existing = self._find_access_point_by_path(
                    file_system_id, path, region, credential
                )
                if existing:
                    return existing
            raise EFSAccessPointError(
                f"Failed to create access point: {e}",
                file_system_id=file_system_id,
                path=path,
            )

    def _find_access_point_by_path(
        self,
        file_system_id: str,
        path: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find an access point by its root directory path."""
        access_points = self.list_access_points(file_system_id, region, credential)
        for ap in access_points:
            if ap.get("root_directory_path") == path:
                return ap
        return None

    def delete_access_point(
        self,
        access_point_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> bool:
        """
        Delete an EFS access point.

        Args:
            access_point_id: Access point ID
            region: AWS region
            credential: AWS credential

        Returns:
            True if deleted successfully
        """
        client = self._get_efs_client(region, credential)

        try:
            client.delete_access_point(AccessPointId=access_point_id)

            self.log_info(f"Deleted EFS access point: {access_point_id}")
            self.notify_observers(
                "efs_access_point_deleted", access_point_id=access_point_id
            )

            return True

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "AccessPointNotFound":
                self.log_warning(
                    f"Access point not found for deletion: {access_point_id}"
                )
                return False
            raise EFSAccessPointError(
                f"Failed to delete access point: {e}", access_point_id=access_point_id
            )

    def list_access_points(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> List[Dict[str, Any]]:
        """
        List all access points for a file system.

        Args:
            file_system_id: EFS file system ID
            region: AWS region
            credential: AWS credential

        Returns:
            List of access point dictionaries
        """
        client = self._get_efs_client(region, credential)

        try:
            access_points = []
            paginator = client.get_paginator("describe_access_points")

            for page in paginator.paginate(FileSystemId=file_system_id):
                for ap in page.get("AccessPoints", []):
                    access_points.append(self._format_access_point(ap))

            return access_points

        except ClientError as e:
            raise EFSAccessPointError(
                f"Failed to list access points: {e}", file_system_id=file_system_id
            )

    def describe_access_point(
        self,
        access_point_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """
        Get details of a specific access point.

        Args:
            access_point_id: Access point ID
            region: AWS region
            credential: AWS credential

        Returns:
            Dict with access point details
        """
        client = self._get_efs_client(region, credential)

        try:
            response = client.describe_access_points(AccessPointId=access_point_id)
            access_points = response.get("AccessPoints", [])

            if not access_points:
                raise EFSAccessPointError(
                    f"Access point not found: {access_point_id}",
                    access_point_id=access_point_id,
                )

            return self._format_access_point(access_points[0])

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "AccessPointNotFound":
                raise EFSAccessPointError(
                    f"Access point not found: {access_point_id}",
                    access_point_id=access_point_id,
                )
            raise EFSAccessPointError(
                f"Failed to describe access point: {e}", access_point_id=access_point_id
            )

    def _format_access_point(self, ap: Dict[str, Any]) -> Dict[str, Any]:
        """Format access point response to consistent structure."""
        posix_user = ap.get("PosixUser", {})
        root_dir = ap.get("RootDirectory", {})
        creation_info = root_dir.get("CreationInfo", {})

        return {
            "access_point_id": ap.get("AccessPointId"),
            "access_point_arn": ap.get("AccessPointArn"),
            "file_system_id": ap.get("FileSystemId"),
            "name": ap.get("Name"),
            "life_cycle_state": ap.get("LifeCycleState"),
            "root_directory_path": root_dir.get("Path", "/"),
            "posix_uid": posix_user.get("Uid"),
            "posix_gid": posix_user.get("Gid"),
            "owner_uid": creation_info.get("OwnerUid"),
            "owner_gid": creation_info.get("OwnerGid"),
            "permissions": creation_info.get("Permissions"),
            "tags": {t["Key"]: t["Value"] for t in ap.get("Tags", [])},
        }

    # -------------------------------------------------------------------------
    # Mount Target Management
    # -------------------------------------------------------------------------

    def create_mount_targets(
        self,
        file_system_id: str,
        subnet_ids: List[str],
        security_group_ids: List[str],
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> List[str]:
        """
        Create mount targets in each subnet for an EFS file system.

        Args:
            file_system_id: EFS file system ID
            subnet_ids: List of subnet IDs
            security_group_ids: Security group IDs for the mount targets
            region: AWS region
            credential: AWS credential

        Returns:
            List of created mount target IDs
        """
        client = self._get_efs_client(region, credential)
        mount_target_ids = []

        for subnet_id in subnet_ids:
            try:
                response = client.create_mount_target(
                    FileSystemId=file_system_id,
                    SubnetId=subnet_id,
                    SecurityGroups=security_group_ids,
                )

                mount_target_id = response["MountTargetId"]
                mount_target_ids.append(mount_target_id)

                self.log_info(
                    f"Created mount target: {mount_target_id} in subnet {subnet_id}"
                )

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "MountTargetConflict":
                    # Mount target already exists in this subnet
                    existing = self._get_mount_target_for_subnet(
                        file_system_id, subnet_id, region, credential
                    )
                    if existing:
                        mount_target_ids.append(existing)
                        self.log_info(
                            f"Mount target already exists in subnet {subnet_id}"
                        )
                        continue
                raise EFSMountTargetError(
                    f"Failed to create mount target in subnet {subnet_id}: {e}",
                    file_system_id=file_system_id,
                    subnet_id=subnet_id,
                )

        self.notify_observers(
            "efs_mount_targets_created",
            file_system_id=file_system_id,
            mount_target_ids=mount_target_ids,
        )

        return mount_target_ids

    def _get_mount_target_for_subnet(
        self,
        file_system_id: str,
        subnet_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Optional[str]:
        """Get the mount target ID for a specific subnet."""
        client = self._get_efs_client(region, credential)

        try:
            response = client.describe_mount_targets(FileSystemId=file_system_id)
            for mt in response.get("MountTargets", []):
                if mt.get("SubnetId") == subnet_id:
                    return mt.get("MountTargetId")
            return None
        except ClientError:
            return None

    def _get_mount_target_ids(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> List[str]:
        """Get all mount target IDs for a file system."""
        client = self._get_efs_client(region, credential)

        try:
            response = client.describe_mount_targets(FileSystemId=file_system_id)
            return [mt["MountTargetId"] for mt in response.get("MountTargets", [])]
        except ClientError:
            return []

    def delete_mount_targets(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> int:
        """
        Delete all mount targets for a file system.

        Args:
            file_system_id: EFS file system ID
            region: AWS region
            credential: AWS credential

        Returns:
            Number of mount targets deleted
        """
        client = self._get_efs_client(region, credential)
        deleted_count = 0

        try:
            response = client.describe_mount_targets(FileSystemId=file_system_id)
            mount_targets = response.get("MountTargets", [])

            for mt in mount_targets:
                mount_target_id = mt["MountTargetId"]
                try:
                    client.delete_mount_target(MountTargetId=mount_target_id)
                    deleted_count += 1
                    self.log_info(f"Deleted mount target: {mount_target_id}")
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    if error_code != "MountTargetNotFound":
                        self.log_warning(
                            f"Failed to delete mount target {mount_target_id}: {e}"
                        )

            if deleted_count > 0:
                self.notify_observers(
                    "efs_mount_targets_deleted",
                    file_system_id=file_system_id,
                    count=deleted_count,
                )

            return deleted_count

        except ClientError as e:
            raise EFSMountTargetError(
                f"Failed to delete mount targets: {e}", file_system_id=file_system_id
            )

    def wait_for_mount_targets_available(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> bool:
        """
        Wait for all mount targets of a file system to become available.

        Args:
            file_system_id: EFS file system ID
            region: AWS region
            credential: AWS credential
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            True if all mount targets are available

        Raises:
            EFSMountTargetError: If timeout is reached
        """
        client = self._get_efs_client(region, credential)
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = client.describe_mount_targets(FileSystemId=file_system_id)
                mount_targets = response.get("MountTargets", [])

                if not mount_targets:
                    time.sleep(poll_interval)
                    continue

                all_available = all(
                    mt.get("LifeCycleState") == "available" for mt in mount_targets
                )

                if all_available:
                    self.log_info(f"All mount targets available for {file_system_id}")
                    return True

                # Check for errors
                for mt in mount_targets:
                    if mt.get("LifeCycleState") == "error":
                        raise EFSMountTargetError(
                            f"Mount target {mt['MountTargetId']} entered error state",
                            file_system_id=file_system_id,
                            mount_target_id=mt["MountTargetId"],
                        )

                time.sleep(poll_interval)

            except ClientError as e:
                self.log_warning(f"Error polling mount target status: {e}")
                time.sleep(poll_interval)

        raise EFSMountTargetError(
            f"Timeout waiting for mount targets to become available for {file_system_id}",
            file_system_id=file_system_id,
        )

    def _wait_for_no_mount_targets(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> None:
        """Wait for all mount targets to be fully deleted."""
        client = self._get_efs_client(region, credential)
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = client.describe_mount_targets(FileSystemId=file_system_id)
                mount_targets = response.get("MountTargets", [])

                if not mount_targets:
                    return

                # Check if any are still deleting
                deleting = [
                    mt for mt in mount_targets if mt.get("LifeCycleState") == "deleting"
                ]
                if deleting:
                    self.log_debug(
                        f"Waiting for {len(deleting)} mount targets to delete..."
                    )

                time.sleep(poll_interval)

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "FileSystemNotFound":
                    return  # File system already gone
                self.log_warning(f"Error checking mount targets: {e}")
                time.sleep(poll_interval)

        raise EFSMountTargetError(
            f"Timeout waiting for mount targets to be deleted for {file_system_id}",
            file_system_id=file_system_id,
        )

    def describe_mount_targets(
        self,
        file_system_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get details of all mount targets for a file system.

        Args:
            file_system_id: EFS file system ID
            region: AWS region
            credential: AWS credential

        Returns:
            List of mount target dictionaries
        """
        client = self._get_efs_client(region, credential)

        try:
            response = client.describe_mount_targets(FileSystemId=file_system_id)
            return [
                {
                    "mount_target_id": mt.get("MountTargetId"),
                    "file_system_id": mt.get("FileSystemId"),
                    "subnet_id": mt.get("SubnetId"),
                    "life_cycle_state": mt.get("LifeCycleState"),
                    "ip_address": mt.get("IpAddress"),
                    "network_interface_id": mt.get("NetworkInterfaceId"),
                    "availability_zone_id": mt.get("AvailabilityZoneId"),
                    "availability_zone_name": mt.get("AvailabilityZoneName"),
                    "vpc_id": mt.get("VpcId"),
                }
                for mt in response.get("MountTargets", [])
            ]

        except ClientError as e:
            raise EFSMountTargetError(
                f"Failed to describe mount targets: {e}", file_system_id=file_system_id
            )

    # -------------------------------------------------------------------------
    # Security Group Management
    # -------------------------------------------------------------------------

    # remote-compose-jzp: max retries on the find/create race. If the
    # find-existing path consistently misses the SG (permissions error,
    # eventual-consistency lag, etc.) we'd previously recurse until stack
    # overflow. A bounded loop bounds the worst case at MAX_DUPLICATE_
    # RETRIES iterations + a clear error.
    _MAX_DUPLICATE_RETRIES = 3

    def get_or_create_efs_security_group(
        self,
        vpc_id: str,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get or create a security group allowing NFS traffic for EFS.

        Creates a security group that allows inbound NFS (port 2049)
        traffic from the VPC CIDR block.

        Race handling: when create_security_group races against another
        caller and AWS returns InvalidGroup.Duplicate, we re-run the
        find pass — bounded at _MAX_DUPLICATE_RETRIES iterations so a
        broken find path can't recurse indefinitely.

        Args:
            vpc_id: VPC ID
            name: Security group name
            region: AWS region
            credential: AWS credential
            description: Security group description

        Returns:
            Dict with security_group_id, group_name, vpc_id
        """
        ec2 = self._get_ec2_client(region, credential)

        last_duplicate_error: Optional[ClientError] = None
        for attempt in range(self._MAX_DUPLICATE_RETRIES):
            existing = self._find_efs_security_group(ec2, vpc_id, name)
            if existing is not None:
                if attempt > 0:
                    self.log_info(
                        f"Found EFS security group {name!r} after "
                        f"create-race retry {attempt}"
                    )
                return existing
            try:
                return self._create_efs_security_group(
                    ec2,
                    vpc_id,
                    name,
                    region=region,
                    credential=credential,
                    description=description,
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
                    raise EFSError(f"Failed to create EFS security group: {e}")
                # Lost a create race — another caller has the SG; the
                # next iteration's find should pick it up. Cap retries
                # so a broken find path can't loop forever.
                last_duplicate_error = e
                continue
        raise EFSError(
            f"Failed to get_or_create EFS security group {name!r} after "
            f"{self._MAX_DUPLICATE_RETRIES} retries — find/create kept "
            f"racing without converging. Last error: {last_duplicate_error}"
        )

    def _find_efs_security_group(
        self,
        ec2,
        vpc_id: str,
        name: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            response = ec2.describe_security_groups(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "group-name", "Values": [name]},
                ]
            )
        except ClientError as e:
            raise EFSError(f"Failed to search for security group: {e}")
        security_groups = response.get("SecurityGroups", [])
        if not security_groups:
            return None
        sg = security_groups[0]
        self.log_info(f"Found existing EFS security group: {name}")
        return {
            "security_group_id": sg["GroupId"],
            "group_name": sg["GroupName"],
            "vpc_id": sg["VpcId"],
            "description": sg.get("Description"),
        }

    def _create_efs_security_group(
        self,
        ec2,
        vpc_id: str,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        vpc_cidr = self._get_vpc_cidr(vpc_id, region, credential)

        # Create security group. Caller (get_or_create_efs_security_group)
        # catches InvalidGroup.Duplicate and retries the find/create
        # loop — we re-raise here so the bounded retry can do its work.
        response = ec2.create_security_group(
            GroupName=name,
            Description=description or f"EFS security group for {name}",
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": name},
                        {"Key": "CreatedBy", "Value": "remote-compose"},
                    ],
                }
            ],
        )

        security_group_id = response["GroupId"]

        # Add NFS inbound rule.
        ec2.authorize_security_group_ingress(
            GroupId=security_group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "IpRanges": [
                        {
                            "CidrIp": vpc_cidr,
                            "Description": "NFS from VPC",
                        }
                    ],
                }
            ],
        )

        self.log_info(f"Created EFS security group: {name} ({security_group_id})")
        self.notify_observers(
            "efs_security_group_created", security_group_id=security_group_id, name=name
        )
        return {
            "security_group_id": security_group_id,
            "group_name": name,
            "vpc_id": vpc_id,
            "description": description or f"EFS security group for {name}",
        }

    def _get_vpc_cidr(
        self,
        vpc_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """Get the CIDR block for a VPC."""
        ec2 = self._get_ec2_client(region, credential)

        try:
            response = ec2.describe_vpcs(VpcIds=[vpc_id])
            vpcs = response.get("Vpcs", [])

            if not vpcs:
                raise EFSError(f"VPC not found: {vpc_id}")

            return vpcs[0].get("CidrBlock", "10.0.0.0/8")

        except ClientError as e:
            raise EFSError(f"Failed to get VPC CIDR: {e}")

    def authorize_security_group_for_efs(
        self,
        security_group_id: str,
        source_security_group_id: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> bool:
        """
        Authorize a source security group to access EFS through the target security group.

        This allows services in the source security group to mount EFS volumes
        protected by the target security group.

        Args:
            security_group_id: EFS security group ID (target)
            source_security_group_id: Source security group ID (e.g., ECS tasks)
            region: AWS region
            credential: AWS credential

        Returns:
            True if rule was added or already exists
        """
        ec2 = self._get_ec2_client(region, credential)

        try:
            ec2.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 2049,
                        "ToPort": 2049,
                        "UserIdGroupPairs": [
                            {
                                "GroupId": source_security_group_id,
                                "Description": f"NFS from {source_security_group_id}",
                            }
                        ],
                    }
                ],
            )

            self.log_info(
                f"Authorized {source_security_group_id} to access EFS via {security_group_id}"
            )
            return True

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "InvalidPermission.Duplicate":
                self.log_debug(f"Rule already exists for {source_security_group_id}")
                return True
            raise EFSError(f"Failed to authorize security group: {e}")

    # -------------------------------------------------------------------------
    # ECS Volume Configuration
    # -------------------------------------------------------------------------

    def get_ecs_volume_config(
        self,
        file_system_id: str,
        access_point_id: Optional[str] = None,
        root_directory: str = "/",
        volume_name: Optional[str] = None,
        transit_encryption: str = "ENABLED",
        iam_authorization: str = "DISABLED",
    ) -> Dict[str, Any]:
        """
        Get ECS task definition volume configuration for EFS.

        Returns the volume configuration in the format expected by
        ECS task definitions.

        Args:
            file_system_id: EFS file system ID
            access_point_id: Optional access point ID (recommended for per-volume isolation)
            root_directory: Root directory on EFS (default: '/', ignored if access_point_id is set)
            volume_name: Name for the volume (default: derived from file_system_id)
            transit_encryption: ENABLED or DISABLED
            iam_authorization: ENABLED or DISABLED

        Returns:
            Dict with ECS volume configuration
        """
        volume_name = volume_name or f"efs-{file_system_id[-8:]}"

        efs_volume_config = {
            "fileSystemId": file_system_id,
            "transitEncryption": transit_encryption,
        }

        if access_point_id:
            efs_volume_config["accessPointId"] = access_point_id
            # When using access points, root directory must be / or omitted
            efs_volume_config["rootDirectory"] = "/"
            # IAM authorization is required when using access points
            efs_volume_config["authorizationConfig"] = {
                "accessPointId": access_point_id,
                "iam": iam_authorization,
            }
        else:
            efs_volume_config["rootDirectory"] = root_directory

        return {
            "name": volume_name,
            "efsVolumeConfiguration": efs_volume_config,
        }

    def get_ecs_mount_point(
        self,
        volume_name: str,
        container_path: str,
        read_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Get ECS container mount point configuration.

        Returns the mount point configuration in the format expected by
        ECS container definitions.

        Args:
            volume_name: Name of the volume (must match volume configuration)
            container_path: Path inside the container to mount the volume
            read_only: Whether the mount should be read-only

        Returns:
            Dict with container mount point configuration
        """
        return {
            "sourceVolume": volume_name,
            "containerPath": container_path,
            "readOnly": read_only,
        }

    def get_complete_ecs_efs_config(
        self,
        file_system_id: str,
        container_path: str,
        access_point_id: Optional[str] = None,
        volume_name: Optional[str] = None,
        read_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Get complete ECS configuration for mounting an EFS volume.

        Convenience method that returns both the volume configuration
        (for task definition) and mount point (for container definition).

        Args:
            file_system_id: EFS file system ID
            container_path: Path inside the container to mount the volume
            access_point_id: Optional access point ID
            volume_name: Name for the volume
            read_only: Whether the mount should be read-only

        Returns:
            Dict with 'volume' and 'mount_point' configurations
        """
        volume_config = self.get_ecs_volume_config(
            file_system_id=file_system_id,
            access_point_id=access_point_id,
            volume_name=volume_name,
        )

        mount_point = self.get_ecs_mount_point(
            volume_name=volume_config["name"],
            container_path=container_path,
            read_only=read_only,
        )

        return {
            "volume": volume_config,
            "mount_point": mount_point,
        }
