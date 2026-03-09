"""
Service for AWS ECS integration.

Provides functionality for managing ECS clusters, services, task definitions,
and deployments without requiring SSH access to EC2 instances.
"""

import json
from typing import Optional, List, Dict, Any
import time

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from ..models import ECSCluster, ECSTaskDefinition, ECSService as ECSServiceModel, SecureCredential
from ..conf import get_setting
from ..exceptions import (
    AWSError,
    AWSCredentialError,
    ECSError,
    ECSClusterError,
    ECSClusterNotFoundError,
    ECSServiceError,
    ECSServiceNotFoundError,
    ECSTaskDefinitionError,
    ECSTaskError,
    ECSDeploymentError,
    ECSDeploymentTimeoutError,
)
from .base import BaseService
from .credential_service import CredentialService
from .aws_client_factory import AWSClientFactory, get_aws_client_factory


class ECSService(BaseService):
    """
    Service for AWS ECS operations.

    Handles cluster management, task definition registration,
    service creation/updates, and deployment orchestration.
    """

    def __init__(
        self,
        credential_service: Optional[CredentialService] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.credential_service = credential_service or CredentialService()
        self.default_region = get_setting('AWS_DEFAULT_REGION', 'us-east-1')

    def _get_ecs_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """Get boto3 ECS client with optional credentials."""
        region = region or self.default_region

        try:
            if credential and credential.credential_type == SecureCredential.CredentialType.AWS_ACCESS_KEY:
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    'ecs',
                    region_name=region,
                    aws_access_key_id=aws_creds['access_key_id'],
                    aws_secret_access_key=aws_creds['secret_access_key'],
                )
            else:
                return boto3.client('ecs', region_name=region)

        except NoCredentialsError:
            raise AWSCredentialError(
                "No AWS credentials found. Configure credentials via environment variables, "
                "AWS credentials file, or create an AWS credential in remote_compose."
            )
        except Exception as e:
            raise AWSError(f"Failed to create ECS client: {e}")

    def _get_ec2_client(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ):
        """Get boto3 EC2 client for VPC/subnet discovery."""
        region = region or self.default_region

        try:
            if credential and credential.credential_type == SecureCredential.CredentialType.AWS_ACCESS_KEY:
                aws_creds = self.credential_service.get_aws_credentials(credential)
                return boto3.client(
                    'ec2',
                    region_name=region,
                    aws_access_key_id=aws_creds['access_key_id'],
                    aws_secret_access_key=aws_creds['secret_access_key'],
                )
            else:
                return boto3.client('ec2', region_name=region)
        except Exception as e:
            raise AWSError(f"Failed to create EC2 client: {e}")

    # -------------------------------------------------------------------------
    # Cluster Management
    # -------------------------------------------------------------------------

    def list_clusters(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> List[Dict[str, Any]]:
        """
        List ECS clusters in the AWS account.

        Returns:
            List of cluster dictionaries with ARN, name, and status
        """
        client = self._get_ecs_client(region, credential)

        try:
            cluster_arns = []
            paginator = client.get_paginator('list_clusters')
            for page in paginator.paginate():
                cluster_arns.extend(page.get('clusterArns', []))

            if not cluster_arns:
                return []

            response = client.describe_clusters(clusters=cluster_arns)
            clusters = []
            for cluster in response.get('clusters', []):
                clusters.append({
                    'arn': cluster['clusterArn'],
                    'name': cluster['clusterName'],
                    'status': cluster['status'],
                    'running_tasks': cluster.get('runningTasksCount', 0),
                    'pending_tasks': cluster.get('pendingTasksCount', 0),
                    'active_services': cluster.get('activeServicesCount', 0),
                    'capacity_providers': cluster.get('capacityProviders', []),
                })

            return clusters

        except ClientError as e:
            raise ECSClusterError(f"Failed to list clusters: {e}")

    def get_cluster(
        self,
        cluster_name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """Get details for a specific ECS cluster."""
        client = self._get_ecs_client(region, credential)

        try:
            response = client.describe_clusters(clusters=[cluster_name])
            clusters = response.get('clusters', [])

            if not clusters:
                failures = response.get('failures', [])
                if failures:
                    raise ECSClusterNotFoundError(
                        f"Cluster not found: {cluster_name}",
                        cluster_name=cluster_name,
                        region=region
                    )
                raise ECSClusterNotFoundError(f"Cluster not found: {cluster_name}")

            cluster = clusters[0]
            return {
                'arn': cluster['clusterArn'],
                'name': cluster['clusterName'],
                'status': cluster['status'],
                'running_tasks': cluster.get('runningTasksCount', 0),
                'pending_tasks': cluster.get('pendingTasksCount', 0),
                'active_services': cluster.get('activeServicesCount', 0),
                'capacity_providers': cluster.get('capacityProviders', []),
                'settings': cluster.get('settings', []),
            }

        except ClientError as e:
            raise ECSClusterError(f"Failed to get cluster: {e}", cluster_name=cluster_name)

    def create_cluster(
        self,
        name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
        capacity_providers: Optional[List[str]] = None,
        settings: Optional[Dict] = None,
    ) -> ECSCluster:
        """
        Create a new ECS cluster in AWS and track it locally.

        Args:
            name: Cluster name
            region: AWS region
            credential: AWS credential
            capacity_providers: List of capacity providers (e.g., ['FARGATE', 'FARGATE_SPOT'])
            settings: Additional cluster settings

        Returns:
            ECSCluster model instance
        """
        client = self._get_ecs_client(region, credential)
        region = region or self.default_region

        try:
            create_params = {'clusterName': name}

            if capacity_providers:
                create_params['capacityProviders'] = capacity_providers
                create_params['defaultCapacityProviderStrategy'] = [
                    {'capacityProvider': cp, 'weight': 1}
                    for cp in capacity_providers
                ]

            if settings:
                create_params['settings'] = [
                    {'name': k, 'value': v}
                    for k, v in settings.items()
                ]

            response = client.create_cluster(**create_params)
            aws_cluster = response['cluster']

            cluster = ECSCluster.objects.create(
                name=name,
                aws_cluster_arn=aws_cluster['clusterArn'],
                aws_cluster_name=aws_cluster['clusterName'],
                aws_region=region,
                status=ECSCluster.ClusterStatus.ACTIVE,
                is_managed=True,
                aws_credential=credential,
                metadata={
                    'capacity_providers': aws_cluster.get('capacityProviders', []),
                    'settings': aws_cluster.get('settings', []),
                }
            )

            self.log_info(f"Created ECS cluster: {name} in {region}")
            self.notify_observers('ecs_cluster_created', cluster=cluster)

            return cluster

        except ClientError as e:
            raise ECSClusterError(f"Failed to create cluster: {e}", cluster_name=name, region=region)

    def import_cluster(
        self,
        cluster_name_or_arn: str,
        local_name: Optional[str] = None,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> ECSCluster:
        """
        Import an existing AWS ECS cluster for management.

        Args:
            cluster_name_or_arn: Cluster name or ARN in AWS
            local_name: Local name for the cluster (defaults to AWS name)
            region: AWS region
            credential: AWS credential

        Returns:
            ECSCluster model instance
        """
        aws_cluster = self.get_cluster(cluster_name_or_arn, region, credential)

        existing = ECSCluster.objects.filter(aws_cluster_arn=aws_cluster['arn']).first()
        if existing:
            self.log_info(f"Cluster already imported: {existing.name}")
            return existing

        cluster = ECSCluster.objects.create(
            name=local_name or aws_cluster['name'],
            aws_cluster_arn=aws_cluster['arn'],
            aws_cluster_name=aws_cluster['name'],
            aws_region=region or self.default_region,
            status=ECSCluster.ClusterStatus.ACTIVE,
            is_managed=False,
            aws_credential=credential,
            metadata={
                'capacity_providers': aws_cluster.get('capacity_providers', []),
            }
        )

        self.log_info(f"Imported ECS cluster: {cluster.name}")
        return cluster

    def delete_cluster(
        self,
        cluster: ECSCluster,
        delete_in_aws: bool = False,
        force: bool = False,
    ) -> None:
        """
        Delete an ECS cluster.

        Args:
            cluster: ECSCluster model instance
            delete_in_aws: Also delete the cluster in AWS
            force: Force delete even if services are running
        """
        if delete_in_aws and cluster.is_managed:
            client = self._get_ecs_client(cluster.aws_region, cluster.aws_credential)

            try:
                if not force:
                    aws_cluster = self.get_cluster(
                        cluster.aws_cluster_name,
                        cluster.aws_region,
                        cluster.aws_credential
                    )
                    if aws_cluster['active_services'] > 0:
                        raise ECSClusterError(
                            f"Cluster has {aws_cluster['active_services']} active services. "
                            "Use force=True or delete services first."
                        )

                client.delete_cluster(cluster=cluster.aws_cluster_name)
                self.log_info(f"Deleted ECS cluster in AWS: {cluster.aws_cluster_name}")

            except ClientError as e:
                raise ECSClusterError(f"Failed to delete cluster in AWS: {e}")

        cluster.delete()
        self.log_info(f"Removed cluster from tracking: {cluster.name}")

    # -------------------------------------------------------------------------
    # Task Definition Management
    # -------------------------------------------------------------------------

    def register_task_definition(
        self,
        task_definition: ECSTaskDefinition,
    ) -> ECSTaskDefinition:
        """
        Register a task definition in AWS ECS.

        Args:
            task_definition: ECSTaskDefinition model instance

        Returns:
            Updated ECSTaskDefinition with AWS ARN
        """
        client = self._get_ecs_client(
            task_definition.cluster.aws_region,
            task_definition.cluster.aws_credential
        )

        try:
            task_def_params = task_definition.to_aws_format()
            response = client.register_task_definition(**task_def_params)

            aws_task_def = response['taskDefinition']
            new_revision = aws_task_def['revision']
            # Check if a stale record exists with this (cluster, name, revision)
            # from a previous failed deploy, and reclaim its pk.
            stale = ECSTaskDefinition.objects.filter(
                cluster=task_definition.cluster,
                name=task_definition.name,
                revision=new_revision,
            ).exclude(pk=task_definition.pk).first()
            if stale:
                stale.delete()
            task_definition.aws_task_definition_arn = aws_task_def['taskDefinitionArn']
            task_definition.revision = new_revision
            task_definition.status = ECSTaskDefinition.Status.REGISTERED
            task_definition.save()

            self.log_info(f"Registered task definition: {task_definition.full_arn}")
            self.notify_observers('task_definition_registered', task_definition=task_definition)

            return task_definition

        except ClientError as e:
            raise ECSTaskDefinitionError(
                f"Failed to register task definition: {e}",
                task_definition=task_definition.name
            )

    def list_task_definitions(
        self,
        family_prefix: Optional[str] = None,
        status: str = 'ACTIVE',
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> List[str]:
        """List task definition ARNs in AWS."""
        client = self._get_ecs_client(region, credential)

        try:
            params = {'status': status}
            if family_prefix:
                params['familyPrefix'] = family_prefix

            arns = []
            paginator = client.get_paginator('list_task_definitions')
            for page in paginator.paginate(**params):
                arns.extend(page.get('taskDefinitionArns', []))

            return arns

        except ClientError as e:
            raise ECSTaskDefinitionError(f"Failed to list task definitions: {e}")

    def describe_task_definition(
        self,
        task_definition: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """Get details of a task definition from AWS."""
        client = self._get_ecs_client(region, credential)

        try:
            response = client.describe_task_definition(taskDefinition=task_definition)
            return response['taskDefinition']
        except ClientError as e:
            raise ECSTaskDefinitionError(
                f"Failed to describe task definition: {e}",
                task_definition=task_definition
            )

    def deregister_task_definition(
        self,
        task_definition: ECSTaskDefinition,
    ) -> None:
        """Deregister a task definition in AWS."""
        if not task_definition.aws_task_definition_arn:
            self.log_warning(f"Task definition {task_definition.name} not registered in AWS")
            return

        client = self._get_ecs_client(
            task_definition.cluster.aws_region,
            task_definition.cluster.aws_credential
        )

        try:
            client.deregister_task_definition(
                taskDefinition=task_definition.aws_task_definition_arn
            )
            task_definition.status = ECSTaskDefinition.Status.DEREGISTERED
            task_definition.save()

            self.log_info(f"Deregistered task definition: {task_definition.full_arn}")

        except ClientError as e:
            raise ECSTaskDefinitionError(
                f"Failed to deregister task definition: {e}",
                task_definition=task_definition.name
            )

    # -------------------------------------------------------------------------
    # Service Management
    # -------------------------------------------------------------------------

    def create_service(
        self,
        ecs_service: ECSServiceModel,
    ) -> ECSServiceModel:
        """
        Create an ECS service in AWS.

        Args:
            ecs_service: ECSService model instance

        Returns:
            Updated ECSService with AWS ARN
        """
        client = self._get_ecs_client(
            ecs_service.cluster.aws_region,
            ecs_service.cluster.aws_credential
        )

        try:
            service_params = ecs_service.to_aws_create_format()
            response = client.create_service(**service_params)

            aws_service = response['service']
            ecs_service.aws_service_arn = aws_service['serviceArn']
            ecs_service.status = ECSServiceModel.ServiceStatus.CREATING
            ecs_service.save()

            self.log_info(f"Created ECS service: {ecs_service.name}")
            self.notify_observers('ecs_service_created', service=ecs_service)

            return ecs_service

        except ClientError as e:
            raise ECSServiceError(
                f"Failed to create service: {e}",
                service_name=ecs_service.name,
                cluster_name=ecs_service.cluster.name
            )

    def update_service(
        self,
        ecs_service: ECSServiceModel,
        task_definition: Optional[ECSTaskDefinition] = None,
        desired_count: Optional[int] = None,
        force_new_deployment: bool = False,
    ) -> ECSServiceModel:
        """
        Update an ECS service.

        Args:
            ecs_service: ECSService model instance
            task_definition: New task definition (optional)
            desired_count: New desired count (optional)
            force_new_deployment: Force new deployment even without changes

        Returns:
            Updated ECSService
        """
        client = self._get_ecs_client(
            ecs_service.cluster.aws_region,
            ecs_service.cluster.aws_credential
        )

        try:
            update_params = {
                'cluster': ecs_service.cluster.aws_cluster_arn or ecs_service.cluster.aws_cluster_name,
                'service': ecs_service.name,
            }

            if task_definition:
                update_params['taskDefinition'] = (
                    task_definition.aws_task_definition_arn or task_definition.full_arn
                )
                ecs_service.task_definition = task_definition

            if desired_count is not None:
                update_params['desiredCount'] = desired_count
                ecs_service.desired_count = desired_count

            if force_new_deployment:
                update_params['forceNewDeployment'] = True

            response = client.update_service(**update_params)

            aws_service = response['service']
            ecs_service.update_from_aws(aws_service)
            ecs_service.status = ECSServiceModel.ServiceStatus.UPDATING

            self.log_info(f"Updated ECS service: {ecs_service.name}")
            self.notify_observers('ecs_service_updated', service=ecs_service)

            return ecs_service

        except ClientError as e:
            raise ECSServiceError(
                f"Failed to update service: {e}",
                service_name=ecs_service.name,
                cluster_name=ecs_service.cluster.name
            )

    def describe_service(
        self,
        cluster_name: str,
        service_name: str,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """Get details of an ECS service from AWS."""
        client = self._get_ecs_client(region, credential)

        try:
            response = client.describe_services(
                cluster=cluster_name,
                services=[service_name]
            )

            services = response.get('services', [])
            if not services:
                failures = response.get('failures', [])
                if failures:
                    raise ECSServiceNotFoundError(
                        f"Service not found: {service_name}",
                        service_name=service_name,
                        cluster_name=cluster_name
                    )
                raise ECSServiceNotFoundError(f"Service not found: {service_name}")

            return services[0]

        except ClientError as e:
            raise ECSServiceError(
                f"Failed to describe service: {e}",
                service_name=service_name,
                cluster_name=cluster_name
            )

    def delete_service(
        self,
        ecs_service: ECSServiceModel,
        force: bool = False,
    ) -> None:
        """
        Delete an ECS service.

        Args:
            ecs_service: ECSService model instance
            force: Force delete without draining
        """
        client = self._get_ecs_client(
            ecs_service.cluster.aws_region,
            ecs_service.cluster.aws_credential
        )

        try:
            if not force:
                client.update_service(
                    cluster=ecs_service.cluster.aws_cluster_name,
                    service=ecs_service.name,
                    desiredCount=0,
                )
                ecs_service.status = ECSServiceModel.ServiceStatus.DRAINING
                ecs_service.save()
                self.log_info(f"Draining service: {ecs_service.name}")

            client.delete_service(
                cluster=ecs_service.cluster.aws_cluster_name,
                service=ecs_service.name,
                force=force,
            )

            ecs_service.status = ECSServiceModel.ServiceStatus.INACTIVE
            ecs_service.save()

            self.log_info(f"Deleted ECS service: {ecs_service.name}")
            self.notify_observers('ecs_service_deleted', service=ecs_service)

        except ClientError as e:
            raise ECSServiceError(
                f"Failed to delete service: {e}",
                service_name=ecs_service.name,
                cluster_name=ecs_service.cluster.name
            )

    def wait_for_service_stable(
        self,
        ecs_service: ECSServiceModel,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> ECSServiceModel:
        """
        Wait for an ECS service to reach a stable state.

        Args:
            ecs_service: ECSService model instance
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            Updated ECSService

        Raises:
            ECSDeploymentTimeoutError: If timeout is reached
        """
        client = self._get_ecs_client(
            ecs_service.cluster.aws_region,
            ecs_service.cluster.aws_credential
        )

        start_time = time.time()
        last_status = None

        while time.time() - start_time < timeout:
            try:
                aws_service = self.describe_service(
                    cluster_name=ecs_service.cluster.aws_cluster_name,
                    service_name=ecs_service.name,
                    region=ecs_service.cluster.aws_region,
                    credential=ecs_service.cluster.aws_credential,
                )

                ecs_service.update_from_aws(aws_service)

                deployments = aws_service.get('deployments', [])
                primary = next((d for d in deployments if d.get('status') == 'PRIMARY'), None)

                if primary:
                    rollout_state = primary.get('rolloutState')
                    if rollout_state != last_status:
                        self.log_info(
                            f"Service {ecs_service.name}: {rollout_state} "
                            f"({ecs_service.running_count}/{ecs_service.desired_count} running)"
                        )
                        last_status = rollout_state

                    if rollout_state == 'COMPLETED':
                        ecs_service.status = ECSServiceModel.ServiceStatus.ACTIVE
                        ecs_service.save()
                        self.log_info(f"Service {ecs_service.name} is stable")
                        return ecs_service
                    elif rollout_state == 'FAILED':
                        reason = primary.get('rolloutStateReason', 'Unknown reason')
                        raise ECSDeploymentError(
                            f"Deployment failed: {reason}",
                            details={'rollout_reason': reason}
                        )

                time.sleep(poll_interval)

            except ECSServiceError:
                raise
            except Exception as e:
                self.log_warning(f"Error polling service status: {e}")
                time.sleep(poll_interval)

        raise ECSDeploymentTimeoutError(
            f"Timeout waiting for service {ecs_service.name} to stabilize",
            details={
                'timeout': timeout,
                'running_count': ecs_service.running_count,
                'desired_count': ecs_service.desired_count,
            }
        )

    # -------------------------------------------------------------------------
    # Task Management
    # -------------------------------------------------------------------------

    def run_task(
        self,
        cluster: ECSCluster,
        task_definition: ECSTaskDefinition,
        count: int = 1,
        launch_type: Optional[str] = None,
        overrides: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run a standalone task (not as part of a service).

        Args:
            cluster: ECSCluster instance
            task_definition: ECSTaskDefinition to run
            count: Number of tasks to run
            launch_type: Override cluster's default launch type
            overrides: Container overrides

        Returns:
            List of task dictionaries
        """
        client = self._get_ecs_client(cluster.aws_region, cluster.aws_credential)

        try:
            run_params = {
                'cluster': cluster.aws_cluster_arn or cluster.aws_cluster_name,
                'taskDefinition': task_definition.aws_task_definition_arn or task_definition.full_arn,
                'count': count,
                'launchType': launch_type or cluster.launch_type.upper(),
            }

            if cluster.launch_type == ECSCluster.LaunchType.FARGATE:
                run_params['networkConfiguration'] = {
                    'awsvpcConfiguration': {
                        'subnets': cluster.subnet_ids,
                        'securityGroups': cluster.security_group_ids,
                        'assignPublicIp': 'ENABLED',
                    }
                }

            if overrides:
                run_params['overrides'] = overrides

            response = client.run_task(**run_params)

            failures = response.get('failures', [])
            if failures:
                reasons = [f['reason'] for f in failures]
                raise ECSTaskError(f"Failed to run tasks: {', '.join(reasons)}")

            tasks = response.get('tasks', [])
            self.log_info(f"Started {len(tasks)} task(s) in cluster {cluster.name}")

            return [
                {
                    'task_arn': t['taskArn'],
                    'task_definition_arn': t['taskDefinitionArn'],
                    'last_status': t['lastStatus'],
                    'desired_status': t['desiredStatus'],
                }
                for t in tasks
            ]

        except ClientError as e:
            raise ECSTaskError(f"Failed to run task: {e}")

    def stop_task(
        self,
        cluster: ECSCluster,
        task_arn: str,
        reason: str = 'Stopped by remote-compose',
    ) -> None:
        """Stop a running task."""
        client = self._get_ecs_client(cluster.aws_region, cluster.aws_credential)

        try:
            client.stop_task(
                cluster=cluster.aws_cluster_arn or cluster.aws_cluster_name,
                task=task_arn,
                reason=reason,
            )
            self.log_info(f"Stopped task: {task_arn}")

        except ClientError as e:
            raise ECSTaskError(f"Failed to stop task: {e}", task_arn=task_arn)

    def list_tasks(
        self,
        cluster: ECSCluster,
        service_name: Optional[str] = None,
        status: str = 'RUNNING',
    ) -> List[str]:
        """List task ARNs in a cluster."""
        client = self._get_ecs_client(cluster.aws_region, cluster.aws_credential)

        try:
            params = {
                'cluster': cluster.aws_cluster_arn or cluster.aws_cluster_name,
                'desiredStatus': status,
            }
            if service_name:
                params['serviceName'] = service_name

            task_arns = []
            paginator = client.get_paginator('list_tasks')
            for page in paginator.paginate(**params):
                task_arns.extend(page.get('taskArns', []))

            return task_arns

        except ClientError as e:
            raise ECSTaskError(f"Failed to list tasks: {e}")

    def describe_tasks(
        self,
        cluster: ECSCluster,
        task_arns: List[str],
    ) -> List[Dict[str, Any]]:
        """Get details of specific tasks."""
        if not task_arns:
            return []

        client = self._get_ecs_client(cluster.aws_region, cluster.aws_credential)

        try:
            response = client.describe_tasks(
                cluster=cluster.aws_cluster_arn or cluster.aws_cluster_name,
                tasks=task_arns,
            )

            return response.get('tasks', [])

        except ClientError as e:
            raise ECSTaskError(f"Failed to describe tasks: {e}")

    # -------------------------------------------------------------------------
    # Network Discovery
    # -------------------------------------------------------------------------

    def discover_default_vpc(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> Dict[str, Any]:
        """
        Discover the default VPC and its subnets.

        Returns:
            Dict with vpc_id, subnet_ids, and security_group_id
        """
        ec2 = self._get_ec2_client(region, credential)

        try:
            vpcs = ec2.describe_vpcs(Filters=[{'Name': 'is-default', 'Values': ['true']}])

            if not vpcs.get('Vpcs'):
                raise ECSError("No default VPC found. Please specify VPC and subnets.")

            vpc_id = vpcs['Vpcs'][0]['VpcId']

            subnets = ec2.describe_subnets(
                Filters=[
                    {'Name': 'vpc-id', 'Values': [vpc_id]},
                    {'Name': 'default-for-az', 'Values': ['true']},
                ]
            )
            subnet_ids = [s['SubnetId'] for s in subnets.get('Subnets', [])]

            sgs = ec2.describe_security_groups(
                Filters=[
                    {'Name': 'vpc-id', 'Values': [vpc_id]},
                    {'Name': 'group-name', 'Values': ['default']},
                ]
            )
            sg_id = sgs['SecurityGroups'][0]['GroupId'] if sgs.get('SecurityGroups') else None

            return {
                'vpc_id': vpc_id,
                'subnet_ids': subnet_ids,
                'security_group_id': sg_id,
            }

        except ClientError as e:
            raise ECSError(f"Failed to discover VPC: {e}")

    def sync_cluster_networking(
        self,
        cluster: ECSCluster,
    ) -> ECSCluster:
        """
        Sync cluster networking from AWS if not set.

        Discovers default VPC settings if cluster has no networking configured.
        """
        if cluster.subnet_ids and cluster.security_group_ids:
            return cluster

        network = self.discover_default_vpc(cluster.aws_region, cluster.aws_credential)

        cluster.vpc_id = network['vpc_id']
        cluster.subnet_ids = network['subnet_ids']
        if network.get('security_group_id'):
            cluster.security_group_ids = [network['security_group_id']]

        cluster.save()
        self.log_info(f"Updated cluster {cluster.name} networking from VPC {network['vpc_id']}")

        return cluster

    def get_or_create_task_execution_role(
        self,
        region: Optional[str] = None,
        credential: Optional[SecureCredential] = None,
    ) -> str:
        """
        Get or create the ECS task execution role.

        Returns the ARN of the ecsTaskExecutionRole.
        """
        region = region or self.default_region

        try:
            # Use AWS client factory for consistent credential handling
            aws_factory = get_aws_client_factory()
            iam = aws_factory.get_client('iam', region=region, credential=credential)

            # Try to get existing role
            try:
                response = iam.get_role(RoleName='ecsTaskExecutionRole')
                role_arn = response['Role']['Arn']

                # Ensure the CloudWatch Logs policy is attached
                self._ensure_logs_policy(iam)

                return role_arn
            except ClientError as e:
                if e.response['Error']['Code'] != 'NoSuchEntity':
                    raise

            # Role doesn't exist, create it
            self.log_info("Creating ecsTaskExecutionRole...")

            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole"
                    }
                ]
            }

            response = iam.create_role(
                RoleName='ecsTaskExecutionRole',
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description='Role for ECS task execution created by remote-compose',
            )

            role_arn = response['Role']['Arn']

            # Attach the managed policy
            iam.attach_role_policy(
                RoleName='ecsTaskExecutionRole',
                PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'
            )

            # Add inline policy for CloudWatch Logs CreateLogGroup
            # The managed policy doesn't include this permission
            self._ensure_logs_policy(iam)

            self.log_info(f"Created ecsTaskExecutionRole: {role_arn}")
            return role_arn

        except ClientError as e:
            raise ECSError(f"Failed to get/create task execution role: {e}")

    def _ensure_logs_policy(self, iam) -> None:
        """Ensure the CloudWatch Logs CreateLogGroup policy is attached to the execution role."""
        policy_name = 'CloudWatchLogsCreateGroup'

        # Check if policy already exists
        try:
            iam.get_role_policy(RoleName='ecsTaskExecutionRole', PolicyName=policy_name)
            return  # Policy already exists
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchEntity':
                raise

        # Add the policy
        logs_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogGroup"
                    ],
                    "Resource": "*"
                }
            ]
        }

        iam.put_role_policy(
            RoleName='ecsTaskExecutionRole',
            PolicyName=policy_name,
            PolicyDocument=json.dumps(logs_policy)
        )
        self.log_info("Added CloudWatchLogsCreateGroup policy to ecsTaskExecutionRole")

    def ensure_cluster_has_execution_role(
        self,
        cluster: ECSCluster,
    ) -> ECSCluster:
        """
        Ensure the cluster has a task execution role configured.
        """
        if cluster.task_execution_role_arn:
            return cluster

        role_arn = self.get_or_create_task_execution_role(
            cluster.aws_region,
            cluster.aws_credential,
        )

        cluster.task_execution_role_arn = role_arn
        cluster.save()
        self.log_info(f"Set cluster {cluster.name} execution role: {role_arn}")

        return cluster
