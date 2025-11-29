"""
Custom exceptions for remote_compose.
"""


class RemoteComposeError(Exception):
    """Base exception for all remote_compose errors."""

    def __init__(self, message, code=None, details=None):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self):
        if self.code:
            return f"[{self.code}] {self.message}"
        return self.message


# Connection Errors (2000-2999)
class ConnectionError(RemoteComposeError):
    """Raised when connection to remote host fails."""
    pass


class SSHConnectionError(ConnectionError):
    """Raised when SSH connection fails."""

    def __init__(self, message, host=None, port=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['host'] = host
        self.details['port'] = port


class SSHAuthenticationError(SSHConnectionError):
    """Raised when SSH authentication fails."""
    pass


class SSHTimeoutError(SSHConnectionError):
    """Raised when SSH connection times out."""
    pass


class SSHHostKeyError(SSHConnectionError):
    """Raised when SSH host key verification fails."""
    pass


# Docker Errors (3000-3999)
class DockerError(RemoteComposeError):
    """Base class for Docker-related errors."""
    pass


class DockerContextError(DockerError):
    """Raised when Docker context operations fail."""
    pass


class DockerComposeError(DockerError):
    """Raised when docker-compose operations fail."""
    pass


class ComposeFileError(DockerComposeError):
    """Raised when compose file is invalid or missing."""
    pass


# Deployment Errors (4000-4999)
class DeploymentError(RemoteComposeError):
    """Raised when deployment execution fails."""
    pass


class DeploymentTimeoutError(DeploymentError):
    """Raised when deployment times out."""
    pass


class RollbackError(DeploymentError):
    """Raised when rollback fails."""
    pass


class DeploymentInProgressError(DeploymentError):
    """Raised when attempting concurrent deployments on same target."""
    pass


# Validation Errors (1000-1999)
class ValidationError(RemoteComposeError):
    """Raised when validation fails."""
    pass


class ConfigurationError(ValidationError):
    """Raised when configuration is invalid."""
    pass


# Credential Errors (5000-5999)
class CredentialError(RemoteComposeError):
    """Raised when credential operations fail."""
    pass


class EncryptionError(CredentialError):
    """Raised when encryption/decryption fails."""
    pass


# AWS Errors (6000-6999)
class AWSError(RemoteComposeError):
    """Raised when AWS operations fail."""
    pass


class EC2Error(AWSError):
    """Raised when EC2 operations fail."""
    pass


class AWSCredentialError(AWSError):
    """Raised when AWS credentials are invalid."""
    pass


# ECS Errors (7000-7999)
class ECSError(AWSError):
    """Base class for ECS-related errors."""
    pass


class ECSClusterError(ECSError):
    """Raised when ECS cluster operations fail."""

    def __init__(self, message, cluster_name=None, region=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['cluster_name'] = cluster_name
        self.details['region'] = region


class ECSClusterNotFoundError(ECSClusterError):
    """Raised when ECS cluster is not found."""
    pass


class ECSServiceError(ECSError):
    """Raised when ECS service operations fail."""

    def __init__(self, message, service_name=None, cluster_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['service_name'] = service_name
        self.details['cluster_name'] = cluster_name


class ECSServiceNotFoundError(ECSServiceError):
    """Raised when ECS service is not found."""
    pass


class ECSTaskDefinitionError(ECSError):
    """Raised when task definition operations fail."""

    def __init__(self, message, task_definition=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['task_definition'] = task_definition


class ECSTaskError(ECSError):
    """Raised when ECS task operations fail."""

    def __init__(self, message, task_arn=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['task_arn'] = task_arn


class ECSDeploymentError(ECSError):
    """Raised when ECS deployment fails."""
    pass


class ECSDeploymentTimeoutError(ECSDeploymentError):
    """Raised when ECS deployment times out waiting for stability."""
    pass


class ComposeConversionError(ECSError):
    """Raised when converting docker-compose to ECS format fails."""

    def __init__(self, message, compose_file=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['compose_file'] = compose_file


# ECR Errors (8000-8999)
class ECRError(AWSError):
    """Base class for ECR-related errors."""
    pass


class ECRRepositoryError(ECRError):
    """Raised when ECR repository operations fail."""

    def __init__(self, message, repository_name=None, region=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['repository_name'] = repository_name
        self.details['region'] = region


class ECRAuthenticationError(ECRError):
    """Raised when ECR authentication fails."""

    def __init__(self, message, region=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['region'] = region


class ECRImageError(ECRError):
    """Raised when ECR image operations fail."""

    def __init__(self, message, repository_name=None, image_tag=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['repository_name'] = repository_name
        self.details['image_tag'] = image_tag


# EFS Errors (9000-9999)
class EFSError(AWSError):
    """Base class for EFS-related errors."""
    pass


class EFSFileSystemError(EFSError):
    """Raised when EFS file system operations fail."""

    def __init__(self, message, file_system_id=None, file_system_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['file_system_id'] = file_system_id
        self.details['file_system_name'] = file_system_name


class EFSAccessPointError(EFSError):
    """Raised when EFS access point operations fail."""

    def __init__(self, message, access_point_id=None, file_system_id=None, path=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['access_point_id'] = access_point_id
        self.details['file_system_id'] = file_system_id
        self.details['path'] = path


class EFSMountTargetError(EFSError):
    """Raised when EFS mount target operations fail."""

    def __init__(self, message, file_system_id=None, mount_target_id=None, subnet_id=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['file_system_id'] = file_system_id
        self.details['mount_target_id'] = mount_target_id
        self.details['subnet_id'] = subnet_id


# VPC Errors (10000-10999)
class VPCError(AWSError):
    """Base class for VPC-related errors."""
    pass


class VPCProvisioningError(VPCError):
    """Raised when VPC provisioning fails."""

    def __init__(self, message, vpc_id=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['vpc_id'] = vpc_id


class SubnetError(VPCError):
    """Raised when subnet operations fail."""
    pass


# ALB Errors (11000-11999)
class ALBError(AWSError):
    """Base class for Application Load Balancer errors."""
    pass


class ALBProvisioningError(ALBError):
    """Raised when ALB provisioning fails."""

    def __init__(self, message, alb_arn=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['alb_arn'] = alb_arn


class TargetGroupError(ALBError):
    """Raised when target group operations fail."""

    def __init__(self, message, target_group_arn=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['target_group_arn'] = target_group_arn


# Secrets Manager Errors (12000-12999)
class SecretsManagerError(AWSError):
    """Base class for Secrets Manager errors."""
    pass


class SecretProvisioningError(SecretsManagerError):
    """Raised when secret creation/update fails."""

    def __init__(self, message, secret_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['secret_name'] = secret_name


# Service Connect Errors (13000-13999)
class ServiceConnectError(AWSError):
    """Base class for Service Connect errors."""
    pass


class NamespaceError(ServiceConnectError):
    """Raised when Cloud Map namespace operations fail."""

    def __init__(self, message, namespace_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['namespace_name'] = namespace_name


# Security Group Errors (14000-14999)
class SecurityGroupError(AWSError):
    """Base class for Security Group errors."""
    pass


class SecurityGroupProvisioningError(SecurityGroupError):
    """Raised when security group creation/configuration fails."""

    def __init__(self, message, security_group_id=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['security_group_id'] = security_group_id
