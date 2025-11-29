"""
Service for AWS VPC provisioning for ECS deployments.

Provides functionality for creating and managing VPC infrastructure
including subnets, internet gateways, NAT gateways, and route tables.
"""

import time
from typing import Optional, Dict, Any, List

from botocore.exceptions import ClientError

from ..models import VPCInfrastructure
from ..exceptions import VPCProvisioningError
from .base import BaseService
from .aws_client_factory import AWSClientFactory, get_aws_client_factory


class VPCService(BaseService):
    """
    Service for provisioning VPC infrastructure for ECS clusters.

    Creates a complete VPC setup with public and private subnets across
    two availability zones, internet gateway, NAT gateway, and route tables.
    Uses get-or-create semantics via AWS resource tags.
    """

    # Standard CIDR layout for subnets within the VPC
    PUBLIC_SUBNET_1_CIDR = '10.0.1.0/24'
    PUBLIC_SUBNET_2_CIDR = '10.0.2.0/24'
    PRIVATE_SUBNET_1_CIDR = '10.0.10.0/24'
    PRIVATE_SUBNET_2_CIDR = '10.0.11.0/24'

    def __init__(self, aws_factory: Optional[AWSClientFactory] = None, **kwargs):
        super().__init__(**kwargs)
        self.aws_factory = aws_factory or get_aws_client_factory()

    def _build_tags(self, cluster_name: str, resource_type: str, extra: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
        """Build standard tags for a managed resource."""
        tags = [
            {'Key': 'remote-compose:cluster', 'Value': cluster_name},
            {'Key': 'remote-compose:managed', 'Value': 'true'},
            {'Key': 'Name', 'Value': f'{cluster_name}-{resource_type}'},
        ]
        if extra:
            tags.extend({'Key': k, 'Value': v} for k, v in extra.items())
        return tags

    def _tag_spec(self, resource_type_aws: str, cluster_name: str, resource_label: str, extra: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Build TagSpecifications for a create call."""
        return [{
            'ResourceType': resource_type_aws,
            'Tags': self._build_tags(cluster_name, resource_label, extra),
        }]

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def provision_vpc(
        self,
        cluster,
        vpc_cidr: str = '10.0.0.0/16',
        region: Optional[str] = None,
        credential=None,
    ) -> VPCInfrastructure:
        """
        Provision a complete VPC infrastructure for an ECS cluster.

        Creates VPC, 2 public subnets, 2 private subnets, internet gateway,
        NAT gateway, and route tables. Uses get-or-create pattern via AWS tags.

        Args:
            cluster: ECSCluster model instance.
            vpc_cidr: CIDR block for the VPC (default 10.0.0.0/16).
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            VPCInfrastructure model instance.

        Raises:
            VPCProvisioningError: If any provisioning step fails.
        """
        ec2 = self.aws_factory.get_client('ec2', region=region, credential=credential)
        cluster_name = cluster.name

        # Check for existing managed VPC
        existing = self._find_existing_vpc(cluster_name, region, credential)
        if existing:
            self.log_info(f"Found existing VPC for cluster {cluster_name}: {existing}")
            # Return or update the model
            vpc_infra, _ = VPCInfrastructure.objects.get_or_create(
                cluster=cluster,
                defaults={'vpc_id': existing, 'vpc_cidr': vpc_cidr, 'is_managed': True},
            )
            if vpc_infra.vpc_id != existing:
                vpc_infra.vpc_id = existing
                vpc_infra.save(update_fields=['vpc_id'])
            return vpc_infra

        try:
            # 1. Create VPC
            vpc_response = ec2.create_vpc(
                CidrBlock=vpc_cidr,
                TagSpecifications=self._tag_spec('vpc', cluster_name, 'vpc'),
            )
            vpc_id = vpc_response['Vpc']['VpcId']
            self.log_info(f"Created VPC {vpc_id} for cluster {cluster_name}")

            # Enable DNS support and hostnames
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={'Value': True})
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={'Value': True})

            # 2. Get availability zones
            azs = self._get_availability_zones(region, credential)

            # 3. Create subnets
            public_subnet_1 = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=self.PUBLIC_SUBNET_1_CIDR,
                AvailabilityZone=azs[0],
                TagSpecifications=self._tag_spec('subnet', cluster_name, 'public-subnet-1'),
            )['Subnet']['SubnetId']

            public_subnet_2 = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=self.PUBLIC_SUBNET_2_CIDR,
                AvailabilityZone=azs[1],
                TagSpecifications=self._tag_spec('subnet', cluster_name, 'public-subnet-2'),
            )['Subnet']['SubnetId']

            private_subnet_1 = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=self.PRIVATE_SUBNET_1_CIDR,
                AvailabilityZone=azs[0],
                TagSpecifications=self._tag_spec('subnet', cluster_name, 'private-subnet-1'),
            )['Subnet']['SubnetId']

            private_subnet_2 = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=self.PRIVATE_SUBNET_2_CIDR,
                AvailabilityZone=azs[1],
                TagSpecifications=self._tag_spec('subnet', cluster_name, 'private-subnet-2'),
            )['Subnet']['SubnetId']

            # Enable auto-assign public IP on public subnets
            ec2.modify_subnet_attribute(SubnetId=public_subnet_1, MapPublicIpOnLaunch={'Value': True})
            ec2.modify_subnet_attribute(SubnetId=public_subnet_2, MapPublicIpOnLaunch={'Value': True})

            self.log_info(f"Created 4 subnets in {azs[0]} and {azs[1]}")

            # 4. Create Internet Gateway
            igw_response = ec2.create_internet_gateway(
                TagSpecifications=self._tag_spec('internet-gateway', cluster_name, 'igw'),
            )
            igw_id = igw_response['InternetGateway']['InternetGatewayId']
            ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            self.log_info(f"Created and attached IGW {igw_id}")

            # 5. Allocate Elastic IP and create NAT Gateway in first public subnet
            eip_response = ec2.allocate_address(
                Domain='vpc',
                TagSpecifications=self._tag_spec('elastic-ip', cluster_name, 'nat-eip'),
            )
            eip_allocation_id = eip_response['AllocationId']

            nat_response = ec2.create_nat_gateway(
                SubnetId=public_subnet_1,
                AllocationId=eip_allocation_id,
                TagSpecifications=self._tag_spec('natgateway', cluster_name, 'nat-gw'),
            )
            nat_gw_id = nat_response['NatGateway']['NatGatewayId']
            self.log_info(f"Created NAT Gateway {nat_gw_id} (waiting for availability)")

            # Wait for NAT Gateway to become available
            self._wait_for_nat_gateway(ec2, nat_gw_id)

            # 6. Create Route Tables
            # Public route table
            public_rt_response = ec2.create_route_table(
                VpcId=vpc_id,
                TagSpecifications=self._tag_spec('route-table', cluster_name, 'public-rt'),
            )
            public_rt_id = public_rt_response['RouteTable']['RouteTableId']

            ec2.create_route(
                RouteTableId=public_rt_id,
                DestinationCidrBlock='0.0.0.0/0',
                GatewayId=igw_id,
            )
            ec2.associate_route_table(RouteTableId=public_rt_id, SubnetId=public_subnet_1)
            ec2.associate_route_table(RouteTableId=public_rt_id, SubnetId=public_subnet_2)

            # Private route table
            private_rt_response = ec2.create_route_table(
                VpcId=vpc_id,
                TagSpecifications=self._tag_spec('route-table', cluster_name, 'private-rt'),
            )
            private_rt_id = private_rt_response['RouteTable']['RouteTableId']

            ec2.create_route(
                RouteTableId=private_rt_id,
                DestinationCidrBlock='0.0.0.0/0',
                NatGatewayId=nat_gw_id,
            )
            ec2.associate_route_table(RouteTableId=private_rt_id, SubnetId=private_subnet_1)
            ec2.associate_route_table(RouteTableId=private_rt_id, SubnetId=private_subnet_2)

            self.log_info(f"Created route tables and routes for VPC {vpc_id}")

            # 7. Persist to database
            vpc_infra, _ = VPCInfrastructure.objects.update_or_create(
                cluster=cluster,
                defaults={
                    'vpc_id': vpc_id,
                    'vpc_cidr': vpc_cidr,
                    'public_subnet_ids': [public_subnet_1, public_subnet_2],
                    'private_subnet_ids': [private_subnet_1, private_subnet_2],
                    'internet_gateway_id': igw_id,
                    'nat_gateway_id': nat_gw_id,
                    'elastic_ip_allocation_id': eip_allocation_id,
                    'public_route_table_id': public_rt_id,
                    'private_route_table_id': private_rt_id,
                    'is_managed': True,
                },
            )

            self.notify_observers(
                'vpc_provisioned',
                cluster_name=cluster_name,
                vpc_id=vpc_id,
            )

            return vpc_infra

        except ClientError as e:
            raise VPCProvisioningError(
                f"Failed to provision VPC for cluster {cluster_name}: {e}",
                vpc_id=locals().get('vpc_id'),
            )

    def _find_existing_vpc(
        self,
        cluster_name: str,
        region: Optional[str] = None,
        credential=None,
    ) -> Optional[str]:
        """
        Check for a VPC with matching remote-compose cluster tag.

        Args:
            cluster_name: Name of the ECS cluster.
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            VPC ID if found, None otherwise.
        """
        ec2 = self.aws_factory.get_client('ec2', region=region, credential=credential)

        try:
            response = ec2.describe_vpcs(
                Filters=[
                    {'Name': 'tag:remote-compose:cluster', 'Values': [cluster_name]},
                    {'Name': 'tag:remote-compose:managed', 'Values': ['true']},
                ]
            )
            vpcs = response.get('Vpcs', [])
            if vpcs:
                return vpcs[0]['VpcId']
            return None

        except ClientError as e:
            self.log_warning(f"Error searching for existing VPC: {e}")
            return None

    def _get_availability_zones(
        self,
        region: Optional[str] = None,
        credential=None,
    ) -> List[str]:
        """
        Get the first two available availability zones in the region.

        Args:
            region: AWS region override.
            credential: SecureCredential for AWS access.

        Returns:
            List of two AZ names.

        Raises:
            VPCProvisioningError: If fewer than 2 AZs are available.
        """
        ec2 = self.aws_factory.get_client('ec2', region=region, credential=credential)

        try:
            response = ec2.describe_availability_zones(
                Filters=[
                    {'Name': 'state', 'Values': ['available']},
                ]
            )
            azs = [az['ZoneName'] for az in response.get('AvailabilityZones', [])]

            if len(azs) < 2:
                raise VPCProvisioningError(
                    f"Need at least 2 availability zones, found {len(azs)} in region"
                )

            return azs[:2]

        except ClientError as e:
            raise VPCProvisioningError(f"Failed to get availability zones: {e}")

    def _wait_for_nat_gateway(
        self,
        ec2,
        nat_gateway_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> None:
        """Wait for a NAT gateway to reach the 'available' state."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = ec2.describe_nat_gateways(NatGatewayIds=[nat_gateway_id])
                gateways = response.get('NatGateways', [])

                if gateways:
                    state = gateways[0].get('State')
                    if state == 'available':
                        self.log_info(f"NAT Gateway {nat_gateway_id} is available")
                        return
                    elif state == 'failed':
                        failure_message = gateways[0].get('FailureMessage', 'Unknown failure')
                        raise VPCProvisioningError(
                            f"NAT Gateway {nat_gateway_id} failed: {failure_message}"
                        )

                time.sleep(poll_interval)

            except ClientError as e:
                self.log_warning(f"Error polling NAT Gateway status: {e}")
                time.sleep(poll_interval)

        raise VPCProvisioningError(
            f"Timeout waiting for NAT Gateway {nat_gateway_id} to become available"
        )

    def teardown_vpc(self, vpc_infrastructure: VPCInfrastructure) -> None:
        """
        Tear down all VPC resources in reverse order.

        Deletion order: NAT Gateway -> Elastic IP -> subnets ->
        route tables -> Internet Gateway -> VPC.

        Args:
            vpc_infrastructure: VPCInfrastructure model instance to tear down.

        Raises:
            VPCProvisioningError: If teardown fails.
        """
        ec2 = self.aws_factory.get_client('ec2')
        vpc_id = vpc_infrastructure.vpc_id

        self.log_info(f"Starting teardown of VPC {vpc_id}")

        try:
            # 1. Delete NAT Gateway
            if vpc_infrastructure.nat_gateway_id:
                try:
                    ec2.delete_nat_gateway(NatGatewayId=vpc_infrastructure.nat_gateway_id)
                    self.log_info(f"Deleted NAT Gateway {vpc_infrastructure.nat_gateway_id}")
                    # Wait for NAT gateway deletion
                    self._wait_for_nat_gateway_deletion(ec2, vpc_infrastructure.nat_gateway_id)
                except ClientError as e:
                    if 'NatGatewayNotFound' not in str(e):
                        self.log_warning(f"Error deleting NAT Gateway: {e}")

            # 2. Release Elastic IP
            if vpc_infrastructure.elastic_ip_allocation_id:
                try:
                    ec2.release_address(AllocationId=vpc_infrastructure.elastic_ip_allocation_id)
                    self.log_info(f"Released EIP {vpc_infrastructure.elastic_ip_allocation_id}")
                except ClientError as e:
                    if 'InvalidAllocationID.NotFound' not in str(e):
                        self.log_warning(f"Error releasing EIP: {e}")

            # 3. Detach and delete Internet Gateway
            if vpc_infrastructure.internet_gateway_id:
                try:
                    ec2.detach_internet_gateway(
                        InternetGatewayId=vpc_infrastructure.internet_gateway_id,
                        VpcId=vpc_id,
                    )
                    ec2.delete_internet_gateway(
                        InternetGatewayId=vpc_infrastructure.internet_gateway_id,
                    )
                    self.log_info(f"Deleted IGW {vpc_infrastructure.internet_gateway_id}")
                except ClientError as e:
                    if 'InvalidInternetGatewayID.NotFound' not in str(e):
                        self.log_warning(f"Error deleting IGW: {e}")

            # 4. Delete route table associations and route tables
            for rt_id in [vpc_infrastructure.public_route_table_id, vpc_infrastructure.private_route_table_id]:
                if rt_id:
                    self._delete_route_table(ec2, rt_id)

            # 5. Delete subnets
            all_subnets = (vpc_infrastructure.public_subnet_ids or []) + (vpc_infrastructure.private_subnet_ids or [])
            for subnet_id in all_subnets:
                try:
                    ec2.delete_subnet(SubnetId=subnet_id)
                    self.log_info(f"Deleted subnet {subnet_id}")
                except ClientError as e:
                    if 'InvalidSubnetID.NotFound' not in str(e):
                        self.log_warning(f"Error deleting subnet {subnet_id}: {e}")

            # 6. Delete VPC
            try:
                ec2.delete_vpc(VpcId=vpc_id)
                self.log_info(f"Deleted VPC {vpc_id}")
            except ClientError as e:
                if 'InvalidVpcID.NotFound' not in str(e):
                    raise VPCProvisioningError(
                        f"Failed to delete VPC {vpc_id}: {e}",
                        vpc_id=vpc_id,
                    )

            # 7. Remove database record
            vpc_infrastructure.delete()

            self.notify_observers('vpc_torn_down', vpc_id=vpc_id)
            self.log_info(f"VPC teardown complete for {vpc_id}")

        except VPCProvisioningError:
            raise
        except ClientError as e:
            raise VPCProvisioningError(
                f"Failed to teardown VPC {vpc_id}: {e}",
                vpc_id=vpc_id,
            )

    def _delete_route_table(self, ec2, route_table_id: str) -> None:
        """Delete a route table and its associations."""
        try:
            # Disassociate all non-main associations
            response = ec2.describe_route_tables(RouteTableIds=[route_table_id])
            for rt in response.get('RouteTables', []):
                for assoc in rt.get('Associations', []):
                    if not assoc.get('Main', False):
                        ec2.disassociate_route_table(
                            AssociationId=assoc['RouteTableAssociationId']
                        )

            ec2.delete_route_table(RouteTableId=route_table_id)
            self.log_info(f"Deleted route table {route_table_id}")

        except ClientError as e:
            if 'InvalidRouteTableID.NotFound' not in str(e):
                self.log_warning(f"Error deleting route table {route_table_id}: {e}")

    def _wait_for_nat_gateway_deletion(
        self,
        ec2,
        nat_gateway_id: str,
        timeout: int = 300,
        poll_interval: int = 10,
    ) -> None:
        """Wait for a NAT gateway to be fully deleted."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = ec2.describe_nat_gateways(NatGatewayIds=[nat_gateway_id])
                gateways = response.get('NatGateways', [])

                if not gateways or gateways[0].get('State') == 'deleted':
                    return

                time.sleep(poll_interval)

            except ClientError:
                return  # Gateway no longer exists

        self.log_warning(f"Timeout waiting for NAT Gateway {nat_gateway_id} deletion")
