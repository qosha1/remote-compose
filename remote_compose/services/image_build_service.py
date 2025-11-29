"""
Service for building Docker images locally and pushing to ECR.

Provides functionality for:
- Building Docker images with subprocess
- Pushing images to ECR
- Batch operations for multi-service builds
- Parallel build support using ThreadPoolExecutor
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable, TYPE_CHECKING
import subprocess
import shutil
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..exceptions import DockerError, AWSError
from ..conf import get_setting
from .base import BaseService

if TYPE_CHECKING:
    from .ecr_service import ECRService
    from ..models import SecureCredential


@dataclass
class ImageBuildResult:
    """Result of a Docker image build operation."""
    success: bool
    image_id: Optional[str] = None
    image_tag: Optional[str] = None
    build_log: str = ""
    duration: float = 0.0
    error: Optional[str] = None


@dataclass
class ImagePushResult:
    """Result of a Docker image push operation."""
    success: bool
    digest: Optional[str] = None
    push_log: str = ""
    duration: float = 0.0
    error: Optional[str] = None


@dataclass
class BuildAndPushResult:
    """Combined result of build and push operations."""
    service_name: str
    build_result: Optional[ImageBuildResult] = None
    push_result: Optional[ImagePushResult] = None
    ecr_uri: Optional[str] = None
    success: bool = False


class ImageBuildError(DockerError):
    """Raised when Docker image build fails."""

    def __init__(self, message: str, build_log: str = "", **kwargs):
        super().__init__(message, **kwargs)
        self.details['build_log'] = build_log


class ImagePushError(DockerError):
    """Raised when Docker image push fails."""

    def __init__(self, message: str, push_log: str = "", **kwargs):
        super().__init__(message, **kwargs)
        self.details['push_log'] = push_log


class ImageBuildService(BaseService):
    """
    Service for building Docker images and pushing to container registries.

    Handles local Docker builds, tagging, and pushing to ECR.
    Supports streaming output for progress display.
    """

    def __init__(
        self,
        ecr_service: Optional["ECRService"] = None,
        default_platform: str = "linux/amd64",
        **kwargs
    ):
        """
        Initialize the image build service.

        Args:
            ecr_service: Optional ECRService for authentication and repo management
            default_platform: Default platform for builds (linux/amd64 for Fargate)
        """
        super().__init__(**kwargs)
        self._ecr_service = ecr_service
        self.default_platform = default_platform
        self._docker_available: Optional[bool] = None

    @property
    def ecr_service(self) -> "ECRService":
        """Get the ECR service, creating it if necessary."""
        if self._ecr_service is None:
            from .ecr_service import ECRService
            self._ecr_service = ECRService()
        return self._ecr_service

    def _check_docker_available(self) -> bool:
        """Check if Docker is available on the system."""
        if self._docker_available is not None:
            return self._docker_available

        docker_path = shutil.which("docker")
        if not docker_path:
            self._docker_available = False
            return False

        try:
            result = subprocess.run(
                ["docker", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self._docker_available = result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            self._docker_available = False

        return self._docker_available

    def _ensure_docker_available(self) -> None:
        """Raise error if Docker is not available."""
        if not self._check_docker_available():
            raise ImageBuildError(
                "Docker is not installed or not running. "
                "Please install Docker and ensure the Docker daemon is running."
            )

    def build_image(
        self,
        context: str,
        dockerfile: Optional[str] = None,
        image_tag: str = "latest",
        build_args: Optional[Dict[str, str]] = None,
        target: Optional[str] = None,
        platform: Optional[str] = None,
        stream_output: bool = False,
        output_callback: Optional[Callable[[str], None]] = None,
        timeout: int = 600,
    ) -> ImageBuildResult:
        """
        Build a Docker image locally.

        Args:
            context: Path to the build context directory
            dockerfile: Path to Dockerfile (relative to context or absolute)
            image_tag: Tag for the built image (e.g., "myapp:v1.0")
            build_args: Dictionary of build arguments
            target: Multi-stage build target
            platform: Target platform (defaults to linux/amd64 for Fargate)
            stream_output: Whether to stream build output
            output_callback: Callback function for streaming output lines
            timeout: Build timeout in seconds

        Returns:
            ImageBuildResult with success status, image_id, and build log
        """
        self._ensure_docker_available()
        start_time = time.time()

        platform = platform or self.default_platform

        cmd = [
            "docker", "build",
            "--platform", platform,
            "-t", image_tag,
        ]

        if dockerfile:
            cmd.extend(["-f", dockerfile])

        if target:
            cmd.extend(["--target", target])

        if build_args:
            for key, value in build_args.items():
                cmd.extend(["--build-arg", f"{key}={value}"])

        cmd.append(context)

        self.log_info(f"Building image: {image_tag}")
        self.log_debug(f"Build command: {' '.join(cmd)}")

        build_log = []
        image_id = None

        try:
            if stream_output:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )

                for line in iter(process.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        build_log.append(line)
                        if output_callback:
                            output_callback(line)

                        if "Successfully built" in line:
                            parts = line.split()
                            if len(parts) >= 3:
                                image_id = parts[-1]

                process.wait(timeout=timeout)
                returncode = process.returncode
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                returncode = result.returncode
                build_log = (result.stdout + result.stderr).splitlines()

                for line in build_log:
                    if "Successfully built" in line:
                        parts = line.split()
                        if len(parts) >= 3:
                            image_id = parts[-1]

            duration = time.time() - start_time

            if returncode != 0:
                error_msg = f"Docker build failed with exit code {returncode}"
                self.log_error(error_msg)
                return ImageBuildResult(
                    success=False,
                    image_tag=image_tag,
                    build_log="\n".join(build_log),
                    duration=duration,
                    error=error_msg,
                )

            if not image_id:
                image_id = self.get_image_id(image_tag)

            self.log_info(f"Successfully built image: {image_tag} ({image_id})")
            self.notify_observers(
                'image_built',
                image_tag=image_tag,
                image_id=image_id,
                duration=duration,
            )

            return ImageBuildResult(
                success=True,
                image_id=image_id,
                image_tag=image_tag,
                build_log="\n".join(build_log),
                duration=duration,
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            error_msg = f"Docker build timed out after {timeout} seconds"
            self.log_error(error_msg)
            return ImageBuildResult(
                success=False,
                image_tag=image_tag,
                build_log="\n".join(build_log),
                duration=duration,
                error=error_msg,
            )
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Docker build failed: {e}"
            self.log_error(error_msg, exc_info=True)
            return ImageBuildResult(
                success=False,
                image_tag=image_tag,
                build_log="\n".join(build_log),
                duration=duration,
                error=error_msg,
            )

    def push_image(
        self,
        image_tag: str,
        stream_output: bool = False,
        output_callback: Optional[Callable[[str], None]] = None,
        timeout: int = 600,
    ) -> ImagePushResult:
        """
        Push a Docker image to the registry.

        Args:
            image_tag: Full image tag including registry (e.g., "123456789.dkr.ecr.us-east-1.amazonaws.com/myapp:v1.0")
            stream_output: Whether to stream push output
            output_callback: Callback function for streaming output lines
            timeout: Push timeout in seconds

        Returns:
            ImagePushResult with success status, digest, and push log
        """
        self._ensure_docker_available()
        start_time = time.time()

        cmd = ["docker", "push", image_tag]

        self.log_info(f"Pushing image: {image_tag}")
        push_log = []
        digest = None

        try:
            if stream_output:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )

                for line in iter(process.stdout.readline, ''):
                    line = line.rstrip()
                    if line:
                        push_log.append(line)
                        if output_callback:
                            output_callback(line)

                        if "digest:" in line.lower():
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if part.lower() == "digest:" and i + 1 < len(parts):
                                    digest = parts[i + 1]
                                    break

                process.wait(timeout=timeout)
                returncode = process.returncode
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                returncode = result.returncode
                push_log = (result.stdout + result.stderr).splitlines()

                for line in push_log:
                    if "digest:" in line.lower():
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part.lower() == "digest:" and i + 1 < len(parts):
                                digest = parts[i + 1]
                                break

            duration = time.time() - start_time

            if returncode != 0:
                error_msg = f"Docker push failed with exit code {returncode}"
                self.log_error(error_msg)
                return ImagePushResult(
                    success=False,
                    push_log="\n".join(push_log),
                    duration=duration,
                    error=error_msg,
                )

            self.log_info(f"Successfully pushed image: {image_tag}")
            self.notify_observers(
                'image_pushed',
                image_tag=image_tag,
                digest=digest,
                duration=duration,
            )

            return ImagePushResult(
                success=True,
                digest=digest,
                push_log="\n".join(push_log),
                duration=duration,
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            error_msg = f"Docker push timed out after {timeout} seconds"
            self.log_error(error_msg)
            return ImagePushResult(
                success=False,
                push_log="\n".join(push_log),
                duration=duration,
                error=error_msg,
            )
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Docker push failed: {e}"
            self.log_error(error_msg, exc_info=True)
            return ImagePushResult(
                success=False,
                push_log="\n".join(push_log),
                duration=duration,
                error=error_msg,
            )

    def tag_image(
        self,
        source_tag: str,
        target_tag: str,
    ) -> bool:
        """
        Tag an existing image with a new tag.

        Args:
            source_tag: Source image tag
            target_tag: Target image tag

        Returns:
            True if tagging succeeded, False otherwise
        """
        self._ensure_docker_available()

        cmd = ["docker", "tag", source_tag, target_tag]

        self.log_debug(f"Tagging image: {source_tag} -> {target_tag}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                self.log_error(f"Failed to tag image: {result.stderr}")
                return False

            self.log_info(f"Tagged image: {source_tag} -> {target_tag}")
            return True

        except (subprocess.TimeoutExpired, OSError) as e:
            self.log_error(f"Failed to tag image: {e}")
            return False

    def image_exists_locally(self, image_tag: str) -> bool:
        """
        Check if an image exists locally.

        Args:
            image_tag: Image tag to check

        Returns:
            True if image exists locally, False otherwise
        """
        if not self._check_docker_available():
            return False

        cmd = ["docker", "image", "inspect", image_tag]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def get_image_id(self, image_tag: str) -> Optional[str]:
        """
        Get the image ID for a given tag.

        Args:
            image_tag: Image tag to look up

        Returns:
            Image ID or None if not found
        """
        if not self._check_docker_available():
            return None

        cmd = ["docker", "image", "inspect", "--format", "{{.Id}}", image_tag]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                image_id = result.stdout.strip()
                if image_id.startswith("sha256:"):
                    image_id = image_id[7:19]
                return image_id
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None

    def build_and_push(
        self,
        service_name: str,
        context: str,
        dockerfile: Optional[str] = None,
        ecr_uri: str = "",
        build_args: Optional[Dict[str, str]] = None,
        target: Optional[str] = None,
        stream_output: bool = False,
        output_callback: Optional[Callable[[str], None]] = None,
        build_timeout: int = 600,
        push_timeout: int = 600,
    ) -> BuildAndPushResult:
        """
        Build an image, tag it for ECR, and push it.

        Args:
            service_name: Name of the service being built
            context: Path to build context
            dockerfile: Optional path to Dockerfile
            ecr_uri: ECR repository URI (e.g., "123456789.dkr.ecr.us-east-1.amazonaws.com/myapp")
            build_args: Optional build arguments
            target: Optional multi-stage build target
            stream_output: Whether to stream output
            output_callback: Callback for streaming output
            build_timeout: Timeout for build in seconds
            push_timeout: Timeout for push in seconds

        Returns:
            BuildAndPushResult with combined results
        """
        result = BuildAndPushResult(
            service_name=service_name,
            ecr_uri=ecr_uri,
        )

        local_tag = f"{service_name}:latest"

        build_result = self.build_image(
            context=context,
            dockerfile=dockerfile,
            image_tag=local_tag,
            build_args=build_args,
            target=target,
            stream_output=stream_output,
            output_callback=output_callback,
            timeout=build_timeout,
        )
        result.build_result = build_result

        if not build_result.success:
            self.log_error(f"Build failed for {service_name}: {build_result.error}")
            return result

        if not self.tag_image(local_tag, ecr_uri):
            result.build_result = ImageBuildResult(
                success=False,
                image_id=build_result.image_id,
                image_tag=local_tag,
                build_log=build_result.build_log,
                duration=build_result.duration,
                error=f"Failed to tag image for ECR: {ecr_uri}",
            )
            return result

        push_result = self.push_image(
            image_tag=ecr_uri,
            stream_output=stream_output,
            output_callback=output_callback,
            timeout=push_timeout,
        )
        result.push_result = push_result

        if push_result.success:
            result.success = True
            self.log_info(f"Successfully built and pushed {service_name} to {ecr_uri}")
            self.notify_observers(
                'build_and_push_complete',
                service_name=service_name,
                ecr_uri=ecr_uri,
                build_duration=build_result.duration,
                push_duration=push_result.duration,
            )
        else:
            self.log_error(f"Push failed for {service_name}: {push_result.error}")

        return result

    def build_and_push_multiple(
        self,
        services: List[Dict[str, Any]],
        project_name: str,
        ecr_prefix: str,
        parallel: bool = False,
        max_workers: int = 4,
        stream_output: bool = False,
        output_callback: Optional[Callable[[str, str], None]] = None,
    ) -> List[BuildAndPushResult]:
        """
        Build and push multiple service images.

        Args:
            services: List of dicts with build info:
                - name: Service name
                - context: Build context path
                - dockerfile: Optional Dockerfile path
                - build_args: Optional build arguments
                - target: Optional multi-stage target
            project_name: Project name for image tagging
            ecr_prefix: ECR registry prefix (e.g., "123456789.dkr.ecr.us-east-1.amazonaws.com")
            parallel: Whether to build in parallel
            max_workers: Max parallel workers
            stream_output: Whether to stream output
            output_callback: Callback for output (receives service_name and line)

        Returns:
            List of BuildAndPushResult for each service
        """
        results = []

        def build_service(service: Dict[str, Any]) -> BuildAndPushResult:
            service_name = service['name']
            ecr_uri = f"{ecr_prefix}/{project_name}-{service_name}:latest"

            callback = None
            if output_callback:
                callback = lambda line: output_callback(service_name, line)

            return self.build_and_push(
                service_name=service_name,
                context=service['context'],
                dockerfile=service.get('dockerfile'),
                ecr_uri=ecr_uri,
                build_args=service.get('build_args'),
                target=service.get('target'),
                stream_output=stream_output,
                output_callback=callback,
            )

        if parallel and len(services) > 1:
            self.log_info(f"Building {len(services)} services in parallel (max {max_workers} workers)")

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_service = {
                    executor.submit(build_service, service): service
                    for service in services
                }

                for future in as_completed(future_to_service):
                    service = future_to_service[future]
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        self.log_error(f"Build failed for {service['name']}: {e}")
                        results.append(BuildAndPushResult(
                            service_name=service['name'],
                            success=False,
                            build_result=ImageBuildResult(
                                success=False,
                                error=str(e),
                            ),
                        ))
        else:
            self.log_info(f"Building {len(services)} services sequentially")
            for service in services:
                result = build_service(service)
                results.append(result)

        successful = sum(1 for r in results if r.success)
        self.log_info(f"Build complete: {successful}/{len(services)} services succeeded")

        self.notify_observers(
            'batch_build_complete',
            project_name=project_name,
            total=len(services),
            successful=successful,
            results=results,
        )

        return results

    def authenticate_ecr(
        self,
        region: Optional[str] = None,
        credential: Optional["SecureCredential"] = None,
        use_cli: bool = False,
    ) -> bool:
        """
        Authenticate Docker with ECR.

        Can use either boto3 via ECRService (preferred) or AWS CLI fallback.

        Args:
            region: AWS region (defaults to configured region)
            credential: Optional SecureCredential for AWS authentication
            use_cli: Force use of AWS CLI instead of boto3

        Returns:
            True if authentication succeeded
        """
        self._ensure_docker_available()

        region = region or get_setting('AWS_DEFAULT_REGION', 'us-east-1')
        self.log_info(f"Authenticating with ECR in {region}")

        if not use_cli:
            try:
                result = self.ecr_service.docker_login(
                    region=region,
                    credential=credential,
                    execute=True,
                )
                self.log_info(result)
                return True
            except Exception as e:
                self.log_warning(f"Boto3 ECR auth failed, falling back to CLI: {e}")

        return self._authenticate_ecr_cli(region)

    def _authenticate_ecr_cli(self, region: str) -> bool:
        """Fallback ECR authentication using AWS CLI."""
        cmd = [
            "aws", "ecr", "get-login-password",
            "--region", region,
        ]

        try:
            password_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if password_result.returncode != 0:
                self.log_error(f"Failed to get ECR login password: {password_result.stderr}")
                return False

            password = password_result.stdout.strip()

            sts_cmd = ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"]
            sts_result = subprocess.run(
                sts_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if sts_result.returncode != 0:
                self.log_error("Failed to get AWS account ID")
                return False

            account_id = sts_result.stdout.strip()
            registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

            login_cmd = [
                "docker", "login",
                "--username", "AWS",
                "--password-stdin",
                registry,
            ]

            login_result = subprocess.run(
                login_cmd,
                input=password,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if login_result.returncode != 0:
                self.log_error(f"Docker login failed: {login_result.stderr}")
                return False

            self.log_info(f"Successfully authenticated with ECR: {registry}")
            return True

        except subprocess.TimeoutExpired:
            self.log_error("ECR authentication timed out")
            return False
        except Exception as e:
            self.log_error(f"ECR authentication failed: {e}")
            return False

    def ensure_ecr_repository(
        self,
        repository_name: str,
        region: Optional[str] = None,
        credential: Optional["SecureCredential"] = None,
        use_cli: bool = False,
    ) -> Optional[str]:
        """
        Ensure an ECR repository exists, creating it if necessary.

        Args:
            repository_name: Name of the repository
            region: AWS region
            credential: Optional SecureCredential for AWS authentication
            use_cli: Force use of AWS CLI instead of boto3

        Returns:
            Repository URI or None if creation failed
        """
        region = region or get_setting('AWS_DEFAULT_REGION', 'us-east-1')

        if not use_cli:
            try:
                repo = self.ecr_service.get_or_create_repository(
                    name=repository_name,
                    region=region,
                    credential=credential,
                )
                uri = repo.get('repository_uri')
                self.log_info(f"ECR repository ready: {uri}")
                return uri
            except Exception as e:
                self.log_warning(f"Boto3 ECR operation failed, falling back to CLI: {e}")

        return self._ensure_ecr_repository_cli(repository_name, region)

    def _ensure_ecr_repository_cli(
        self,
        repository_name: str,
        region: str,
    ) -> Optional[str]:
        """Fallback ECR repository management using AWS CLI."""
        describe_cmd = [
            "aws", "ecr", "describe-repositories",
            "--repository-names", repository_name,
            "--region", region,
        ]

        try:
            result = subprocess.run(
                describe_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                repos = data.get('repositories', [])
                if repos:
                    uri = repos[0].get('repositoryUri')
                    self.log_debug(f"ECR repository exists: {uri}")
                    return uri

            self.log_info(f"Creating ECR repository: {repository_name}")

            create_cmd = [
                "aws", "ecr", "create-repository",
                "--repository-name", repository_name,
                "--region", region,
            ]

            create_result = subprocess.run(
                create_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if create_result.returncode != 0:
                self.log_error(f"Failed to create ECR repository: {create_result.stderr}")
                return None

            data = json.loads(create_result.stdout)
            uri = data.get('repository', {}).get('repositoryUri')
            self.log_info(f"Created ECR repository: {uri}")
            return uri

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            self.log_error(f"ECR repository operation failed: {e}")
            return None

    def cleanup_local_images(
        self,
        image_tags: List[str],
        force: bool = False,
    ) -> int:
        """
        Remove local images to free up disk space.

        Args:
            image_tags: List of image tags to remove
            force: Force removal even if containers are using the images

        Returns:
            Number of images successfully removed
        """
        if not self._check_docker_available():
            return 0

        removed = 0

        for tag in image_tags:
            cmd = ["docker", "rmi"]
            if force:
                cmd.append("-f")
            cmd.append(tag)

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if result.returncode == 0:
                    removed += 1
                    self.log_debug(f"Removed image: {tag}")
                else:
                    self.log_warning(f"Failed to remove image {tag}: {result.stderr}")

            except (subprocess.TimeoutExpired, OSError) as e:
                self.log_warning(f"Failed to remove image {tag}: {e}")

        self.log_info(f"Cleaned up {removed}/{len(image_tags)} local images")
        return removed
