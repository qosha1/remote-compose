"""
Image build and push step.

Handles building Docker images and pushing them to ECR.
"""

import os
from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class BuildAndPushImagesStep(PipelineStep):
    """
    Build Docker images and push to ECR.

    For each service with a build configuration:
    1. Check if image already exists in ECR (unless force_rebuild)
    2. Build the Docker image
    3. Push to ECR
    4. Update service configuration with ECR URI
    """

    def __init__(self):
        super().__init__("BuildAndPushImages")

    def should_run(self, context: PipelineContext) -> bool:
        """Only run if we need to build images."""
        return context.build_images and context.has_build_services

    def _get_build_key(self, service) -> str:
        """
        Generate a normalized build context key for deduplication.

        Services sharing the same context path and dockerfile will produce
        the same key, allowing the image to be built once and reused.

        Args:
            service: PreprocessedService with build_info

        Returns:
            Normalized string key: "{context}:{dockerfile}"
        """
        context = os.path.normpath(service.build_info.context)
        dockerfile = service.build_info.dockerfile
        return f"{context}:{dockerfile}"

    def _retag_and_push(self, source_uri: str, target_uri: str) -> None:
        """Tag and push an already-built image to a different ECR repo."""
        import subprocess
        subprocess.run(
            ["docker", "tag", source_uri, target_uri],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["docker", "push", target_uri],
            check=True, capture_output=True, text=True, timeout=600,
        )

    def _assign_per_service_uris(self, context, group, primary_uri, primary_name):
        """
        For each service in a shared build group, ensure it has its own ECR URI.

        Builds are deduplicated (one docker build), but each service gets its
        own ECR repo so task definitions reference the correct image.
        """
        service_uris = {}
        for svc_name, svc in group:
            repo = context.ecr_repositories.get(svc_name)
            if repo:
                svc_uri = f"{repo['repository_uri']}:{context.image_tag}"
            else:
                # Fallback: if no dedicated repo, use primary
                svc_uri = primary_uri

            if svc_uri != primary_uri:
                self._retag_and_push(primary_uri, svc_uri)

            svc.image_name = svc_uri
            svc.config['image'] = svc_uri
            service_uris[svc_name] = svc_uri
        return service_uris

    def execute(self, context: PipelineContext) -> StepResult:
        """Build and push images to ECR, deduplicating shared build contexts."""
        if context.dry_run:
            return self._dry_run(context)

        image_build_service = context.services.image_build

        build_services = context.preprocessed.get_build_services()
        compose_dir = str(context.compose_dir)

        built_count = 0
        shared_count = 0

        # Group services by build context key to detect shared builds
        # Maps build_key -> list of (service_name, service) tuples
        build_groups: dict = {}
        for service_name, service in build_services.items():
            if not service.build_info:
                continue
            build_key = self._get_build_key(service)
            if build_key not in build_groups:
                build_groups[build_key] = []
            build_groups[build_key].append((service_name, service))

        for build_key, group in build_groups.items():
            # Use the first service in the group as the "primary" builder
            primary_name, primary_service = group[0]

            # Get ECR repository info for the primary service
            repo = context.ecr_repositories.get(primary_name)
            if not repo:
                return StepResult.fail(
                    f"No ECR repository found for service '{primary_name}'"
                )

            primary_uri = f"{repo['repository_uri']}:{context.image_tag}"

            # Resolve build context path
            build_context = primary_service.build_info.context
            if not os.path.isabs(build_context):
                build_context = os.path.join(compose_dir, build_context)

            # Resolve dockerfile path
            dockerfile = primary_service.build_info.dockerfile
            dockerfile_path = os.path.join(build_context, dockerfile)
            if not os.path.exists(dockerfile_path):
                dockerfile_path = None  # Let Docker find Dockerfile

            self.emit_event(
                'image_build_started',
                service=primary_name,
                context=build_context
            )

            try:
                # Build and push primary image
                build_result = image_build_service.build_and_push(
                    service_name=primary_name,
                    context=build_context,
                    dockerfile=dockerfile_path,
                    ecr_uri=primary_uri,
                    build_args=primary_service.build_info.args,
                    target=primary_service.build_info.target,
                )

                if not build_result.success:
                    error_msg = "Unknown build error"
                    if build_result.build_result:
                        error_msg = build_result.build_result.error or error_msg
                    return StepResult.fail(
                        f"Image build failed for '{primary_name}': {error_msg}"
                    )

                # Retag and push to each service's own ECR repo
                service_uris = self._assign_per_service_uris(
                    context, group, primary_uri, primary_name
                )

                # Store primary URI for shared_images (backward compat)
                context.shared_images[build_key] = primary_uri

                context.built_images.append(primary_uri)
                built_count += 1
                shared_count += len(group) - 1

                self.emit_event(
                    'image_build_completed',
                    service=primary_name,
                    image_uri=primary_uri,
                    shared_with=[name for name, _ in group[1:]]
                )

            except Exception as e:
                return StepResult.fail(
                    f"Image build failed for '{primary_name}': {e}",
                    error=e
                )

        summary_parts = [f"built {built_count}"]
        if shared_count > 0:
            summary_parts.append(f"shared {shared_count} (deduplicated)")

        return StepResult.ok(f"Images: {', '.join(summary_parts)}")

    def _dry_run(self, context: PipelineContext) -> StepResult:
        """Simulate building images, accounting for shared build contexts."""
        build_services = context.preprocessed.get_build_services()

        # Group by build key to show accurate deduplication info
        build_groups: dict = {}
        for service_name, service in build_services.items():
            if not service.build_info:
                continue
            build_key = self._get_build_key(service)
            if build_key not in build_groups:
                build_groups[build_key] = []
            build_groups[build_key].append((service_name, service))

        # Assign per-service placeholder URIs (each service gets its own repo)
        for build_key, group in build_groups.items():
            primary_name = group[0][0]
            primary_uri = (
                f"{context.account_id}.dkr.ecr.{context.cluster.aws_region}"
                f".amazonaws.com/{context.project_name}/{primary_name}"
                f":{context.image_tag}"
            )
            context.shared_images[build_key] = primary_uri
            for svc_name, svc in group:
                svc_uri = (
                    f"{context.account_id}.dkr.ecr.{context.cluster.aws_region}"
                    f".amazonaws.com/{context.project_name}/{svc_name}"
                    f":{context.image_tag}"
                )
                svc.image_name = svc_uri
                svc.config['image'] = svc_uri

        unique_builds = len(build_groups)
        total_services = sum(len(g) for g in build_groups.values())
        shared = total_services - unique_builds

        msg = f"[DRY RUN] Would build {unique_builds} unique images for {total_services} services"
        if shared > 0:
            msg += f" ({shared} shared via deduplication)"

        return StepResult.ok(msg)
