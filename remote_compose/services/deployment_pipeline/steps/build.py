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

    def execute(self, context: PipelineContext) -> StepResult:
        """Build and push images to ECR, deduplicating shared build contexts."""
        if context.dry_run:
            return self._dry_run(context)

        ecr_service = context.services.ecr
        image_build_service = context.services.image_build

        build_services = context.preprocessed.get_build_services()
        compose_dir = str(context.compose_dir)

        built_count = 0
        skipped_count = 0
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

            # Check if we already have a shared image for this build key
            if build_key in context.shared_images:
                ecr_uri = context.shared_images[build_key]
                for svc_name, svc in group:
                    svc.image_name = ecr_uri
                    svc.config['image'] = ecr_uri
                    shared_count += 1
                    self.emit_event(
                        'image_shared',
                        service=svc_name,
                        build_key=build_key,
                        image_uri=ecr_uri
                    )
                continue

            # Get ECR repository info for the primary service
            repo = context.ecr_repositories.get(primary_name)
            if not repo:
                return StepResult.fail(
                    f"No ECR repository found for service '{primary_name}'"
                )

            ecr_uri = f"{repo['repository_uri']}:{context.image_tag}"
            repo_name = f"{context.project_name}/{primary_name}"

            # Check if we need to rebuild
            should_build = context.force_rebuild
            if not should_build:
                try:
                    exists = ecr_service.image_exists(
                        repository=repo_name,
                        tag=context.image_tag,
                        region=context.cluster.aws_region,
                        credential=context.cluster.aws_credential,
                    )
                    should_build = not exists
                except Exception:
                    should_build = True

            if not should_build:
                # Image exists, apply to all services in the group
                for svc_name, svc in group:
                    svc.image_name = ecr_uri
                    svc.config['image'] = ecr_uri
                skipped_count += 1
                context.shared_images[build_key] = ecr_uri
                self.emit_event(
                    'image_skipped',
                    service=primary_name,
                    reason="Image already exists in ECR"
                )
                # Count additional services as shared
                shared_count += len(group) - 1
                continue

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
                # Build and push
                build_result = image_build_service.build_and_push(
                    service_name=primary_name,
                    context=build_context,
                    dockerfile=dockerfile_path,
                    ecr_uri=ecr_uri,
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

                # Record as shared image for this build key
                context.shared_images[build_key] = ecr_uri

                # Update all services in the group with the built ECR URI
                for svc_name, svc in group:
                    svc.image_name = ecr_uri
                    svc.config['image'] = ecr_uri

                context.built_images.append(ecr_uri)
                built_count += 1
                # Count additional services sharing this build as shared
                shared_count += len(group) - 1

                self.emit_event(
                    'image_build_completed',
                    service=primary_name,
                    image_uri=ecr_uri,
                    shared_with=[name for name, _ in group[1:]]
                )

            except Exception as e:
                return StepResult.fail(
                    f"Image build failed for '{primary_name}': {e}",
                    error=e
                )

        # Generate summary
        summary_parts = []
        if built_count > 0:
            summary_parts.append(f"built {built_count}")
        if skipped_count > 0:
            summary_parts.append(f"skipped {skipped_count} (cached)")
        if shared_count > 0:
            summary_parts.append(f"shared {shared_count} (deduplicated)")

        return StepResult.ok(
            f"Images: {', '.join(summary_parts) or 'none to build'}"
        )

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

        # Assign placeholder URIs, reusing for shared build contexts
        for build_key, group in build_groups.items():
            primary_name = group[0][0]
            placeholder_uri = (
                f"{context.account_id}.dkr.ecr.{context.cluster.aws_region}"
                f".amazonaws.com/{context.project_name}/{primary_name}"
                f":{context.image_tag}"
            )
            context.shared_images[build_key] = placeholder_uri
            for svc_name, svc in group:
                svc.image_name = placeholder_uri
                svc.config['image'] = placeholder_uri

        unique_builds = len(build_groups)
        total_services = sum(len(g) for g in build_groups.values())
        shared = total_services - unique_builds

        msg = f"[DRY RUN] Would build {unique_builds} unique images for {total_services} services"
        if shared > 0:
            msg += f" ({shared} shared via deduplication)"

        return StepResult.ok(msg)
