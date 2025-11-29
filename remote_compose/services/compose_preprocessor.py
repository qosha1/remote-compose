"""
Docker Compose file preprocessor for ECS conversion.

Handles YAML anchor resolution, env_file parsing, build context detection,
volume classification, and incompatibility detection before ECS conversion.
"""

import os
import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import yaml

from ..exceptions import ComposeFileError
from ..utils import sanitize_name
from .base import BaseService


class VolumeType(Enum):
    """Classification of volume mount types."""
    NAMED = "named"
    HOST_PATH = "host_path"
    DOCKER_SOCK = "docker_sock"
    TMPFS = "tmpfs"
    BIND = "bind"


@dataclass
class VolumeInfo:
    """Information about a volume mount."""
    source: str
    target: str
    volume_type: VolumeType
    read_only: bool = False
    incompatible: bool = False
    suggested_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "source": self.source,
            "target": self.target,
            "volume_type": self.volume_type.value,
            "read_only": self.read_only,
            "incompatible": self.incompatible,
            "suggested_action": self.suggested_action,
        }


@dataclass
class BuildInfo:
    """Information about a service build context."""
    context: str
    dockerfile: str = "Dockerfile"
    args: Dict[str, str] = field(default_factory=dict)
    target: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "context": self.context,
            "dockerfile": self.dockerfile,
            "args": self.args,
            "target": self.target,
        }


@dataclass
class PreprocessedService:
    """A preprocessed service ready for ECS conversion."""
    name: str
    config: Dict[str, Any]
    requires_build: bool = False
    build_info: Optional[BuildInfo] = None
    image_name: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    volumes: List[VolumeInfo] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skip: bool = False
    skip_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "config": self.config,
            "requires_build": self.requires_build,
            "build_info": self.build_info.to_dict() if self.build_info else None,
            "image_name": self.image_name,
            "env_vars": self.env_vars,
            "volumes": [v.to_dict() for v in self.volumes],
            "warnings": self.warnings,
            "skip": self.skip,
            "skip_reason": self.skip_reason,
        }


@dataclass
class PreprocessedCompose:
    """Result of preprocessing a docker-compose file."""
    services: Dict[str, PreprocessedService] = field(default_factory=dict)
    named_volumes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    networks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    requires_builds: bool = False
    requires_efs: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "services": {k: v.to_dict() for k, v in self.services.items()},
            "named_volumes": self.named_volumes,
            "networks": self.networks,
            "warnings": self.warnings,
            "errors": self.errors,
            "requires_builds": self.requires_builds,
            "requires_efs": self.requires_efs,
        }

    def get_active_services(self) -> Dict[str, PreprocessedService]:
        """Get services that should not be skipped."""
        return {k: v for k, v in self.services.items() if not v.skip}

    def get_build_services(self) -> Dict[str, PreprocessedService]:
        """Get services that require building."""
        return {k: v for k, v in self.services.items() if v.requires_build and not v.skip}


class ComposePreprocessor(BaseService):
    """
    Preprocesses docker-compose files before ECS conversion.

    Handles:
    - YAML anchor resolution
    - env_file directive parsing
    - Build context detection
    - Volume classification
    - Incompatibility detection
    """

    # Patterns for detecting Docker socket paths
    DOCKER_SOCK_PATTERNS = (
        "/var/run/docker.sock",
        "/run/docker.sock",
    )

    # Features incompatible with Fargate
    FARGATE_INCOMPATIBLE_FEATURES = {
        "privileged": "Privileged mode is not supported in Fargate",
        "network_mode": {
            "host": "Host network mode is not supported in Fargate",
        },
        "pid": {
            "host": "Host PID namespace is not supported in Fargate",
        },
        "ipc": {
            "host": "Host IPC namespace is not supported in Fargate",
        },
    }

    def __init__(
        self,
        aws_account_id: Optional[str] = None,
        aws_region: str = "us-east-1",
        ecr_repository_prefix: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize the preprocessor.

        Args:
            aws_account_id: AWS account ID for ECR image names
            aws_region: AWS region for ECR
            ecr_repository_prefix: Optional prefix for ECR repository names
        """
        super().__init__(**kwargs)
        self.aws_account_id = aws_account_id
        self.aws_region = aws_region
        self.ecr_repository_prefix = ecr_repository_prefix

    def preprocess(
        self,
        compose_content: str,
        compose_dir: Optional[str] = None,
        project_name: Optional[str] = None,
        image_tag: str = "latest",
    ) -> PreprocessedCompose:
        """
        Preprocess a docker-compose file.

        Args:
            compose_content: Raw YAML content of the compose file
            compose_dir: Directory containing the compose file (for env_file resolution)
            project_name: Project name for ECR image naming
            image_tag: Tag to use for generated ECR image names

        Returns:
            PreprocessedCompose with all services processed
        """
        result = PreprocessedCompose()

        # Step 1: Resolve YAML anchors
        try:
            resolved_content = self._resolve_yaml_anchors(compose_content)
        except yaml.YAMLError as e:
            result.errors.append(f"Invalid YAML syntax: {e}")
            return result

        # Step 2: Parse the resolved YAML
        try:
            compose_dict = yaml.safe_load(resolved_content)
        except yaml.YAMLError as e:
            result.errors.append(f"Failed to parse resolved YAML: {e}")
            return result

        if not compose_dict:
            result.errors.append("Empty compose file")
            return result

        if "services" not in compose_dict:
            result.errors.append("No services defined in compose file")
            return result

        # Extract top-level elements
        services = compose_dict.get("services", {})
        result.named_volumes = compose_dict.get("volumes", {}) or {}
        result.networks = compose_dict.get("networks", {}) or {}

        # Process each service
        for service_name, service_config in services.items():
            if not isinstance(service_config, dict):
                result.warnings.append(
                    f"Service '{service_name}' has invalid configuration, skipping"
                )
                continue

            preprocessed = self._preprocess_service(
                service_name=service_name,
                service_config=service_config,
                compose_dir=compose_dir,
                project_name=project_name,
                image_tag=image_tag,
                result=result,
            )
            result.services[service_name] = preprocessed

            if preprocessed.requires_build:
                result.requires_builds = True

            # Check if any volumes require EFS
            for vol in preprocessed.volumes:
                if vol.volume_type == VolumeType.NAMED and not vol.incompatible:
                    result.requires_efs = True

        self.log_info(
            f"Preprocessed compose file: {len(result.services)} services, "
            f"{len(result.warnings)} warnings, {len(result.errors)} errors"
        )

        return result

    def preprocess_file(
        self,
        compose_path: str,
        project_name: Optional[str] = None,
        image_tag: str = "latest",
    ) -> PreprocessedCompose:
        """
        Preprocess a docker-compose file from disk.

        Args:
            compose_path: Path to the docker-compose.yml file
            project_name: Project name for ECR image naming (defaults to directory name)
            image_tag: Tag to use for generated ECR image names

        Returns:
            PreprocessedCompose with all services processed
        """
        path = Path(compose_path)
        if not path.exists():
            raise ComposeFileError(f"Compose file not found: {compose_path}")

        content = path.read_text()
        compose_dir = str(path.parent.absolute())

        if not project_name:
            project_name = path.parent.name

        return self.preprocess(
            compose_content=content,
            compose_dir=compose_dir,
            project_name=project_name,
            image_tag=image_tag,
        )

    def get_deployment_order(self, preprocessed: PreprocessedCompose) -> List[str]:
        """
        Determine deployment order via topological sort of service dependencies.

        Uses Kahn's algorithm to produce a topologically sorted list of service
        names based on depends_on relationships. Services with no dependencies
        are deployed first, followed by services whose dependencies have all
        been deployed.

        Args:
            preprocessed: PreprocessedCompose with service configurations

        Returns:
            List of service names in deployment order (dependencies first)

        Raises:
            ComposeFileError: If a dependency cycle is detected
        """
        active_services = preprocessed.get_active_services()
        service_names = set(active_services.keys())

        # Build adjacency list and in-degree map
        # Edge from A -> B means "A must be deployed before B" (B depends on A)
        adjacency: Dict[str, List[str]] = {name: [] for name in service_names}
        in_degree: Dict[str, int] = {name: 0 for name in service_names}

        for service_name, service in active_services.items():
            depends_on = service.config.get('depends_on', [])

            # Normalize depends_on to a list of dependency names
            dep_names: List[str] = []
            if isinstance(depends_on, list):
                for dep in depends_on:
                    if isinstance(dep, str):
                        dep_names.append(dep)
                    elif isinstance(dep, dict):
                        dep_names.extend(dep.keys())
            elif isinstance(depends_on, dict):
                dep_names = list(depends_on.keys())

            for dep_name in dep_names:
                if dep_name not in service_names:
                    # Dependency references a skipped or non-existent service; skip
                    continue
                # dep_name -> service_name (dep must deploy before service)
                adjacency[dep_name].append(service_name)
                in_degree[service_name] += 1

        # Kahn's algorithm
        queue: deque[str] = deque()
        for name in service_names:
            if in_degree[name] == 0:
                queue.append(name)

        sorted_order: List[str] = []

        while queue:
            node = queue.popleft()
            sorted_order.append(node)

            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(sorted_order) != len(service_names):
            # Cycle detected - find the services involved
            remaining = service_names - set(sorted_order)
            raise ComposeFileError(
                f"Dependency cycle detected among services: {sorted(remaining)}"
            )

        return sorted_order

    def _resolve_yaml_anchors(self, content: str) -> str:
        """
        Resolve YAML anchors and merge keys.

        Uses PyYAML's full loader to resolve anchors, then re-serializes
        to produce a clean YAML without anchor references.

        Args:
            content: Raw YAML content with potential anchors

        Returns:
            YAML content with all anchors resolved
        """
        # Use FullLoader to resolve anchors (but be careful - no untrusted input)
        # Since this is for compose files, we use safe_load with custom handling
        try:
            # First, try to load with safe_load
            parsed = yaml.safe_load(content)
            if parsed is None:
                return content

            # Re-serialize to get clean YAML without anchors
            resolved = yaml.dump(parsed, default_flow_style=False, allow_unicode=True)
            return resolved
        except yaml.YAMLError:
            # If safe_load fails, return original content
            # The error will be caught in the preprocess method
            raise

    def _preprocess_service(
        self,
        service_name: str,
        service_config: Dict[str, Any],
        compose_dir: Optional[str],
        project_name: Optional[str],
        image_tag: str,
        result: PreprocessedCompose,
    ) -> PreprocessedService:
        """
        Preprocess a single service.

        Args:
            service_name: Name of the service
            service_config: Service configuration dictionary
            compose_dir: Directory containing compose file
            project_name: Project name for ECR naming
            image_tag: Image tag for ECR
            result: Parent result to add warnings to

        Returns:
            PreprocessedService instance
        """
        preprocessed = PreprocessedService(
            name=service_name,
            config=service_config.copy(),
        )

        # Check for incompatibilities first
        self._detect_incompatibilities(service_name, service_config, preprocessed, result)

        # Check for replicas: 0 (service should be skipped)
        if self._should_skip_service(service_config, preprocessed):
            return preprocessed

        # Process env_file directives
        self._process_env_files(service_config, compose_dir, preprocessed, result)

        # Detect build requirements
        self._detect_build_context(
            service_name, service_config, project_name, image_tag, preprocessed
        )

        # Classify volumes
        self._classify_volumes(service_config, preprocessed, result)

        # Set final image name
        if not preprocessed.requires_build and "image" in service_config:
            preprocessed.image_name = service_config["image"]

        return preprocessed

    def _should_skip_service(
        self,
        service_config: Dict[str, Any],
        preprocessed: PreprocessedService,
    ) -> bool:
        """
        Check if a service should be skipped.

        Args:
            service_config: Service configuration
            preprocessed: Service being processed

        Returns:
            True if service should be skipped
        """
        deploy = service_config.get("deploy", {})
        replicas = deploy.get("replicas")

        if replicas == 0:
            preprocessed.skip = True
            preprocessed.skip_reason = "Service has replicas: 0"
            preprocessed.warnings.append(
                f"Service '{preprocessed.name}' has deploy.replicas: 0 and will be skipped"
            )
            return True

        # Also skip if scale is 0
        if service_config.get("scale") == 0:
            preprocessed.skip = True
            preprocessed.skip_reason = "Service has scale: 0"
            preprocessed.warnings.append(
                f"Service '{preprocessed.name}' has scale: 0 and will be skipped"
            )
            return True

        return False

    def _detect_incompatibilities(
        self,
        service_name: str,
        service_config: Dict[str, Any],
        preprocessed: PreprocessedService,
        result: PreprocessedCompose,
    ) -> None:
        """
        Detect Fargate-incompatible features.

        Args:
            service_name: Name of the service
            service_config: Service configuration
            preprocessed: Service being processed
            result: Parent result for warnings
        """
        # Check for privileged mode
        if service_config.get("privileged"):
            msg = f"Service '{service_name}': Privileged mode is not supported in Fargate"
            preprocessed.warnings.append(msg)
            result.warnings.append(msg)

        # Check network_mode
        network_mode = service_config.get("network_mode")
        if network_mode == "host":
            msg = f"Service '{service_name}': Host network mode is not supported in Fargate"
            preprocessed.warnings.append(msg)
            result.warnings.append(msg)

        # Check PID namespace
        pid_mode = service_config.get("pid")
        if pid_mode == "host":
            msg = f"Service '{service_name}': Host PID namespace is not supported in Fargate"
            preprocessed.warnings.append(msg)
            result.warnings.append(msg)

        # Check IPC namespace
        ipc_mode = service_config.get("ipc")
        if ipc_mode == "host":
            msg = f"Service '{service_name}': Host IPC namespace is not supported in Fargate"
            preprocessed.warnings.append(msg)
            result.warnings.append(msg)

        # Check for cap_add (some capabilities not supported)
        cap_add = service_config.get("cap_add", [])
        unsupported_caps = {"SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "NET_ADMIN"}
        found_unsupported = set(cap_add) & unsupported_caps
        if found_unsupported:
            msg = (
                f"Service '{service_name}': Capabilities {found_unsupported} "
                "may not be supported in Fargate"
            )
            preprocessed.warnings.append(msg)
            result.warnings.append(msg)

        # Check for devices (not supported in Fargate)
        if service_config.get("devices"):
            msg = f"Service '{service_name}': Device mappings are not supported in Fargate"
            preprocessed.warnings.append(msg)
            result.warnings.append(msg)

    def _process_env_files(
        self,
        service_config: Dict[str, Any],
        compose_dir: Optional[str],
        preprocessed: PreprocessedService,
        result: PreprocessedCompose,
    ) -> None:
        """
        Process env_file directives and inline environment variables.

        Args:
            service_config: Service configuration
            compose_dir: Directory containing compose file
            preprocessed: Service being processed
            result: Parent result for warnings
        """
        env_files = service_config.get("env_file", [])

        # Normalize to list
        if isinstance(env_files, str):
            env_files = [env_files]

        # Start with existing environment variables
        existing_env = service_config.get("environment", {})
        if isinstance(existing_env, list):
            # Convert list format to dict
            env_dict = {}
            for item in existing_env:
                if isinstance(item, str) and "=" in item:
                    key, value = item.split("=", 1)
                    env_dict[key] = value
                elif isinstance(item, dict):
                    env_dict.update(item)
            existing_env = env_dict

        preprocessed.env_vars = dict(existing_env)

        # Process each env_file
        for env_file in env_files:
            env_path = env_file
            if compose_dir and not os.path.isabs(env_file):
                env_path = os.path.join(compose_dir, env_file)

            if not os.path.exists(env_path):
                msg = (
                    f"Service '{preprocessed.name}': env_file '{env_file}' not found, "
                    "variables will not be included"
                )
                preprocessed.warnings.append(msg)
                result.warnings.append(msg)
                continue

            try:
                file_vars = self._parse_env_file(env_path)
                # env_file variables are overridden by explicit environment
                for key, value in file_vars.items():
                    if key not in preprocessed.env_vars:
                        preprocessed.env_vars[key] = value
                self.log_debug(
                    f"Loaded {len(file_vars)} variables from {env_file}"
                )
            except Exception as e:
                msg = (
                    f"Service '{preprocessed.name}': Failed to parse env_file "
                    f"'{env_file}': {e}"
                )
                preprocessed.warnings.append(msg)
                result.warnings.append(msg)

        # Update config with merged environment
        if preprocessed.env_vars:
            preprocessed.config["environment"] = preprocessed.env_vars

        # Remove env_file from config since we've inlined the variables
        if "env_file" in preprocessed.config:
            del preprocessed.config["env_file"]

    def _parse_env_file(self, path: str) -> Dict[str, str]:
        """
        Parse a .env file.

        Args:
            path: Path to the env file

        Returns:
            Dictionary of environment variables
        """
        env_vars = {}

        with open(path, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()

                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                # Handle export prefix
                if line.startswith("export "):
                    line = line[7:].strip()

                # Parse key=value
                if "=" not in line:
                    self.log_debug(f"Skipping invalid line {line_num} in {path}: no '='")
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Remove surrounding quotes
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]

                # Validate key
                if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", key):
                    self.log_debug(f"Skipping invalid variable name: {key}")
                    continue

                env_vars[key] = value

        return env_vars

    def _detect_build_context(
        self,
        service_name: str,
        service_config: Dict[str, Any],
        project_name: Optional[str],
        image_tag: str,
        preprocessed: PreprocessedService,
    ) -> None:
        """
        Detect if service requires building and extract build info.

        Args:
            service_name: Name of the service
            service_config: Service configuration
            project_name: Project name for ECR naming
            image_tag: Image tag
            preprocessed: Service being processed
        """
        build_config = service_config.get("build")

        if not build_config:
            return

        preprocessed.requires_build = True

        # Parse build configuration
        if isinstance(build_config, str):
            # Simple string format: build: ./path
            preprocessed.build_info = BuildInfo(context=build_config)
        elif isinstance(build_config, dict):
            preprocessed.build_info = BuildInfo(
                context=build_config.get("context", "."),
                dockerfile=build_config.get("dockerfile", "Dockerfile"),
                args=build_config.get("args", {}),
                target=build_config.get("target"),
            )
        else:
            preprocessed.warnings.append(
                f"Service '{service_name}': Invalid build configuration format"
            )
            preprocessed.build_info = BuildInfo(context=".")

        # Generate ECR image name
        preprocessed.image_name = self._generate_ecr_image_name(
            service_name=service_name,
            project_name=project_name,
            tag=image_tag,
        )

        # Update config with ECR image name for later conversion
        preprocessed.config["image"] = preprocessed.image_name

    def _generate_ecr_image_name(
        self,
        service_name: str,
        project_name: Optional[str],
        tag: str = "latest",
    ) -> str:
        """
        Generate an ECR image name.

        Format: {account_id}.dkr.ecr.{region}.amazonaws.com/{project}/{service}:{tag}

        Args:
            service_name: Name of the service
            project_name: Project name (used as repository prefix)
            tag: Image tag

        Returns:
            Full ECR image URI
        """
        # Sanitize names
        safe_service = sanitize_name(service_name)
        safe_project = sanitize_name(project_name or "app")

        # Build repository name
        if self.ecr_repository_prefix:
            repo_name = f"{self.ecr_repository_prefix}/{safe_project}/{safe_service}"
        else:
            repo_name = f"{safe_project}/{safe_service}"

        # If we have AWS account ID, generate full ECR URI
        if self.aws_account_id:
            return (
                f"{self.aws_account_id}.dkr.ecr.{self.aws_region}.amazonaws.com/"
                f"{repo_name}:{tag}"
            )

        # Otherwise return just the repository name with tag
        return f"{repo_name}:{tag}"

    def _classify_volumes(
        self,
        service_config: Dict[str, Any],
        preprocessed: PreprocessedService,
        result: PreprocessedCompose,
    ) -> None:
        """
        Classify volumes by type and detect incompatibilities.

        Args:
            service_config: Service configuration
            preprocessed: Service being processed
            result: Parent result for warnings
        """
        volumes = service_config.get("volumes", [])

        for volume in volumes:
            vol_info = self._parse_volume(volume, preprocessed.name, result)
            if vol_info:
                preprocessed.volumes.append(vol_info)

        # Check for tmpfs mounts
        tmpfs_mounts = service_config.get("tmpfs", [])
        if isinstance(tmpfs_mounts, str):
            tmpfs_mounts = [tmpfs_mounts]

        for tmpfs in tmpfs_mounts:
            target = tmpfs if isinstance(tmpfs, str) else tmpfs.get("target", tmpfs)
            vol_info = VolumeInfo(
                source="",
                target=target,
                volume_type=VolumeType.TMPFS,
                incompatible=False,
                suggested_action="Tmpfs mounts are supported in Fargate",
            )
            preprocessed.volumes.append(vol_info)

    def _parse_volume(
        self,
        volume: Any,
        service_name: str,
        result: PreprocessedCompose,
    ) -> Optional[VolumeInfo]:
        """
        Parse a volume definition and classify it.

        Args:
            volume: Volume definition (string or dict)
            service_name: Name of the service
            result: Parent result for warnings

        Returns:
            VolumeInfo or None if invalid
        """
        if isinstance(volume, str):
            return self._parse_volume_string(volume, service_name, result)
        elif isinstance(volume, dict):
            return self._parse_volume_dict(volume, service_name, result)

        return None

    def _parse_volume_string(
        self,
        volume: str,
        service_name: str,
        result: PreprocessedCompose,
    ) -> Optional[VolumeInfo]:
        """
        Parse a volume string definition.

        Formats:
        - /path/in/container (anonymous volume)
        - /host/path:/container/path
        - /host/path:/container/path:ro
        - named_volume:/container/path
        - named_volume:/container/path:ro

        Args:
            volume: Volume string
            service_name: Name of the service
            result: Parent result for warnings

        Returns:
            VolumeInfo or None if invalid
        """
        parts = volume.split(":")
        read_only = False

        if len(parts) == 1:
            # Anonymous volume - just container path
            return VolumeInfo(
                source="",
                target=parts[0],
                volume_type=VolumeType.NAMED,
                incompatible=False,
                suggested_action="Anonymous volume - consider using named volume",
            )

        source = parts[0]
        target = parts[1]

        if len(parts) >= 3:
            read_only = "ro" in parts[2]

        # Classify the volume
        return self._classify_volume_source(source, target, read_only, service_name, result)

    def _parse_volume_dict(
        self,
        volume: Dict[str, Any],
        service_name: str,
        result: PreprocessedCompose,
    ) -> Optional[VolumeInfo]:
        """
        Parse a volume dictionary definition.

        Args:
            volume: Volume dict
            service_name: Name of the service
            result: Parent result for warnings

        Returns:
            VolumeInfo or None if invalid
        """
        source = volume.get("source", "")
        target = volume.get("target", "")
        read_only = volume.get("read_only", False)
        vol_type = volume.get("type", "volume")

        if not target:
            return None

        if vol_type == "tmpfs":
            return VolumeInfo(
                source="",
                target=target,
                volume_type=VolumeType.TMPFS,
                incompatible=False,
                suggested_action="Tmpfs mounts are supported in Fargate",
            )

        if vol_type == "bind":
            return self._classify_volume_source(
                source, target, read_only, service_name, result
            )

        # Default: named volume
        return self._classify_volume_source(source, target, read_only, service_name, result)

    def _classify_volume_source(
        self,
        source: str,
        target: str,
        read_only: bool,
        service_name: str,
        result: PreprocessedCompose,
    ) -> VolumeInfo:
        """
        Classify a volume based on its source.

        Args:
            source: Volume source (path or name)
            target: Container mount target
            read_only: Whether volume is read-only
            service_name: Name of the service
            result: Parent result for warnings

        Returns:
            VolumeInfo with classification
        """
        # Check for Docker socket
        if any(sock in source for sock in self.DOCKER_SOCK_PATTERNS) or \
           any(sock in target for sock in self.DOCKER_SOCK_PATTERNS):
            msg = (
                f"Service '{service_name}': Docker socket mount detected - "
                "this is not supported in Fargate"
            )
            result.warnings.append(msg)
            return VolumeInfo(
                source=source,
                target=target,
                volume_type=VolumeType.DOCKER_SOCK,
                read_only=read_only,
                incompatible=True,
                suggested_action="Remove Docker socket mount - consider using ECS task APIs instead",
            )

        # Check for host path (absolute path or relative ./path)
        if source.startswith("/") or source.startswith("./") or source.startswith("../"):
            msg = (
                f"Service '{service_name}': Host path volume '{source}' - "
                "consider using EFS for persistent storage in Fargate"
            )
            result.warnings.append(msg)
            return VolumeInfo(
                source=source,
                target=target,
                volume_type=VolumeType.HOST_PATH,
                read_only=read_only,
                incompatible=True,
                suggested_action="Convert to EFS volume or remove for Fargate compatibility",
            )

        # Named volume
        return VolumeInfo(
            source=source,
            target=target,
            volume_type=VolumeType.NAMED,
            read_only=read_only,
            incompatible=False,
            suggested_action="Configure EFS filesystem for persistent storage",
        )

    def get_build_commands(
        self,
        preprocessed: PreprocessedCompose,
        compose_dir: str,
    ) -> List[Dict[str, Any]]:
        """
        Generate build commands for services that require building.

        Args:
            preprocessed: Preprocessed compose result
            compose_dir: Directory containing compose file

        Returns:
            List of build command specifications
        """
        commands = []

        for service in preprocessed.get_build_services().values():
            if not service.build_info:
                continue

            # Resolve context path
            context = service.build_info.context
            if not os.path.isabs(context):
                context = os.path.join(compose_dir, context)

            # Resolve dockerfile path
            dockerfile = service.build_info.dockerfile
            dockerfile_path = os.path.join(context, dockerfile)

            cmd = {
                "service": service.name,
                "image": service.image_name,
                "context": context,
                "dockerfile": dockerfile_path,
                "build_args": service.build_info.args,
                "target": service.build_info.target,
            }
            commands.append(cmd)

        return commands
