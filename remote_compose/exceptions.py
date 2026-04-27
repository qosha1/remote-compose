"""
Custom exceptions for remote_compose.

Error code ranges (remote-compose-t8j):
  1xxx — Validation
  2xxx — Connection (SSH / network)
  3xxx — Docker
  4xxx — Deployment
  5xxx — Credentials / encryption
  6xxx — AWS (generic)
  7xxx — ECS
  8xxx — ECR
  9xxx — EFS

Each subclass declares ``code = NNNN`` as a class attribute. The base
class __init__ falls back to it when ``code=`` isn't passed explicitly,
so every raised exception carries a stable per-type code. Tests
(test_exception_code_ranges.py) verify each class has a code in its
declared range — drift gets caught before runtime.
"""


class RemoteComposeError(Exception):
    """Base exception for all remote_compose errors."""

    code = None  # subclasses override

    def __init__(self, message, code=None, details=None):
        self.message = message
        # remote-compose-t8j: when code isn't passed, use the class
        # default. This makes every raised instance carry a stable
        # per-type code without each call site having to repeat it.
        self.code = code if code is not None else self.__class__.code
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self):
        if self.code:
            return f"[{self.code}] {self.message}"
        return self.message


# Validation Errors (1000-1999)
class ValidationError(RemoteComposeError):
    """Raised when validation fails."""
    code = 1000


class ConfigurationError(ValidationError):
    """Raised when configuration is invalid."""
    code = 1001


# Connection Errors (2000-2999)
class RemoteConnectionError(RemoteComposeError):
    """Raised when connection to a remote host (SSH / TCP / HTTP) fails.

    Renamed from ``ConnectionError`` (remote-compose-7fn) — the old name
    shadowed Python's builtin ``ConnectionError``, so any
    ``except ConnectionError`` in this codebase silently caught network
    OS-level errors (refused / reset / aborted) as if they were rc-domain
    errors. New code should always use this name.
    """
    code = 2000


class SSHConnectionError(RemoteConnectionError):
    """Raised when SSH connection fails."""
    code = 2001

    def __init__(self, message, host=None, port=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['host'] = host
        self.details['port'] = port


class SSHAuthenticationError(SSHConnectionError):
    """Raised when SSH authentication fails."""
    code = 2002


class SSHTimeoutError(SSHConnectionError):
    """Raised when SSH connection times out."""
    code = 2003


class SSHHostKeyError(SSHConnectionError):
    """Raised when SSH host key verification fails."""
    code = 2004


# Docker Errors (3000-3999)
class DockerError(RemoteComposeError):
    """Base class for Docker-related errors."""
    code = 3000


class DockerContextError(DockerError):
    """Raised when Docker context operations fail."""
    code = 3001


class DockerComposeError(DockerError):
    """Raised when docker-compose operations fail."""
    code = 3002


class ComposeFileError(DockerComposeError):
    """Raised when compose file is invalid or missing."""
    code = 3003


# Deployment Errors (4000-4999)
class DeploymentError(RemoteComposeError):
    """Raised when deployment execution fails."""
    code = 4000


class DeploymentTimeoutError(DeploymentError):
    """Raised when deployment times out."""
    code = 4001


class RollbackError(DeploymentError):
    """Raised when rollback fails."""
    code = 4002


class DeploymentInProgressError(DeploymentError):
    """Raised when attempting concurrent deployments on same target."""
    code = 4003


# Credential Errors (5000-5999)
class CredentialError(RemoteComposeError):
    """Raised when credential operations fail."""
    code = 5000


class EncryptionError(CredentialError):
    """Raised when encryption/decryption fails."""
    code = 5001


# AWS Errors (6000-6999)
class AWSError(RemoteComposeError):
    """Raised when AWS operations fail."""
    code = 6000


class EC2Error(AWSError):
    """Raised when EC2 operations fail."""
    code = 6001


class AWSCredentialError(AWSError):
    """Raised when AWS credentials are invalid."""
    code = 6002


# ECS Errors (7000-7999)
class ECSError(AWSError):
    """Base class for ECS-related errors."""
    code = 7000


class ECSClusterError(ECSError):
    """Raised when ECS cluster operations fail."""
    code = 7001

    def __init__(self, message, cluster_name=None, region=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['cluster_name'] = cluster_name
        self.details['region'] = region


class ECSClusterNotFoundError(ECSClusterError):
    """Raised when ECS cluster is not found."""
    code = 7002


class ECSServiceError(ECSError):
    """Raised when ECS service operations fail."""
    code = 7003

    def __init__(self, message, service_name=None, cluster_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['service_name'] = service_name
        self.details['cluster_name'] = cluster_name


class ECSServiceNotFoundError(ECSServiceError):
    """Raised when ECS service is not found."""
    code = 7004


class ECSTaskDefinitionError(ECSError):
    """Raised when task definition operations fail."""
    code = 7005

    def __init__(self, message, task_definition=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['task_definition'] = task_definition


class ECSTaskError(ECSError):
    """Raised when ECS task operations fail."""
    code = 7006

    def __init__(self, message, task_arn=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['task_arn'] = task_arn


class ECSDeploymentError(ECSError):
    """Raised when ECS deployment fails."""
    code = 7007


class ECSDeploymentTimeoutError(ECSDeploymentError):
    """Raised when ECS deployment times out waiting for stability."""
    code = 7008


class ComposeConversionError(ECSError):
    """Raised when converting docker-compose to ECS format fails."""
    code = 7009

    def __init__(self, message, compose_file=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['compose_file'] = compose_file


# ECR Errors (8000-8999)
class ECRError(AWSError):
    """Base class for ECR-related errors."""
    code = 8000


class ECRRepositoryError(ECRError):
    """Raised when ECR repository operations fail."""
    code = 8001

    def __init__(self, message, repository_name=None, region=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['repository_name'] = repository_name
        self.details['region'] = region


class ECRAuthenticationError(ECRError):
    """Raised when ECR authentication fails."""
    code = 8002

    def __init__(self, message, region=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['region'] = region


class ECRImageError(ECRError):
    """Raised when ECR image operations fail."""
    code = 8003

    def __init__(self, message, repository_name=None, image_tag=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['repository_name'] = repository_name
        self.details['image_tag'] = image_tag


# EFS Errors (9000-9999)
class EFSError(AWSError):
    """Base class for EFS-related errors."""
    code = 9000


class EFSFileSystemError(EFSError):
    """Raised when EFS file system operations fail."""
    code = 9001

    def __init__(self, message, file_system_id=None, file_system_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['file_system_id'] = file_system_id
        self.details['file_system_name'] = file_system_name


class EFSAccessPointError(EFSError):
    """Raised when EFS access point operations fail."""
    code = 9002

    def __init__(self, message, access_point_id=None, file_system_id=None, path=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['access_point_id'] = access_point_id
        self.details['file_system_id'] = file_system_id
        self.details['path'] = path


class EFSMountTargetError(EFSError):
    """Raised when EFS mount target operations fail."""
    code = 9003

    def __init__(self, message, file_system_id=None, mount_target_id=None, subnet_id=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['file_system_id'] = file_system_id
        self.details['mount_target_id'] = mount_target_id
        self.details['subnet_id'] = subnet_id


# VPC Errors (10000-10999)
class VPCError(AWSError):
    """Base class for VPC-related errors."""
    code = 10000


class VPCProvisioningError(VPCError):
    """Raised when VPC provisioning fails."""
    code = 10001

    def __init__(self, message, vpc_id=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['vpc_id'] = vpc_id


class SubnetError(VPCError):
    """Raised when subnet operations fail."""
    code = 10002


# ALB Errors (11000-11999)
class ALBError(AWSError):
    """Base class for Application Load Balancer errors."""
    code = 11000


class ALBProvisioningError(ALBError):
    """Raised when ALB provisioning fails."""
    code = 11001

    def __init__(self, message, alb_arn=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['alb_arn'] = alb_arn


class TargetGroupError(ALBError):
    """Raised when target group operations fail."""
    code = 11002

    def __init__(self, message, target_group_arn=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['target_group_arn'] = target_group_arn


# Secrets Manager Errors (12000-12999)
class SecretsManagerError(AWSError):
    """Base class for Secrets Manager errors."""
    code = 12000


class SecretProvisioningError(SecretsManagerError):
    """Raised when secret creation/update fails."""
    code = 12001

    def __init__(self, message, secret_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['secret_name'] = secret_name


# Service Connect Errors (13000-13999)
class ServiceConnectError(AWSError):
    """Base class for Service Connect errors."""
    code = 13000


class NamespaceError(ServiceConnectError):
    """Raised when Cloud Map namespace operations fail."""
    code = 13001

    def __init__(self, message, namespace_name=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['namespace_name'] = namespace_name


# Security Group Errors (14000-14999)
class SecurityGroupError(AWSError):
    """Base class for Security Group errors."""
    code = 14000


class SecurityGroupProvisioningError(SecurityGroupError):
    """Raised when security group creation/configuration fails."""
    code = 14001

    def __init__(self, message, security_group_id=None, **kwargs):
        super().__init__(message, **kwargs)
        self.details['security_group_id'] = security_group_id
