"""
Service for AWS ECS Service Connect (Cloud Map) integration.

Provides functionality for managing Cloud Map namespaces and building
Service Connect configuration for ECS services, enabling service-to-service
communication via DNS-based discovery.
"""

import time
import uuid
from typing import Optional, Dict

from botocore.exceptions import ClientError

from ..models import ServiceConnectNamespace
from ..exceptions import NamespaceError
from .base import BaseService
from .aws_client_factory import AWSClientFactory, get_aws_client_factory


class ServiceConnectService(BaseService):
    """
    Service for managing ECS Service Connect namespaces and configuration.

    Creates Cloud Map HTTP namespaces for service-to-service discovery
    and builds the serviceConnectConfiguration dicts used by ECS CreateService.
    """

    def __init__(self, aws_factory: Optional[AWSClientFactory] = None, **kwargs):
        super().__init__(**kwargs)
        self.aws_factory = aws_factory or get_aws_client_factory()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get_or_create_namespace(
        self,
        cluster,
        namespace_name: Optional[str] = None,
        region: Optional[str] = None,
        credential=None,
    ) -> ServiceConnectNamespace:
        """
        Get or create an HTTP namespace in AWS Cloud Map.

        If a namespace with the given name already exists, it is reused.
        The default name is the cluster name.

        Args:
            cluster: ECSCluster model instance.
            namespace_name: Namespace name (defaults to cluster.name).
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            ServiceConnectNamespace model instance.

        Raises:
            NamespaceError: If namespace creation or lookup fails.
        """
        sd = self.aws_factory.get_client(
            "servicediscovery", region=region, credential=credential
        )
        ns_name = namespace_name or cluster.name

        # Check for existing namespace in database
        try:
            existing = ServiceConnectNamespace.objects.get(cluster=cluster)
            # Verify it still exists in AWS
            if self._namespace_exists(sd, existing.namespace_id):
                self.log_info(f"Found existing namespace: {existing.namespace_name}")
                return existing
            else:
                # Stale record -- remove it so we can recreate
                self.log_warning(
                    f"Namespace {existing.namespace_id} no longer exists in AWS, recreating"
                )
                existing.delete()
        except ServiceConnectNamespace.DoesNotExist:
            pass

        # Search for existing namespace in AWS by name
        existing_ns = self._find_namespace_by_name(sd, ns_name)
        if existing_ns:
            ns_model, _ = ServiceConnectNamespace.objects.update_or_create(
                cluster=cluster,
                defaults={
                    "namespace_id": existing_ns["id"],
                    "namespace_name": ns_name,
                    "namespace_arn": existing_ns.get("arn", ""),
                    "namespace_type": "HTTP",
                },
            )
            self.log_info(f"Linked existing Cloud Map namespace: {ns_name}")
            return ns_model

        # Create new HTTP namespace
        try:
            creator_request_id = str(uuid.uuid4())

            response = sd.create_http_namespace(
                Name=ns_name,
                CreatorRequestId=creator_request_id,
                Description=f"Service Connect namespace for {cluster.name}",
                Tags=[
                    {"Key": "remote-compose:cluster", "Value": cluster.name},
                    {"Key": "remote-compose:managed", "Value": "true"},
                    {"Key": "Name", "Value": ns_name},
                ],
            )

            operation_id = response.get("OperationId")
            self.log_info(f"Creating namespace {ns_name} (operation {operation_id})")

            # Wait for the operation to complete
            namespace_id = self._wait_for_operation(sd, operation_id)

            # Fetch namespace details
            ns_details = sd.get_namespace(Id=namespace_id)
            ns_data = ns_details.get("Namespace", {})
            namespace_arn = ns_data.get("Arn", "")

            ns_model, _ = ServiceConnectNamespace.objects.update_or_create(
                cluster=cluster,
                defaults={
                    "namespace_id": namespace_id,
                    "namespace_name": ns_name,
                    "namespace_arn": namespace_arn,
                    "namespace_type": "HTTP",
                },
            )

            self.notify_observers(
                "namespace_created",
                cluster_name=cluster.name,
                namespace_name=ns_name,
                namespace_id=namespace_id,
            )

            self.log_info(f"Created namespace {ns_name}: {namespace_id}")
            return ns_model

        except ClientError as e:
            raise NamespaceError(
                f"Failed to create namespace {ns_name}: {e}",
                namespace_name=ns_name,
            )

    def build_service_connect_config(
        self,
        namespace_name: str,
        service_name: str,
        port: int,
        port_name: Optional[str] = None,
    ) -> Dict:
        """
        Build a serviceConnectConfiguration dict for ECS CreateService.

        This configuration enables Service Connect for an ECS service,
        registering it in the namespace so other services can discover
        it by name.

        Args:
            namespace_name: Cloud Map namespace name.
            service_name: Name the service will be discoverable as.
            port: Port the service listens on.
            port_name: Optional port name (defaults to service_name).

        Returns:
            Dict suitable for the ``serviceConnectConfiguration`` parameter
            of ``ecs.create_service()``.
        """
        port_name = port_name or service_name

        return {
            "enabled": True,
            "namespace": namespace_name,
            "services": [
                {
                    "portName": port_name,
                    "discoveryName": service_name,
                    "clientAliases": [
                        {
                            "port": port,
                            "dnsName": service_name,
                        },
                    ],
                },
            ],
        }

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _namespace_exists(self, sd, namespace_id: str) -> bool:
        """Check whether a namespace still exists in AWS."""
        try:
            sd.get_namespace(Id=namespace_id)
            return True
        except ClientError:
            return False

    def _find_namespace_by_name(self, sd, name: str) -> Optional[Dict]:
        """Search for an HTTP namespace by name."""
        try:
            paginator = sd.get_paginator("list_namespaces")

            for page in paginator.paginate(
                Filters=[
                    {
                        "Name": "TYPE",
                        "Values": ["HTTP"],
                        "Condition": "EQ",
                    },
                ],
            ):
                for ns in page.get("Namespaces", []):
                    if ns.get("Name") == name:
                        return {
                            "id": ns["Id"],
                            "arn": ns.get("Arn", ""),
                            "name": ns["Name"],
                        }

            return None

        except ClientError as e:
            self.log_warning(f"Error searching for namespace {name}: {e}")
            return None

    def _wait_for_operation(
        self,
        sd,
        operation_id: str,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> str:
        """
        Wait for a Cloud Map operation to complete and return the resource ID.

        Args:
            sd: Service Discovery client.
            operation_id: Operation ID to poll.
            timeout: Maximum wait time in seconds.
            poll_interval: Time between polls in seconds.

        Returns:
            The ID of the created resource (namespace).

        Raises:
            NamespaceError: If the operation fails or times out.
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = sd.get_operation(OperationId=operation_id)
                operation = response.get("Operation", {})
                status = operation.get("Status")

                if status == "SUCCESS":
                    targets = operation.get("Targets", {})
                    namespace_id = targets.get("NAMESPACE")
                    if namespace_id:
                        return namespace_id
                    raise NamespaceError(
                        f"Operation succeeded but no namespace ID in targets: {targets}"
                    )

                elif status == "FAIL":
                    error_message = operation.get("ErrorMessage", "Unknown error")
                    raise NamespaceError(
                        f"Namespace creation operation failed: {error_message}"
                    )

                # SUBMITTED or PENDING -- keep waiting
                time.sleep(poll_interval)

            except ClientError as e:
                self.log_warning(f"Error polling operation {operation_id}: {e}")
                time.sleep(poll_interval)

        raise NamespaceError(
            f"Timeout waiting for namespace creation operation {operation_id}"
        )
