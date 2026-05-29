"""
Docker Compose to ECS Task Definition converter.

Converts docker-compose.yml files to AWS ECS task definitions,
handling service definitions, resource allocations, and networking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
from typing import Optional, List, Dict, Any, Tuple, TYPE_CHECKING
from pathlib import Path

import yaml

from ..models import ECSCluster, ECSTaskDefinition
from ..exceptions import ComposeConversionError
from ..utils import sanitize_name
from .base import BaseService

if TYPE_CHECKING:
    from .compose_preprocessor import PreprocessedCompose, PreprocessedService

logger = logging.getLogger(__name__)

# Fargate CPU/Memory combinations (CPU units -> allowed memory values in MB)
FARGATE_CPU_MEMORY_MAP = {
    "256": [512, 1024, 2048],
    "512": [1024, 2048, 3072, 4096],
    "1024": [2048, 3072, 4096, 5120, 6144, 7168, 8192],
    "2048": [
        4096,
        5120,
        6144,
        7168,
        8192,
        9216,
        10240,
        11264,
        12288,
        13312,
        14336,
        15360,
        16384,
    ],
    "4096": list(range(8192, 30721, 1024)),
    "8192": list(range(16384, 61441, 4096)),
    "16384": list(range(32768, 122881, 8192)),
}


class ComposeToECSConverter(BaseService):
    """
    Converts docker-compose.yml to ECS task definitions.

    Handles:
    - Service to container definition conversion
    - Port mappings
    - Environment variables
    - Resource constraints (CPU/memory)
    - Health checks
    - Logging configuration
    - Volume mounts (EFS for Fargate)
    """

    def __init__(
        self,
        account_id: Optional[str] = None,
        **kwargs,
    ):
        """
        ``account_id`` is the AWS account that owns the Secrets Manager
        entries referenced by compose ``secrets:`` blocks. Required to
        emit valid ECS task-def ``valueFrom`` ARNs (remote-compose-9yo).
        Earlier behavior wrote literal ``arn:aws:secretsmanager:REGION:
        ACCOUNT:secret:<name>`` placeholders that ECS rejects at
        register-task-definition time. When None, _convert_secrets
        raises ComposeConversionError instead of emitting bogus ARNs.
        """
        super().__init__(**kwargs)
        self._conversion_warnings = []
        self._account_id = account_id

    @property
    def warnings(self) -> List[str]:
        """Get warnings from last conversion."""
        return self._conversion_warnings.copy()

    def convert(
        self,
        compose_content: str,
        cluster: ECSCluster,
        task_family_name: str,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
    ) -> ECSTaskDefinition:
        """
        Convert docker-compose content to an ECS task definition.

        Args:
            compose_content: Docker compose YAML content
            cluster: Target ECS cluster
            task_family_name: Name for the task definition family
            cpu: Override total CPU (Fargate units)
            memory: Override total memory (MB)

        Returns:
            ECSTaskDefinition model (saved to DB, not yet registered in AWS)
        """
        self._conversion_warnings = []

        try:
            compose_dict = yaml.safe_load(compose_content)
        except yaml.YAMLError as e:
            raise ComposeConversionError(f"Invalid YAML: {e}")

        if not compose_dict:
            raise ComposeConversionError("Empty compose file")

        services = compose_dict.get("services", {})
        if not services:
            raise ComposeConversionError("No services defined in compose file")

        container_definitions = []
        total_cpu = 0
        total_memory = 0
        volumes = []

        network_mode = (
            "awsvpc"
            if cluster.launch_type == ECSCluster.LaunchType.FARGATE
            else "bridge"
        )

        for service_name, service_config in services.items():
            container_def, service_cpu, service_memory, service_volumes = (
                self._convert_service(
                    service_name,
                    service_config,
                    cluster.launch_type,
                    network_mode,
                    cluster.aws_region,
                )
            )
            container_definitions.append(container_def)
            total_cpu += service_cpu
            total_memory += service_memory
            volumes.extend(service_volumes)

        compose_hash = hashlib.sha256(compose_content.encode()).hexdigest()

        task_definition = self._build_task_definition(
            cluster=cluster,
            task_family_name=task_family_name,
            container_definitions=container_definitions,
            volumes=volumes,
            total_cpu=total_cpu,
            total_memory=total_memory,
            compose_hash=compose_hash,
            source_compose_file=compose_content,
            override_cpu=cpu,
            override_memory=memory,
        )

        task_definition.save()

        self._log_conversion_result(task_definition.name)
        return task_definition

    def convert_file(
        self,
        compose_file_path: str,
        cluster: ECSCluster,
        task_family_name: Optional[str] = None,
        **kwargs,
    ) -> ECSTaskDefinition:
        """
        Convert a docker-compose file to an ECS task definition.

        Args:
            compose_file_path: Path to docker-compose.yml
            cluster: Target ECS cluster
            task_family_name: Name for task family (defaults to directory name)
            **kwargs: Additional arguments passed to convert()

        Returns:
            ECSTaskDefinition model
        """
        path = Path(compose_file_path)
        if not path.exists():
            raise ComposeConversionError(f"Compose file not found: {path}")

        content = path.read_text()

        if not task_family_name:
            task_family_name = path.parent.name

        task_family_name = sanitize_name(task_family_name)

        return self.convert(content, cluster, task_family_name, **kwargs)

    def convert_preprocessed(
        self,
        preprocessed: "PreprocessedCompose",
        cluster: ECSCluster,
        task_family_name: str,
        efs_config: Optional[Dict[str, Dict[str, str]]] = None,
        cpu: Optional[str] = None,
        memory: Optional[str] = None,
        strict_mode: bool = False,
    ) -> ECSTaskDefinition:
        """
        Convert preprocessed compose data to an ECS task definition.

        Args:
            preprocessed: PreprocessedCompose from ComposePreprocessor
            cluster: Target ECS cluster
            task_family_name: Name for the task definition family
            efs_config: EFS volume configurations mapping volume names to EFS details
            cpu: Override total CPU (Fargate units)
            memory: Override total memory (MB)
            strict_mode: If True, raise errors on warnings instead of continuing

        Returns:
            ECSTaskDefinition model (unsaved, not yet registered in AWS).
            Caller is responsible for calling .save().

        Raises:
            ComposeConversionError: If conversion fails or strict_mode encounters warnings
        """
        self._conversion_warnings = []
        efs_config = efs_config or {}

        active_services = self._validate_preprocessed(preprocessed, strict_mode)

        container_definitions = []
        total_cpu = 0
        total_memory = 0
        all_volumes: Dict[str, Dict[str, Any]] = {}

        network_mode = (
            "awsvpc"
            if cluster.launch_type == ECSCluster.LaunchType.FARGATE
            else "bridge"
        )

        for service_name, service in active_services.items():
            container_def, service_cpu, service_memory, service_volumes = (
                self._convert_preprocessed_service(
                    service=service,
                    launch_type=cluster.launch_type,
                    network_mode=network_mode,
                    region=cluster.aws_region,
                    efs_config=efs_config,
                )
            )
            container_definitions.append(container_def)
            total_cpu += service_cpu
            total_memory += service_memory

            for vol in service_volumes:
                all_volumes.setdefault(vol["name"], vol)

        # Add EFS volume definitions for named volumes
        for vol in self._convert_efs_volumes(preprocessed.named_volumes, efs_config):
            all_volumes.setdefault(vol["name"], vol)

        content_for_hash = json.dumps(preprocessed.to_dict(), sort_keys=True)
        compose_hash = hashlib.sha256(content_for_hash.encode()).hexdigest()

        task_definition = self._build_task_definition(
            cluster=cluster,
            task_family_name=task_family_name,
            container_definitions=container_definitions,
            volumes=list(all_volumes.values()),
            total_cpu=total_cpu,
            total_memory=total_memory,
            compose_hash=compose_hash,
            override_cpu=cpu,
            override_memory=memory,
        )

        self._log_conversion_result(task_definition.name)
        return task_definition

    def convert_per_service(
        self,
        preprocessed: "PreprocessedCompose",
        cluster: ECSCluster,
        project_name: str,
        efs_config: Optional[Dict[str, Dict[str, str]]] = None,
        service_resources: Optional[Dict[str, Dict[str, Any]]] = None,
        secrets_arns: Optional[Dict[str, str]] = None,
        shared_images: Optional[Dict[str, str]] = None,
        strict_mode: bool = False,
        infrastructure_env: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, ECSTaskDefinition]:
        """
        Convert preprocessed compose data to one ECSTaskDefinition per service.

        Unlike convert_preprocessed() which bundles all containers into a single
        task definition, this method creates a separate task definition for each
        compose service.

        Args:
            preprocessed: PreprocessedCompose from ComposePreprocessor
            cluster: Target ECS cluster
            project_name: Project name used as prefix for task family names
            efs_config: EFS volume configurations mapping volume names to EFS details
            service_resources: Per-service CPU/memory overrides
            secrets_arns: Mapping of env var names to AWS Secrets Manager ARNs
            shared_images: Mapping of build context keys to ECR URIs
            strict_mode: If True, raise errors on warnings instead of continuing
            infrastructure_env: Per-service and universal infrastructure env vars

        Returns:
            Dict mapping service name to its ECSTaskDefinition (unsaved).
            Caller is responsible for calling .save() on each.

        Raises:
            ComposeConversionError: If conversion fails or strict_mode encounters warnings
        """
        self._conversion_warnings = []
        efs_config = efs_config or {}
        service_resources = service_resources or {}
        secrets_arns = secrets_arns or {}
        shared_images = shared_images or {}
        infrastructure_env = infrastructure_env or {}

        active_services = self._validate_preprocessed(preprocessed, strict_mode)

        network_mode = (
            "awsvpc"
            if cluster.launch_type == ECSCluster.LaunchType.FARGATE
            else "bridge"
        )

        content_for_hash = json.dumps(preprocessed.to_dict(), sort_keys=True)
        compose_hash = hashlib.sha256(content_for_hash.encode()).hexdigest()

        task_definitions: Dict[str, ECSTaskDefinition] = {}

        for service_name, service in active_services.items():
            # Note: per-service image URIs are already set by BuildAndPushImagesStep.
            # Each service gets its own ECR repo even when sharing a Dockerfile.
            # Only fall back to shared_images if service has no image_name set.
            if service.build_info and shared_images and not service.image_name:
                build_key = f"{os.path.normpath(service.build_info.context)}:{service.build_info.dockerfile}"
                if build_key in shared_images:
                    service.image_name = shared_images[build_key]
                    service.config["image"] = shared_images[build_key]

            # Inject infrastructure environment variables
            if infrastructure_env:
                self._apply_infrastructure_env(
                    service, service_name, infrastructure_env
                )

            container_def, service_cpu, service_memory, service_volumes = (
                self._convert_preprocessed_service(
                    service=service,
                    launch_type=cluster.launch_type,
                    network_mode=network_mode,
                    region=cluster.aws_region,
                    efs_config=efs_config,
                )
            )

            # Per-service mode: each task has one container, so cross-container
            # dependencies (from compose depends_on/links) are invalid in ECS.
            container_def.pop("dependsOn", None)
            container_def.pop("links", None)

            # Move matching env vars to ECS secrets list
            if secrets_arns and "environment" in container_def:
                self._apply_secrets(container_def, secrets_arns)

            # Collect volumes (service-level + EFS named volumes)
            all_volumes: Dict[str, Dict[str, Any]] = {}
            for vol in service_volumes:
                all_volumes.setdefault(vol["name"], vol)
            for vol in self._convert_efs_volumes(
                preprocessed.named_volumes, efs_config
            ):
                all_volumes.setdefault(vol["name"], vol)

            # Per-service resource overrides
            resource_override = service_resources.get(service_name, {})

            # Propagate overrides to the container definition itself, not just
            # the task. Without this the container keeps the compose-default
            # hard limits (cpu=256, memory=512) while the task gets the full
            # override allocation — so the container OOM-kills long before it
            # can use the memory the task was billed for.
            override_cpu = resource_override.get("cpu")
            override_memory = resource_override.get("memory")
            if override_cpu is not None:
                container_def["cpu"] = int(override_cpu)
                service_cpu = int(override_cpu)
            if override_memory is not None:
                container_def["memory"] = int(override_memory)
                service_memory = int(override_memory)

            task_family_name = sanitize_name(f"{project_name}-{service_name}")

            task_definition = self._build_task_definition(
                cluster=cluster,
                task_family_name=task_family_name,
                container_definitions=[container_def],
                volumes=list(all_volumes.values()),
                total_cpu=service_cpu,
                total_memory=service_memory,
                compose_hash=compose_hash,
                override_cpu=override_cpu,
                override_memory=override_memory,
            )

            task_definitions[service_name] = task_definition

        if self._conversion_warnings:
            self.log_warning(f"Conversion warnings: {self._conversion_warnings}")

        self.log_info(
            f"Converted preprocessed compose to {len(task_definitions)} "
            f"per-service task definitions for project '{project_name}'"
        )
        return task_definitions

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _validate_preprocessed(
        self,
        preprocessed: "PreprocessedCompose",
        strict_mode: bool,
    ) -> Dict[str, "PreprocessedService"]:
        """Validate preprocessed data and return active services."""
        if preprocessed.errors:
            raise ComposeConversionError(
                f"Preprocessed compose has errors: {preprocessed.errors}"
            )

        active_services = preprocessed.get_active_services()
        if not active_services:
            raise ComposeConversionError("No active services to convert")

        if strict_mode and preprocessed.warnings:
            raise ComposeConversionError(
                f"Strict mode: preprocessing warnings: {preprocessed.warnings}"
            )

        return active_services

    def _build_task_definition(
        self,
        cluster: ECSCluster,
        task_family_name: str,
        container_definitions: List[Dict[str, Any]],
        volumes: List[Dict[str, Any]],
        total_cpu: int,
        total_memory: int,
        compose_hash: str,
        source_compose_file: str = "",
        override_cpu: Optional[str] = None,
        override_memory: Optional[str] = None,
    ) -> ECSTaskDefinition:
        """
        Build an ECSTaskDefinition with Fargate-valid resources and revision tracking.

        Calculates valid Fargate CPU/memory, looks up the latest revision in the DB,
        and constructs the model object. Does NOT call .save().

        Returns:
            ECSTaskDefinition (unsaved)
        """
        final_cpu, final_memory = self._calculate_fargate_resources(
            total_cpu, total_memory, override_cpu, override_memory
        )

        existing = (
            ECSTaskDefinition.objects.filter(
                cluster=cluster,
                name=task_family_name,
            )
            .order_by("-revision")
            .first()
        )

        revision = (existing.revision + 1) if existing else 1

        return ECSTaskDefinition(
            name=task_family_name,
            cluster=cluster,
            revision=revision,
            source_compose_file=source_compose_file,
            source_compose_hash=compose_hash,
            container_definitions=container_definitions,
            cpu=str(final_cpu),
            memory=str(final_memory),
            requires_compatibilities=(
                ["FARGATE"]
                if cluster.launch_type == ECSCluster.LaunchType.FARGATE
                else ["EC2"]
            ),
            network_mode=(
                "awsvpc"
                if cluster.launch_type == ECSCluster.LaunchType.FARGATE
                else "bridge"
            ),
            volumes=volumes,
            status=ECSTaskDefinition.Status.DRAFT,
        )

    @staticmethod
    def _apply_secrets(
        container_def: Dict[str, Any], secrets_arns: Dict[str, str]
    ) -> None:
        """Move env vars matching secrets_arns keys into ECS secrets list."""
        remaining_env = []
        ecs_secrets = container_def.get("secrets", [])
        for env_entry in container_def["environment"]:
            env_name = env_entry["name"]
            if env_name in secrets_arns:
                ecs_secrets.append(
                    {
                        "name": env_name,
                        "valueFrom": secrets_arns[env_name],
                    }
                )
            else:
                remaining_env.append(env_entry)
        container_def["environment"] = remaining_env
        if ecs_secrets:
            container_def["secrets"] = ecs_secrets

    @staticmethod
    def _apply_infrastructure_env(
        service: "PreprocessedService",
        service_name: str,
        infrastructure_env: Dict[str, Any],
    ) -> None:
        """Inject infrastructure-derived env vars into a service's env_vars."""
        # Universal vars (stored under '_universal' key)
        universal = infrastructure_env.get("_universal", {})
        if universal:
            for key, value in universal.items():
                service.env_vars.setdefault(key, value)

        # Per-service vars
        svc_env = infrastructure_env.get(service_name, {})
        if isinstance(svc_env, dict):
            for key, value in svc_env.items():
                service.env_vars.setdefault(key, value)

    def _log_conversion_result(self, task_name: str) -> None:
        """Log warnings and info after conversion."""
        if self._conversion_warnings:
            self.log_warning(f"Conversion warnings: {self._conversion_warnings}")
        self.log_info(f"Converted compose to task definition: {task_name}")

    # ------------------------------------------------------------------
    # Service conversion
    # ------------------------------------------------------------------

    def _convert_preprocessed_service(
        self,
        service: "PreprocessedService",
        launch_type: str,
        network_mode: str,
        region: str,
        efs_config: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> Tuple[Dict[str, Any], int, int, List[Dict]]:
        """
        Convert a preprocessed service to an ECS container definition.

        Uses pre-parsed environment variables and pre-determined image names
        from the preprocessing step.

        Returns:
            Tuple of (container_definition, cpu_units, memory_mb, volumes)
        """
        efs_config = efs_config or {}
        config = service.config
        container_name = sanitize_name(service.name)

        # Use pre-determined image name from preprocessing
        image = service.image_name
        if not image:
            image = config.get("image")
            if not image:
                raise ComposeConversionError(
                    f"Service '{service.name}' has no image configured"
                )

        container_def = {
            "name": container_name,
            "image": image,
            "essential": config.get("essential", True),
        }

        cpu, memory = self._convert_resources(config, service.name)
        container_def["cpu"] = cpu
        container_def["memory"] = memory

        if "ports" in config:
            port_mappings = self._convert_ports(config["ports"], network_mode)
            # Add name to port mappings for ECS Service Connect compatibility
            for pm in port_mappings:
                if "name" not in pm:
                    pm["name"] = container_name
            container_def["portMappings"] = port_mappings

        # Use pre-parsed environment variables from preprocessing
        environment = []
        for key, value in service.env_vars.items():
            environment.append(
                {"name": key, "value": str(value) if value is not None else ""}
            )
        container_def["environment"] = environment

        if "secrets" in config:
            container_def["secrets"] = self._convert_secrets(
                config["secrets"],
                region=region,
            )

        if "command" in config:
            container_def["command"] = self._convert_command(config["command"])

        if "entrypoint" in config:
            container_def["entryPoint"] = self._convert_command(config["entrypoint"])

        if "working_dir" in config:
            container_def["workingDirectory"] = config["working_dir"]

        if "user" in config:
            container_def["user"] = str(config["user"])

        if "healthcheck" in config:
            container_def["healthCheck"] = self._convert_healthcheck(
                config["healthcheck"]
            )

        container_def["logConfiguration"] = self._get_default_log_config(
            container_name, region
        )

        # Convert volumes (with optional EFS support)
        volumes = []
        if service.volumes:
            mounts, volume_defs = self._convert_volumes(
                volumes=service.volumes,
                container_name=container_name,
                launch_type=launch_type,
                efs_config=efs_config,
            )
            if mounts:
                container_def["mountPoints"] = mounts
            volumes = volume_defs

        if "depends_on" in config:
            container_def["dependsOn"] = self._convert_depends_on(config["depends_on"])

        if "links" in config:
            container_def["links"] = config["links"]

        return container_def, cpu, memory, volumes

    def _convert_service(
        self,
        name: str,
        config: Dict[str, Any],
        launch_type: str,
        network_mode: str = "awsvpc",
        region: str = "us-east-1",
    ) -> Tuple[Dict[str, Any], int, int, List[Dict]]:
        """
        Convert a compose service to an ECS container definition.

        Returns:
            Tuple of (container_definition, cpu_units, memory_mb, volumes)
        """
        container_name = sanitize_name(name)

        image = config.get("image")
        if not image:
            build_config = config.get("build")
            if build_config:
                self._conversion_warnings.append(
                    f"Service '{name}' uses build context. "
                    "Image must be pre-built and pushed to a registry."
                )
                image = f"{container_name}:latest"
            else:
                raise ComposeConversionError(
                    f"Service '{name}' has no image or build config"
                )

        container_def = {
            "name": container_name,
            "image": image,
            "essential": config.get("essential", True),
        }

        cpu, memory = self._convert_resources(config, name)
        container_def["cpu"] = cpu
        container_def["memory"] = memory

        if "ports" in config:
            container_def["portMappings"] = self._convert_ports(
                config["ports"], network_mode
            )

        environment = []
        if "environment" in config:
            environment.extend(self._convert_environment(config["environment"]))
        container_def["environment"] = environment

        if "secrets" in config:
            container_def["secrets"] = self._convert_secrets(
                config["secrets"],
                region=region,
            )

        if "command" in config:
            container_def["command"] = self._convert_command(config["command"])

        if "entrypoint" in config:
            container_def["entryPoint"] = self._convert_command(config["entrypoint"])

        if "working_dir" in config:
            container_def["workingDirectory"] = config["working_dir"]

        if "user" in config:
            container_def["user"] = str(config["user"])

        if "healthcheck" in config:
            container_def["healthCheck"] = self._convert_healthcheck(
                config["healthcheck"]
            )

        container_def["logConfiguration"] = self._get_default_log_config(
            container_name, region
        )

        volumes = []
        if "volumes" in config:
            mounts, volume_defs = self._convert_volumes(
                config["volumes"], container_name, launch_type
            )
            if mounts:
                container_def["mountPoints"] = mounts
            volumes = volume_defs

        if "depends_on" in config:
            container_def["dependsOn"] = self._convert_depends_on(config["depends_on"])

        if "links" in config:
            container_def["links"] = config["links"]

        return container_def, cpu, memory, volumes

    # ------------------------------------------------------------------
    # Field-level converters
    # ------------------------------------------------------------------

    def _convert_resources(
        self,
        config: Dict[str, Any],
        service_name: str,
    ) -> Tuple[int, int]:
        """Convert resource limits/reservations to ECS format."""
        cpu = 256
        memory = 512

        deploy = config.get("deploy", {})
        resources = deploy.get("resources", {})

        limits = resources.get("limits", {})
        reservations = resources.get("reservations", {})

        if "cpus" in limits:
            cpu = int(float(limits["cpus"]) * 1024)
        elif "cpus" in reservations:
            cpu = int(float(reservations["cpus"]) * 1024)

        if "memory" in limits:
            memory = self._parse_memory(limits["memory"])
        elif "memory" in reservations:
            memory = self._parse_memory(reservations["memory"])

        if "mem_limit" in config:
            memory = self._parse_memory(config["mem_limit"])
        if "mem_reservation" in config:
            memory = max(memory, self._parse_memory(config["mem_reservation"]))

        if "cpus" in config:
            cpu = int(float(config["cpus"]) * 1024)

        return cpu, memory

    def _parse_memory(self, value: Any) -> int:
        """Parse memory value to MB."""
        if isinstance(value, int):
            return value

        memory_str = str(value).lower().strip()
        match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgb])?(?:b)?$", memory_str)

        if not match:
            logger.warning(
                f"Unrecognized memory format '{memory_str}', defaulting to 512 MiB"
            )
            return 512

        num = float(match.group(1))
        unit = match.group(2) or "m"

        multipliers = {"k": 1 / 1024, "m": 1, "g": 1024, "b": 1 / 1024 / 1024}
        return int(num * multipliers.get(unit, 1))

    def _convert_ports(
        self, ports: List, network_mode: str = "awsvpc"
    ) -> List[Dict[str, Any]]:
        """Convert port mappings.

        For awsvpc network mode (Fargate), host port must equal container port.
        """
        mappings = []

        for port in ports:
            if isinstance(port, int):
                mapping = {
                    "containerPort": port,
                    "protocol": "tcp",
                }
                # For awsvpc, host port must match container port
                if network_mode == "awsvpc":
                    mapping["hostPort"] = port
                mappings.append(mapping)
            elif isinstance(port, str):
                parts = port.replace("-", ":").split(":")
                container_port = int(parts[-1].split("/")[0])
                protocol = "udp" if "/udp" in port else "tcp"

                mapping = {
                    "containerPort": container_port,
                    "protocol": protocol,
                }

                # For awsvpc mode, host port must equal container port
                if network_mode == "awsvpc":
                    mapping["hostPort"] = container_port
                    # Warn if a different host port was specified
                    if len(parts) >= 2:
                        host_port = parts[-2]
                        if (
                            host_port
                            and host_port.isdigit()
                            and int(host_port) != container_port
                        ):
                            self._conversion_warnings.append(
                                f"Port mapping {port}: host port changed to {container_port} "
                                f"(awsvpc mode requires host port = container port)"
                            )
                elif len(parts) >= 2:
                    host_port = parts[-2]
                    if host_port and host_port.isdigit():
                        mapping["hostPort"] = int(host_port)

                mappings.append(mapping)
            elif isinstance(port, dict):
                container_port = port.get("target", port.get("container_port"))
                mapping = {
                    "containerPort": container_port,
                    "protocol": port.get("protocol", "tcp"),
                }
                if network_mode == "awsvpc":
                    mapping["hostPort"] = container_port
                else:
                    mapping["hostPort"] = port.get("published", port.get("host_port"))
                mappings.append(mapping)

        return mappings

    def _convert_environment(self, env: Any) -> List[Dict[str, str]]:
        """Convert environment variables."""
        variables = []

        if isinstance(env, dict):
            for key, value in env.items():
                variables.append(
                    {"name": key, "value": str(value) if value is not None else ""}
                )
        elif isinstance(env, list):
            for item in env:
                if isinstance(item, str):
                    if "=" in item:
                        key, value = item.split("=", 1)
                        variables.append({"name": key, "value": value})
                    else:
                        self._conversion_warnings.append(
                            f"Environment variable '{item}' has no value and will be skipped"
                        )
                elif isinstance(item, dict):
                    for key, value in item.items():
                        variables.append(
                            {
                                "name": key,
                                "value": str(value) if value is not None else "",
                            }
                        )

        return variables

    def _convert_secrets(
        self,
        secrets: List,
        region: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Convert compose ``secrets:`` to ECS task-def ``secrets[]``.

        Each entry's ``valueFrom`` must be a fully-qualified ARN — ECS
        register-task-definition rejects placeholders. We need both
        region (per-cluster) and account_id (per-converter, set in
        __init__) to build the ARN. When account_id wasn't supplied,
        raise ComposeConversionError so the caller knows to provide it
        instead of silently emitting bogus values.

        ``arn:aws:secretsmanager:<region>:<account>:secret:<name>``
        """
        ecs_secrets = []
        if not secrets:
            return ecs_secrets

        if not self._account_id:
            raise ComposeConversionError(
                "compose declares secrets but ComposeToECSConverter was "
                "constructed without account_id; the resulting "
                "valueFrom ARN cannot be generated. Pass account_id="
                "<aws-account-id> to the converter constructor "
                "(remote-compose-9yo)."
            )
        if not region:
            raise ComposeConversionError(
                "compose declares secrets but no region was passed to "
                "_convert_secrets; the resulting valueFrom ARN cannot "
                "be region-qualified (remote-compose-9yo)."
            )

        for secret in secrets:
            if isinstance(secret, str):
                self._conversion_warnings.append(
                    f"Secret '{secret}' requires manual configuration "
                    f"in AWS Secrets Manager"
                )
            elif isinstance(secret, dict):
                name = secret.get("source", secret.get("name", ""))
                if name:
                    ecs_secrets.append(
                        {
                            "name": name.upper().replace("-", "_"),
                            "valueFrom": (
                                f"arn:aws:secretsmanager:{region}:"
                                f"{self._account_id}:secret:{name}"
                            ),
                        }
                    )

        return ecs_secrets

    def _convert_command(self, command: Any) -> List[str]:
        """Convert command to list format.

        String form uses shlex.split so quoted args survive — ``sh -c
        "echo hello"`` becomes ``['sh', '-c', 'echo hello']`` instead of
        ``['sh', '-c', '"echo', 'hello"']`` (the str.split() bug from
        remote-compose-l9o).
        """
        if isinstance(command, str):
            return shlex.split(command)
        elif isinstance(command, list):
            return [str(c) for c in command]
        return []

    def _convert_healthcheck(self, healthcheck: Dict) -> Dict[str, Any]:
        """Convert health check to ECS format."""
        ecs_health = {}

        test = healthcheck.get("test", [])
        if isinstance(test, str):
            ecs_health["command"] = ["CMD-SHELL", test]
        elif isinstance(test, list):
            if test and test[0] in ("CMD", "CMD-SHELL", "NONE"):
                ecs_health["command"] = test
            else:
                ecs_health["command"] = ["CMD"] + test

        if "interval" in healthcheck:
            ecs_health["interval"] = self._parse_duration(healthcheck["interval"])
        if "timeout" in healthcheck:
            ecs_health["timeout"] = self._parse_duration(healthcheck["timeout"])
        if "retries" in healthcheck:
            ecs_health["retries"] = healthcheck["retries"]
        if "start_period" in healthcheck:
            ecs_health["startPeriod"] = self._parse_duration(
                healthcheck["start_period"]
            )

        return ecs_health

    def _parse_duration(self, value: Any) -> int:
        """Parse duration to seconds."""
        if isinstance(value, int):
            return value

        duration_str = str(value).lower().strip()
        match = re.match(r"^(\d+)\s*([smh])?$", duration_str)

        if not match:
            logger.warning(
                f"Unrecognized duration format '{duration_str}', defaulting to 30s"
            )
            return 30

        num = int(match.group(1))
        unit = match.group(2) or "s"

        multipliers = {"s": 1, "m": 60, "h": 3600}
        return num * multipliers.get(unit, 1)

    # ------------------------------------------------------------------
    # Volume conversion (unified)
    # ------------------------------------------------------------------

    def _convert_volumes(
        self,
        volumes: List,
        container_name: str,
        launch_type: str,
        efs_config: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Convert volumes to ECS mount points and volume definitions.

        Handles both raw compose volume entries (strings/dicts) and
        preprocessed VolumeInfo objects. When efs_config is provided,
        named volumes with matching EFS configurations get EFS volume
        definitions attached.

        Args:
            volumes: List of volume entries (strings, dicts, or VolumeInfo objects)
            container_name: Name of the container
            launch_type: ECS launch type
            efs_config: Optional EFS volume configurations

        Returns:
            Tuple of (mount_points, volume_definitions)
        """
        from .compose_preprocessor import VolumeInfo, VolumeType

        efs_config = efs_config or {}
        mount_points = []
        volume_defs = []

        for i, volume in enumerate(volumes):
            # Normalize raw compose entries into VolumeInfo objects
            if isinstance(volume, str):
                parts = volume.split(":")
                if len(parts) < 2:
                    continue
                source = parts[0]
                target = parts[1]
                read_only = len(parts) > 2 and "ro" in parts[2]

                if source.startswith("/") or source.startswith("./"):
                    vol = VolumeInfo(
                        source=source,
                        target=target,
                        volume_type=VolumeType.HOST_PATH,
                        read_only=read_only,
                    )
                else:
                    vol = VolumeInfo(
                        source=source,
                        target=target,
                        volume_type=VolumeType.NAMED,
                        read_only=read_only,
                    )

            elif isinstance(volume, dict) and not isinstance(volume, VolumeInfo):
                source = volume.get("source", "")
                target = volume.get("target", "")
                read_only = volume.get("read_only", False)
                raw_type = volume.get("type", volume.get("volume_type", "volume"))

                if raw_type == "bind":
                    vtype = VolumeType.BIND
                elif raw_type == "tmpfs":
                    vtype = VolumeType.TMPFS
                elif raw_type in ("host_path",):
                    vtype = VolumeType.HOST_PATH
                else:
                    vtype = VolumeType.NAMED

                vol = VolumeInfo(
                    source=source,
                    target=target,
                    volume_type=vtype,
                    read_only=read_only,
                    incompatible=volume.get("incompatible", False),
                )

            elif isinstance(volume, VolumeInfo):
                vol = volume
            else:
                continue

            # Skip incompatible volumes
            if vol.incompatible:
                self._conversion_warnings.append(
                    f"Skipping incompatible volume: {vol.source} -> {vol.target}"
                )
                continue

            # Skip volumes without targets
            if not vol.target:
                continue

            # Handle different volume types
            if vol.volume_type == VolumeType.NAMED:
                volume_name = vol.source if vol.source else f"{container_name}-vol-{i}"

                # Check if this volume has EFS configuration
                if volume_name in efs_config:
                    efs_conf = efs_config[volume_name]
                    volume_def = self._create_efs_volume_definition(
                        volume_name, efs_conf
                    )
                    volume_defs.append(volume_def)
                else:
                    if not any(v.get("name") == volume_name for v in volume_defs):
                        volume_defs.append({"name": volume_name})

                mount_points.append(
                    {
                        "sourceVolume": volume_name,
                        "containerPath": vol.target,
                        "readOnly": vol.read_only,
                    }
                )

            elif vol.volume_type == VolumeType.TMPFS:
                self._conversion_warnings.append(
                    f"Tmpfs volume at {vol.target} converted to ephemeral storage"
                )

            elif vol.volume_type in (VolumeType.HOST_PATH, VolumeType.BIND):
                is_fargate = (
                    launch_type == ECSCluster.LaunchType.FARGATE
                    or launch_type == "fargate"
                )
                if is_fargate:
                    label = (
                        "Host path volumes"
                        if vol.volume_type == VolumeType.HOST_PATH
                        else "Bind mounts"
                    )
                    self._conversion_warnings.append(
                        f"{label} not supported in Fargate: {vol.source}"
                    )
                    continue

                volume_name = f"{container_name}-vol-{i}"
                volume_defs.append(
                    {"name": volume_name, "host": {"sourcePath": vol.source}}
                )
                mount_points.append(
                    {
                        "sourceVolume": volume_name,
                        "containerPath": vol.target,
                        "readOnly": vol.read_only,
                    }
                )

        return mount_points, volume_defs

    def _create_efs_volume_definition(
        self,
        volume_name: str,
        efs_conf: Dict[str, str],
    ) -> Dict[str, Any]:
        """Create an EFS volume definition for ECS."""
        volume_def: Dict[str, Any] = {
            "name": volume_name,
            "efsVolumeConfiguration": {
                "fileSystemId": efs_conf["file_system_id"],
                "transitEncryption": "ENABLED",
            },
        }

        if "access_point_id" in efs_conf:
            volume_def["efsVolumeConfiguration"]["authorizationConfig"] = {
                "accessPointId": efs_conf["access_point_id"],
                "iam": "DISABLED",
            }

        if "root_directory" in efs_conf and "access_point_id" not in efs_conf:
            volume_def["efsVolumeConfiguration"]["rootDirectory"] = efs_conf[
                "root_directory"
            ]

        if efs_conf.get("iam_enabled"):
            if "authorizationConfig" in volume_def["efsVolumeConfiguration"]:
                volume_def["efsVolumeConfiguration"]["authorizationConfig"][
                    "iam"
                ] = "ENABLED"
            else:
                volume_def["efsVolumeConfiguration"]["authorizationConfig"] = {
                    "iam": "ENABLED"
                }

        return volume_def

    def _convert_efs_volumes(
        self,
        named_volumes: Dict[str, Dict[str, Any]],
        efs_config: Dict[str, Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """Generate EFS volume definitions from configuration."""
        volume_defs = []

        for volume_name, efs_conf in efs_config.items():
            if "file_system_id" not in efs_conf:
                self._conversion_warnings.append(
                    f"EFS config for volume '{volume_name}' missing file_system_id"
                )
                continue

            volume_def = self._create_efs_volume_definition(volume_name, efs_conf)
            volume_defs.append(volume_def)

            self.log_debug(
                f"Created EFS volume definition for '{volume_name}' "
                f"with filesystem {efs_conf['file_system_id']}"
            )

        return volume_defs

    # ------------------------------------------------------------------
    # Misc converters
    # ------------------------------------------------------------------

    def _convert_depends_on(self, depends_on: Any) -> List[Dict[str, str]]:
        """Convert depends_on to ECS container dependencies."""
        dependencies = []

        if isinstance(depends_on, list):
            for dep in depends_on:
                if isinstance(dep, str):
                    dependencies.append(
                        {"containerName": sanitize_name(dep), "condition": "START"}
                    )
                elif isinstance(dep, dict):
                    for name, config in dep.items():
                        condition = (
                            "HEALTHY"
                            if config.get("condition") == "service_healthy"
                            else "START"
                        )
                        dependencies.append(
                            {
                                "containerName": sanitize_name(name),
                                "condition": condition,
                            }
                        )
        elif isinstance(depends_on, dict):
            for name, config in depends_on.items():
                condition = (
                    "HEALTHY"
                    if config.get("condition") == "service_healthy"
                    else "START"
                )
                dependencies.append(
                    {"containerName": sanitize_name(name), "condition": condition}
                )

        return dependencies

    def _get_default_log_config(
        self, container_name: str, region: str = "us-east-1"
    ) -> Dict[str, Any]:
        """Get default CloudWatch Logs configuration."""
        return {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{container_name}",
                "awslogs-region": region,
                "awslogs-stream-prefix": "ecs",
                "awslogs-create-group": "true",
            },
        }

    def _calculate_fargate_resources(
        self,
        total_cpu: int,
        total_memory: int,
        override_cpu: Optional[str],
        override_memory: Optional[str],
    ) -> Tuple[int, int]:
        """
        Calculate valid Fargate CPU/memory combination.

        Fargate has specific valid combinations, so we need to
        round up to the nearest valid values.
        """
        if override_cpu and override_memory:
            return int(override_cpu), int(override_memory)

        cpu_values = sorted([int(c) for c in FARGATE_CPU_MEMORY_MAP.keys()])
        target_cpu = int(override_cpu) if override_cpu else total_cpu

        selected_cpu = 256
        for cpu in cpu_values:
            if cpu >= target_cpu:
                selected_cpu = cpu
                break
        else:
            selected_cpu = cpu_values[-1]

        allowed_memory = FARGATE_CPU_MEMORY_MAP[str(selected_cpu)]
        target_memory = int(override_memory) if override_memory else total_memory

        selected_memory = allowed_memory[0]
        for mem in allowed_memory:
            if mem >= target_memory:
                selected_memory = mem
                break
        else:
            selected_memory = allowed_memory[-1]

        return selected_cpu, selected_memory
