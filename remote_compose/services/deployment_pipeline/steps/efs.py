"""
EFS setup step.

Handles creating EFS file systems and access points for named volumes.
Supports reusing existing EFS filesystems and access points across deployments
for persistent data storage.
"""

from ..step import PipelineStep, StepResult
from ..context import PipelineContext


class SetupEFSVolumesStep(PipelineStep):
    """
    Create or reuse EFS file systems and access points for named volumes.

    For each named volume in the compose file:
    1. Check for existing EFS file system (by project name)
    2. Reuse existing access points where available
    3. Create new access points only for new volumes
    4. Store configuration for task definition conversion

    This ensures data persistence across deployments.
    """

    def __init__(self):
        super().__init__("SetupEFSVolumes")
        self._created_access_points = []

    def should_run(self, context: PipelineContext) -> bool:
        """Only run if there are named volumes that need EFS."""
        return context.create_efs_for_volumes and context.has_named_volumes

    def execute(self, context: PipelineContext) -> StepResult:
        """Set up EFS for named volumes, reusing existing resources."""
        self._created_access_points = []
        if context.dry_run:
            return self._dry_run(context)

        cluster = context.cluster

        # Get VPC and subnet info
        vpc_id, subnet_ids = self._get_vpc_info(cluster)

        if not vpc_id or not subnet_ids:
            context.add_warning(
                "Cannot create EFS: missing VPC/subnet configuration on cluster"
            )
            return StepResult.ok("Skipped EFS setup: missing network configuration")

        from ....models import EFSFileSystem

        efs_service = context.services.efs
        efs_name = f"{cluster.name}-{context.project_name}-efs"

        # Track stats for result message
        reused_count = 0
        created_count = 0

        try:
            # First, check if we have an existing EFS model for this project
            existing_efs_model = EFSFileSystem.objects.filter(
                cluster=cluster, name=efs_name
            ).first()

            # If we have a model, verify the EFS still exists in AWS
            if existing_efs_model:
                try:
                    efs_service.describe_file_system(
                        existing_efs_model.aws_file_system_id,
                        region=cluster.aws_region,
                        credential=cluster.aws_credential,
                    )
                    self.emit_event(
                        "efs_reused",
                        file_system_id=existing_efs_model.aws_file_system_id,
                        name=efs_name,
                    )
                except Exception:
                    # EFS was deleted in AWS, remove stale record
                    self.emit_event(
                        "efs_stale_record_removed",
                        file_system_id=existing_efs_model.aws_file_system_id,
                        name=efs_name,
                    )
                    existing_efs_model.delete()
                    existing_efs_model = None

            # Get or create the EFS file system
            efs_fs = efs_service.get_or_create_file_system(
                name=efs_name,
                region=cluster.aws_region,
                vpc_id=vpc_id,
                subnet_ids=subnet_ids,
                security_group_ids=cluster.security_group_ids,
                credential=cluster.aws_credential,
            )

            file_system_id = efs_fs["file_system_id"]

            context.track_resource(
                resource_type="efs_file_system",
                resource_id=file_system_id,
                name=efs_name,
            )

            # Get existing access points from database (if model exists)
            existing_access_points = {}
            if existing_efs_model and existing_efs_model.access_points:
                existing_access_points = existing_efs_model.access_points

            # Process each named volume
            for volume_name in context.preprocessed.named_volumes.keys():
                ap_name = f"{context.project_name}-{volume_name}"
                path = f"/{context.project_name}/{volume_name}"

                # Check if we already have this access point
                if volume_name in existing_access_points:
                    existing_ap_id = existing_access_points[volume_name]

                    # Handle both dict format and direct ID format
                    if isinstance(existing_ap_id, dict):
                        access_point_id = existing_ap_id.get("access_point_id")
                    else:
                        access_point_id = existing_ap_id

                    # Verify the access point still exists in AWS
                    try:
                        efs_service.describe_access_point(
                            access_point_id,
                            region=cluster.aws_region,
                            credential=cluster.aws_credential,
                        )
                        # Access point exists, reuse it
                        context.efs_config[volume_name] = {
                            "file_system_id": file_system_id,
                            "access_point_id": access_point_id,
                        }
                        reused_count += 1

                        self.emit_event(
                            "access_point_reused",
                            volume=volume_name,
                            access_point_id=access_point_id,
                        )
                        continue
                    except Exception:
                        # Access point was deleted, will create new one
                        pass

                # Create new access point (either first time or recreating)
                access_point = efs_service.create_access_point(
                    file_system_id=file_system_id,
                    path=path,
                    name=ap_name,
                    region=cluster.aws_region,
                    credential=cluster.aws_credential,
                )

                access_point_id = access_point["access_point_id"]

                context.efs_config[volume_name] = {
                    "file_system_id": file_system_id,
                    "access_point_id": access_point_id,
                }

                self._created_access_points.append(access_point_id)
                created_count += 1

                context.track_resource(
                    resource_type="efs_access_point",
                    resource_id=access_point_id,
                    volume_name=volume_name,
                    file_system_id=file_system_id,
                )

                self.emit_event(
                    "access_point_created",
                    volume=volume_name,
                    access_point_id=access_point_id,
                )

            # Store or update EFSFileSystem model with new access point mappings
            try:
                if existing_efs_model:
                    efs_model = existing_efs_model
                    efs_model.aws_file_system_id = file_system_id
                else:
                    efs_model, _ = EFSFileSystem.objects.get_or_create(
                        aws_file_system_id=file_system_id,
                        defaults={
                            "name": efs_name,
                            "cluster": cluster,
                            "aws_region": cluster.aws_region,
                            "vpc_id": vpc_id,
                        },
                    )

                # Update access points mapping (store just the IDs for simplicity)
                access_point_mapping = {
                    vol: cfg["access_point_id"]
                    for vol, cfg in context.efs_config.items()
                }
                efs_model.access_points = access_point_mapping
                efs_model.save()
                context.efs_file_system = efs_model
            except Exception:
                # Model save failure shouldn't stop the pipeline
                pass

            # Build result message
            total = reused_count + created_count
            if reused_count > 0 and created_count > 0:
                msg = f"EFS configured: {reused_count} reused, {created_count} created ({total} total)"
            elif reused_count > 0:
                msg = f"EFS configured: {reused_count} access points reused (persistent storage)"
            else:
                msg = f"EFS configured: {created_count} access points created"

            return StepResult.ok(msg)

        except Exception as e:
            return StepResult.fail(f"EFS setup failed: {e}", error=e)

    def _dry_run(self, context: PipelineContext) -> StepResult:
        """Simulate EFS setup."""
        volume_count = len(context.preprocessed.named_volumes)
        return StepResult.ok(
            f"[DRY RUN] Would create EFS with {volume_count} access points"
        )

    def _get_vpc_info(self, cluster):
        """Get VPC and subnet information from cluster or AWS."""
        vpc_id = cluster.vpc_id
        subnet_ids = cluster.subnet_ids or []

        if subnet_ids and not vpc_id:
            # Get VPC ID from first subnet
            from ...aws_client_factory import get_aws_client_factory

            aws_factory = get_aws_client_factory()
            ec2 = aws_factory.get_client(
                "ec2", region=cluster.aws_region, credential=cluster.aws_credential
            )

            try:
                response = ec2.describe_subnets(SubnetIds=[subnet_ids[0]])
                if response.get("Subnets"):
                    vpc_id = response["Subnets"][0].get("VpcId")
            except Exception:
                pass

        return vpc_id, subnet_ids

    def cleanup(self, context: PipelineContext) -> None:
        """
        Clean up EFS resources on failure.

        IMPORTANT: We intentionally do NOT delete access points during cleanup
        because they represent persistent data storage. EFS access points
        contain database data, uploads, and other valuable persistent data
        that should survive deployment failures.

        The EFS file system and access points will be reused on the next
        deployment attempt thanks to the reuse logic in execute().
        """
        # Intentionally empty - preserve EFS access points for data persistence
        # The access points can be cleaned up manually if needed, or will be
        # reused by the next deployment.
        pass
