"""
Deployment pipeline context for passing state between steps.

The context is the single source of truth for all pipeline state,
eliminating massive parameter lists and providing clean state management.

Fields are organized into typed sub-context dataclasses:
- DeploymentConfig: Immutable deployment configuration and behavioral flags
- InfrastructureState: Mutable infrastructure provisioning state (VPC, ALB, SGs, etc.)
- ImageState: Mutable image build/push state (ECR repos, built images)
- EFSState: Mutable EFS provisioning state (file systems, access points)

Top-level PipelineContext retains required inputs, deployment tracking,
preprocessing results, ECS state, multi-service state, and observability.

Backwards-compatible @property accessors delegate to sub-contexts so
existing step code (context.vpc_cidr, context.built_images, etc.) continues
to work unchanged.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from ...models import (
        ECSCluster,
        ECSTaskDefinition,
        ECSService,
        Deployment,
        DeploymentTarget,
        EFSFileSystem,
        VPCInfrastructure,
        LoadBalancerConfig,
        ServiceConnectNamespace,
    )
    from ..compose_preprocessor import PreprocessedCompose


# ---------------------------------------------------------------------------
# Sub-context dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DeploymentConfig:
    """
    Immutable deployment configuration and behavioral flags.

    Set at pipeline start and typically not mutated during execution.
    Covers image settings, resource sizing, behavioral toggles,
    environment inputs, and optional configuration paths.
    """

    # Image configuration
    image_tag: str = 'latest'
    version: str = ''
    deployed_by: str = 'system'

    # Resource configuration
    desired_count: int = 1
    cpu: Optional[str] = None
    memory: Optional[str] = None

    # Behavioral flags
    build_images: bool = True
    force_rebuild: bool = False
    push_images: bool = True
    create_efs_for_volumes: bool = True
    wait_for_stable: bool = True
    timeout: int = 300
    strict_mode: bool = False
    dry_run: bool = False

    # Optional inputs
    env_files: Optional[List[str]] = None
    environment: Optional[Dict[str, str]] = None

    # Service config file path (YAML with per-service overrides)
    service_config_path: Optional[str] = None

    # Certificate ARN for HTTPS
    certificate_arn: Optional[str] = None

    # VPC CIDR override
    vpc_cidr: str = '10.0.0.0/16'

    # Secrets/env files
    secrets_files: List[str] = field(default_factory=list)

    # Domain for ALB/Route53
    domain: Optional[str] = None

    # Code-only deployment (skip infra services like postgres, redis)
    code_only: bool = False

    # Subset of services to deploy (None = all)
    selected_services: Optional[List[str]] = None


@dataclass
class InfrastructureState:
    """
    Mutable infrastructure provisioning state.

    Populated during the infrastructure pipeline steps (VPC, security
    groups, ALB, Service Connect, secrets). Read by later ECS steps.
    """

    # VPC
    vpc_infrastructure: Optional['VPCInfrastructure'] = None
    security_groups: Dict[str, str] = field(default_factory=dict)

    # Load balancer
    load_balancer: Optional['LoadBalancerConfig'] = None
    target_groups: Dict[str, Any] = field(default_factory=dict)

    # Service Connect
    service_connect_namespace: Optional['ServiceConnectNamespace'] = None

    # Secrets Manager ARNs: {env_var_name: secret_arn}
    secrets_arns: Dict[str, str] = field(default_factory=dict)


@dataclass
class ImageState:
    """
    Mutable image build/push state.

    Populated during ECR and build steps. Tracks repositories,
    built image URIs, and shared build-context deduplication.
    """

    # AWS account ID (resolved during preprocessing)
    account_id: Optional[str] = None

    # ECR repositories: {service_name: repo_info_dict}
    ecr_repositories: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Successfully built image URIs
    built_images: List[str] = field(default_factory=list)

    # Shared images: {build_context_key: ecr_uri}
    shared_images: Dict[str, str] = field(default_factory=dict)


@dataclass
class EFSState:
    """
    Mutable EFS provisioning state.

    Populated during the EFS setup step. Maps named volumes to
    EFS file system IDs and access point IDs.
    """

    # EFS file system model
    efs_file_system: Optional['EFSFileSystem'] = None

    # Per-volume EFS config: {volume_name: {file_system_id, access_point_id}}
    efs_config: Dict[str, Dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Service registry for dependency injection
# ---------------------------------------------------------------------------

class ServiceRegistry:
    """
    Lazy service factory for pipeline dependency injection.

    Services are created on first access and cached for the pipeline run.
    Use set() to override services for testing.
    """

    def __init__(self):
        self._cache: Dict[str, Any] = {}

    def set(self, name: str, instance) -> None:
        """Override a service instance (for testing)."""
        self._cache[name] = instance

    def _get_or_create(self, name: str, factory):
        if name not in self._cache:
            self._cache[name] = factory()
        return self._cache[name]

    @property
    def ecs(self):
        def factory():
            from ..ecs_service import ECSService
            return ECSService()
        return self._get_or_create('ecs', factory)

    @property
    def ecr(self):
        def factory():
            from ..ecr_service import ECRService
            return ECRService()
        return self._get_or_create('ecr', factory)

    @property
    def efs(self):
        def factory():
            from ..efs_service import EFSService
            return EFSService()
        return self._get_or_create('efs', factory)

    @property
    def vpc(self):
        def factory():
            from ..vpc_service import VPCService
            return VPCService()
        return self._get_or_create('vpc', factory)

    @property
    def alb(self):
        def factory():
            from ..alb_service import ALBService
            return ALBService()
        return self._get_or_create('alb', factory)

    @property
    def security_group(self):
        def factory():
            from ..security_group_service import SecurityGroupService
            return SecurityGroupService()
        return self._get_or_create('security_group', factory)

    @property
    def service_connect(self):
        def factory():
            from ..service_connect_service import ServiceConnectService
            return ServiceConnectService()
        return self._get_or_create('service_connect', factory)

    @property
    def secrets(self):
        def factory():
            from ..secrets_service import SecretsService
            return SecretsService()
        return self._get_or_create('secrets', factory)

    @property
    def image_build(self):
        def factory():
            from ..image_build_service import ImageBuildService
            return ImageBuildService(ecr_service=self.ecr)
        return self._get_or_create('image_build', factory)


# ---------------------------------------------------------------------------
# Main pipeline context
# ---------------------------------------------------------------------------

@dataclass
class PipelineContext:
    """
    Shared context passed through deployment pipeline steps.

    Contains all input parameters and accumulated state from each step.
    Each step can read from and write to this context.

    Fields are grouped into sub-context objects for organization:
    - config: DeploymentConfig (immutable settings)
    - infrastructure: InfrastructureState (VPC, ALB, SGs, secrets)
    - images: ImageState (ECR, built images)
    - efs: EFSState (EFS file systems, access points)

    Backwards-compatible @property accessors are provided so existing
    step code (e.g. context.vpc_cidr) continues to work. New code
    should prefer context.config.vpc_cidr, context.images.built_images, etc.
    """

    # -------------------------------------------------------------------------
    # Required Inputs (set at pipeline start)
    # -------------------------------------------------------------------------

    cluster: 'ECSCluster'
    compose_file_path: Path
    project_name: str

    # -------------------------------------------------------------------------
    # Sub-Contexts
    # -------------------------------------------------------------------------

    config: DeploymentConfig = field(default_factory=DeploymentConfig)
    infrastructure: InfrastructureState = field(default_factory=InfrastructureState)
    images: ImageState = field(default_factory=ImageState)
    efs: EFSState = field(default_factory=EFSState)
    services: ServiceRegistry = field(default_factory=ServiceRegistry)

    # -------------------------------------------------------------------------
    # Optional Input
    # -------------------------------------------------------------------------

    target: Optional['DeploymentTarget'] = None

    # -------------------------------------------------------------------------
    # State Accumulated During Pipeline Execution
    # -------------------------------------------------------------------------

    # Deployment tracking
    deployment: Optional['Deployment'] = None

    # Preprocessing results
    preprocessed: Optional['PreprocessedCompose'] = None

    # ECS state (single-service)
    task_definition: Optional['ECSTaskDefinition'] = None
    ecs_service: Optional['ECSService'] = None

    # -------------------------------------------------------------------------
    # Multi-Service State
    # -------------------------------------------------------------------------

    # Per-service task definitions: {service_name: ECSTaskDefinition}
    task_definitions: Dict[str, Any] = field(default_factory=dict)

    # Per-service ECS services: {service_name: ECSService}
    ecs_services: Dict[str, Any] = field(default_factory=dict)

    # Topologically sorted service deployment order
    service_order: List[str] = field(default_factory=list)

    # Per-service resource overrides from service config YAML
    service_resources: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Per-service desired count
    service_counts: Dict[str, int] = field(default_factory=dict)

    # Public services: {service_name: {port, health_check_path, default_target}}
    public_services: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Infrastructure env vars injected into task definitions
    infrastructure_env: Dict[str, str] = field(default_factory=dict)

    # -------------------------------------------------------------------------
    # Tracking and Observability
    # -------------------------------------------------------------------------

    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Resources created (for cleanup tracking)
    created_resources: List[Dict[str, Any]] = field(default_factory=list)

    # =========================================================================
    # Backwards-compatible property accessors for DeploymentConfig fields
    # =========================================================================

    @property
    def image_tag(self) -> str:
        return self.config.image_tag

    @image_tag.setter
    def image_tag(self, value: str) -> None:
        self.config.image_tag = value

    @property
    def version(self) -> str:
        return self.config.version

    @version.setter
    def version(self, value: str) -> None:
        self.config.version = value

    @property
    def deployed_by(self) -> str:
        return self.config.deployed_by

    @deployed_by.setter
    def deployed_by(self, value: str) -> None:
        self.config.deployed_by = value

    @property
    def desired_count(self) -> int:
        return self.config.desired_count

    @desired_count.setter
    def desired_count(self, value: int) -> None:
        self.config.desired_count = value

    @property
    def cpu(self) -> Optional[str]:
        return self.config.cpu

    @cpu.setter
    def cpu(self, value: Optional[str]) -> None:
        self.config.cpu = value

    @property
    def memory(self) -> Optional[str]:
        return self.config.memory

    @memory.setter
    def memory(self, value: Optional[str]) -> None:
        self.config.memory = value

    @property
    def build_images(self) -> bool:
        return self.config.build_images

    @build_images.setter
    def build_images(self, value: bool) -> None:
        self.config.build_images = value

    @property
    def force_rebuild(self) -> bool:
        return self.config.force_rebuild

    @force_rebuild.setter
    def force_rebuild(self, value: bool) -> None:
        self.config.force_rebuild = value

    @property
    def push_images(self) -> bool:
        return self.config.push_images

    @push_images.setter
    def push_images(self, value: bool) -> None:
        self.config.push_images = value

    @property
    def create_efs_for_volumes(self) -> bool:
        return self.config.create_efs_for_volumes

    @create_efs_for_volumes.setter
    def create_efs_for_volumes(self, value: bool) -> None:
        self.config.create_efs_for_volumes = value

    @property
    def wait_for_stable(self) -> bool:
        return self.config.wait_for_stable

    @wait_for_stable.setter
    def wait_for_stable(self, value: bool) -> None:
        self.config.wait_for_stable = value

    @property
    def timeout(self) -> int:
        return self.config.timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        self.config.timeout = value

    @property
    def strict_mode(self) -> bool:
        return self.config.strict_mode

    @strict_mode.setter
    def strict_mode(self, value: bool) -> None:
        self.config.strict_mode = value

    @property
    def dry_run(self) -> bool:
        return self.config.dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self.config.dry_run = value

    @property
    def env_files(self) -> Optional[List[str]]:
        return self.config.env_files

    @env_files.setter
    def env_files(self, value: Optional[List[str]]) -> None:
        self.config.env_files = value

    @property
    def environment(self) -> Optional[Dict[str, str]]:
        return self.config.environment

    @environment.setter
    def environment(self, value: Optional[Dict[str, str]]) -> None:
        self.config.environment = value

    @property
    def service_config_path(self) -> Optional[str]:
        return self.config.service_config_path

    @service_config_path.setter
    def service_config_path(self, value: Optional[str]) -> None:
        self.config.service_config_path = value

    @property
    def certificate_arn(self) -> Optional[str]:
        return self.config.certificate_arn

    @certificate_arn.setter
    def certificate_arn(self, value: Optional[str]) -> None:
        self.config.certificate_arn = value

    @property
    def vpc_cidr(self) -> str:
        return self.config.vpc_cidr

    @vpc_cidr.setter
    def vpc_cidr(self, value: str) -> None:
        self.config.vpc_cidr = value

    @property
    def secrets_files(self) -> List[str]:
        return self.config.secrets_files

    @secrets_files.setter
    def secrets_files(self, value: List[str]) -> None:
        self.config.secrets_files = value

    @property
    def domain(self) -> Optional[str]:
        return self.config.domain

    @domain.setter
    def domain(self, value: Optional[str]) -> None:
        self.config.domain = value

    @property
    def code_only(self) -> bool:
        return self.config.code_only

    @code_only.setter
    def code_only(self, value: bool) -> None:
        self.config.code_only = value

    @property
    def selected_services(self) -> Optional[List[str]]:
        return self.config.selected_services

    @selected_services.setter
    def selected_services(self, value: Optional[List[str]]) -> None:
        self.config.selected_services = value

    # =========================================================================
    # Backwards-compatible property accessors for InfrastructureState fields
    # =========================================================================

    @property
    def vpc_infrastructure(self) -> Optional['VPCInfrastructure']:
        return self.infrastructure.vpc_infrastructure

    @vpc_infrastructure.setter
    def vpc_infrastructure(self, value: Optional['VPCInfrastructure']) -> None:
        self.infrastructure.vpc_infrastructure = value

    @property
    def security_groups(self) -> Dict[str, str]:
        return self.infrastructure.security_groups

    @security_groups.setter
    def security_groups(self, value: Dict[str, str]) -> None:
        self.infrastructure.security_groups = value

    @property
    def load_balancer(self) -> Optional['LoadBalancerConfig']:
        return self.infrastructure.load_balancer

    @load_balancer.setter
    def load_balancer(self, value: Optional['LoadBalancerConfig']) -> None:
        self.infrastructure.load_balancer = value

    @property
    def target_groups(self) -> Dict[str, Any]:
        return self.infrastructure.target_groups

    @target_groups.setter
    def target_groups(self, value: Dict[str, Any]) -> None:
        self.infrastructure.target_groups = value

    @property
    def service_connect_namespace(self) -> Optional['ServiceConnectNamespace']:
        return self.infrastructure.service_connect_namespace

    @service_connect_namespace.setter
    def service_connect_namespace(self, value: Optional['ServiceConnectNamespace']) -> None:
        self.infrastructure.service_connect_namespace = value

    @property
    def secrets_arns(self) -> Dict[str, str]:
        return self.infrastructure.secrets_arns

    @secrets_arns.setter
    def secrets_arns(self, value: Dict[str, str]) -> None:
        self.infrastructure.secrets_arns = value

    # =========================================================================
    # Backwards-compatible property accessors for ImageState fields
    # =========================================================================

    @property
    def account_id(self) -> Optional[str]:
        return self.images.account_id

    @account_id.setter
    def account_id(self, value: Optional[str]) -> None:
        self.images.account_id = value

    @property
    def ecr_repositories(self) -> Dict[str, Dict[str, Any]]:
        return self.images.ecr_repositories

    @ecr_repositories.setter
    def ecr_repositories(self, value: Dict[str, Dict[str, Any]]) -> None:
        self.images.ecr_repositories = value

    @property
    def built_images(self) -> List[str]:
        return self.images.built_images

    @built_images.setter
    def built_images(self, value: List[str]) -> None:
        self.images.built_images = value

    @property
    def shared_images(self) -> Dict[str, str]:
        return self.images.shared_images

    @shared_images.setter
    def shared_images(self, value: Dict[str, str]) -> None:
        self.images.shared_images = value

    # =========================================================================
    # Backwards-compatible property accessors for EFSState fields
    # =========================================================================

    @property
    def efs_file_system(self) -> Optional['EFSFileSystem']:
        return self.efs.efs_file_system

    @efs_file_system.setter
    def efs_file_system(self, value: Optional['EFSFileSystem']) -> None:
        self.efs.efs_file_system = value

    @property
    def efs_config(self) -> Dict[str, Dict[str, str]]:
        return self.efs.efs_config

    @efs_config.setter
    def efs_config(self, value: Dict[str, Dict[str, str]]) -> None:
        self.efs.efs_config = value

    # =========================================================================
    # Custom __init__ for backwards-compatible construction
    # =========================================================================

    def __init__(
        self,
        cluster: 'ECSCluster',
        compose_file_path: Path,
        project_name: str,
        # DeploymentConfig fields (accepted at top level for backwards compat)
        image_tag: str = 'latest',
        version: str = '',
        deployed_by: str = 'system',
        desired_count: int = 1,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
        build_images: bool = True,
        force_rebuild: bool = False,
        push_images: bool = True,
        create_efs_for_volumes: bool = True,
        wait_for_stable: bool = True,
        timeout: int = 300,
        strict_mode: bool = False,
        dry_run: bool = False,
        env_files: Optional[List[str]] = None,
        environment: Optional[Dict[str, str]] = None,
        service_config_path: Optional[str] = None,
        certificate_arn: Optional[str] = None,
        vpc_cidr: str = '10.0.0.0/16',
        secrets_files: Optional[List[str]] = None,
        domain: Optional[str] = None,
        code_only: bool = False,
        selected_services: Optional[List[str]] = None,
        # InfrastructureState fields (accepted at top level for backwards compat)
        vpc_infrastructure: Optional['VPCInfrastructure'] = None,
        security_groups: Optional[Dict[str, str]] = None,
        load_balancer: Optional['LoadBalancerConfig'] = None,
        target_groups: Optional[Dict[str, Any]] = None,
        service_connect_namespace: Optional['ServiceConnectNamespace'] = None,
        secrets_arns: Optional[Dict[str, str]] = None,
        # ImageState fields (accepted at top level for backwards compat)
        account_id: Optional[str] = None,
        ecr_repositories: Optional[Dict[str, Dict[str, Any]]] = None,
        built_images: Optional[List[str]] = None,
        shared_images: Optional[Dict[str, str]] = None,
        # EFSState fields (accepted at top level for backwards compat)
        efs_file_system: Optional['EFSFileSystem'] = None,
        efs_config: Optional[Dict[str, Dict[str, str]]] = None,
        # Sub-context objects (for new-style construction)
        config: Optional[DeploymentConfig] = None,
        infrastructure: Optional[InfrastructureState] = None,
        images: Optional[ImageState] = None,
        efs: Optional[EFSState] = None,
        services: Optional[ServiceRegistry] = None,
        # Top-level fields
        target: Optional['DeploymentTarget'] = None,
        deployment: Optional['Deployment'] = None,
        preprocessed: Optional['PreprocessedCompose'] = None,
        task_definition: Optional['ECSTaskDefinition'] = None,
        ecs_service: Optional['ECSService'] = None,
        task_definitions: Optional[Dict[str, Any]] = None,
        ecs_services: Optional[Dict[str, Any]] = None,
        service_order: Optional[List[str]] = None,
        service_resources: Optional[Dict[str, Dict[str, Any]]] = None,
        service_counts: Optional[Dict[str, int]] = None,
        public_services: Optional[Dict[str, Dict[str, Any]]] = None,
        infrastructure_env: Optional[Dict[str, str]] = None,
        warnings: Optional[List[str]] = None,
        errors: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_resources: Optional[List[Dict[str, Any]]] = None,
    ):
        # Required inputs
        self.cluster = cluster
        self.compose_file_path = compose_file_path
        self.project_name = project_name

        # Build DeploymentConfig (prefer passed sub-context, overlay flat args)
        self.config = config or DeploymentConfig(
            image_tag=image_tag,
            version=version,
            deployed_by=deployed_by,
            desired_count=desired_count,
            cpu=cpu,
            memory=memory,
            build_images=build_images,
            force_rebuild=force_rebuild,
            push_images=push_images,
            create_efs_for_volumes=create_efs_for_volumes,
            wait_for_stable=wait_for_stable,
            timeout=timeout,
            strict_mode=strict_mode,
            dry_run=dry_run,
            env_files=env_files,
            environment=environment,
            service_config_path=service_config_path,
            certificate_arn=certificate_arn,
            vpc_cidr=vpc_cidr,
            secrets_files=secrets_files if secrets_files is not None else [],
            domain=domain,
            code_only=code_only,
            selected_services=selected_services,
        )

        # Build InfrastructureState
        self.infrastructure = infrastructure or InfrastructureState(
            vpc_infrastructure=vpc_infrastructure,
            security_groups=security_groups if security_groups is not None else {},
            load_balancer=load_balancer,
            target_groups=target_groups if target_groups is not None else {},
            service_connect_namespace=service_connect_namespace,
            secrets_arns=secrets_arns if secrets_arns is not None else {},
        )

        # Build ImageState
        self.images = images or ImageState(
            account_id=account_id,
            ecr_repositories=ecr_repositories if ecr_repositories is not None else {},
            built_images=built_images if built_images is not None else [],
            shared_images=shared_images if shared_images is not None else {},
        )

        # Build EFSState
        self.efs = efs or EFSState(
            efs_file_system=efs_file_system,
            efs_config=efs_config if efs_config is not None else {},
        )

        # Service registry
        self.services = services or ServiceRegistry()

        # Top-level fields
        self.target = target
        self.deployment = deployment
        self.preprocessed = preprocessed
        self.task_definition = task_definition
        self.ecs_service = ecs_service
        self.task_definitions = task_definitions if task_definitions is not None else {}
        self.ecs_services = ecs_services if ecs_services is not None else {}
        self.service_order = service_order if service_order is not None else []
        self.service_resources = service_resources if service_resources is not None else {}
        self.service_counts = service_counts if service_counts is not None else {}
        self.public_services = public_services if public_services is not None else {}
        self.infrastructure_env = infrastructure_env if infrastructure_env is not None else {}
        self.warnings = warnings if warnings is not None else []
        self.errors = errors if errors is not None else []
        self.metadata = metadata if metadata is not None else {}
        self.created_resources = created_resources if created_resources is not None else []

    # =========================================================================
    # Methods
    # =========================================================================

    def add_warning(self, warning: str) -> None:
        """Add a warning message to the context."""
        self.warnings.append(warning)

    def update_metadata(self, **kwargs) -> None:
        """Update the metadata dictionary."""
        self.metadata.update(kwargs)

    def track_resource(
        self,
        resource_type: str,
        resource_id: str,
        cleanup_action: Optional[str] = None,
        **extra
    ) -> None:
        """
        Track a created resource for potential cleanup.

        Args:
            resource_type: Type of resource (e.g., 'ecr_repository', 'efs_access_point')
            resource_id: Unique identifier for the resource
            cleanup_action: Optional action to take during cleanup
            **extra: Additional metadata about the resource
        """
        self.created_resources.append({
            'type': resource_type,
            'id': resource_id,
            'cleanup_action': cleanup_action,
            **extra
        })

    def get_resources_of_type(self, resource_type: str) -> List[Dict[str, Any]]:
        """Get all tracked resources of a specific type."""
        return [r for r in self.created_resources if r['type'] == resource_type]

    @property
    def compose_dir(self) -> Path:
        """Get the directory containing the compose file."""
        return self.compose_file_path.parent.absolute()

    @property
    def has_build_services(self) -> bool:
        """Check if there are services that need to be built."""
        if not self.preprocessed:
            return False
        return len(self.preprocessed.get_build_services()) > 0

    @property
    def has_named_volumes(self) -> bool:
        """Check if there are named volumes that need EFS."""
        if not self.preprocessed:
            return False
        return bool(self.preprocessed.named_volumes)

    def to_summary(self) -> Dict[str, Any]:
        """Generate a summary of the context state for logging/debugging."""
        return {
            'project_name': self.project_name,
            'cluster': self.cluster.name if self.cluster else None,
            'image_tag': self.image_tag,
            'dry_run': self.dry_run,
            'build_images': self.build_images,
            'services_count': len(self.preprocessed.services) if self.preprocessed else 0,
            'build_services_count': len(self.preprocessed.get_build_services()) if self.preprocessed else 0,
            'volumes_count': len(self.preprocessed.named_volumes) if self.preprocessed else 0,
            'built_images_count': len(self.built_images),
            'efs_access_points': len(self.efs_config),
            'warnings_count': len(self.warnings),
            'created_resources_count': len(self.created_resources),
        }
