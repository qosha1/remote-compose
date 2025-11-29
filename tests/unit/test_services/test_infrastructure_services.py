"""
Tests for infrastructure services: VPC, Security Groups, ALB, Secrets, Service Connect.
"""

import os
import pytest
from unittest.mock import MagicMock, patch, mock_open

from botocore.exceptions import ClientError

from remote_compose.models import (
    ECSCluster,
    VPCInfrastructure,
    SecurityGroupConfig,
    LoadBalancerConfig,
    TargetGroupConfig,
    SecretConfig,
    ServiceConnectNamespace,
)
from remote_compose.services import (
    VPCService,
    SecurityGroupService,
    ALBService,
    SecretsService,
    ServiceConnectService,
)
from remote_compose.services.aws_client_factory import AWSClientFactory
from remote_compose.exceptions import (
    VPCProvisioningError,
    SecurityGroupProvisioningError,
    ALBProvisioningError,
    TargetGroupError,
    SecretProvisioningError,
    NamespaceError,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# cluster fixture is provided by tests/conftest.py


@pytest.fixture
def mock_aws_factory():
    factory = MagicMock(spec=AWSClientFactory)
    return factory


@pytest.fixture
def mock_ec2_client():
    return MagicMock()


@pytest.fixture
def mock_elbv2_client():
    return MagicMock()


@pytest.fixture
def mock_sm_client():
    return MagicMock()


@pytest.fixture
def mock_sd_client():
    return MagicMock()


def _make_client_error(code, message, operation='TestOperation'):
    """Helper to build a botocore ClientError."""
    return ClientError(
        {'Error': {'Code': code, 'Message': message}},
        operation,
    )


# ===========================================================================
# TestVPCService
# ===========================================================================

class TestVPCService:
    """Tests for VPCService."""

    @pytest.fixture
    def vpc_service(self, mock_aws_factory):
        return VPCService(aws_factory=mock_aws_factory)

    # -- provision_vpc: full creation path ----------------------------------

    @pytest.mark.django_db
    def test_provision_vpc_creates_all_resources(
        self, vpc_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that provision_vpc creates VPC, subnets, IGW, NAT, and route tables."""
        mock_aws_factory.get_client.return_value = mock_ec2_client
        ec2 = mock_ec2_client

        # No existing VPC
        ec2.describe_vpcs.return_value = {'Vpcs': []}

        # VPC creation
        ec2.create_vpc.return_value = {'Vpc': {'VpcId': 'vpc-new123'}}

        # Availability zones
        ec2.describe_availability_zones.return_value = {
            'AvailabilityZones': [
                {'ZoneName': 'us-east-1a'},
                {'ZoneName': 'us-east-1b'},
            ]
        }

        # Subnets
        ec2.create_subnet.side_effect = [
            {'Subnet': {'SubnetId': 'subnet-pub1'}},
            {'Subnet': {'SubnetId': 'subnet-pub2'}},
            {'Subnet': {'SubnetId': 'subnet-priv1'}},
            {'Subnet': {'SubnetId': 'subnet-priv2'}},
        ]

        # Internet Gateway
        ec2.create_internet_gateway.return_value = {
            'InternetGateway': {'InternetGatewayId': 'igw-abc'}
        }

        # Elastic IP and NAT Gateway
        ec2.allocate_address.return_value = {'AllocationId': 'eipalloc-xyz'}
        ec2.create_nat_gateway.return_value = {
            'NatGateway': {'NatGatewayId': 'nat-gw-123'}
        }
        ec2.describe_nat_gateways.return_value = {
            'NatGateways': [{'State': 'available'}]
        }

        # Route tables
        ec2.create_route_table.side_effect = [
            {'RouteTable': {'RouteTableId': 'rtb-pub'}},
            {'RouteTable': {'RouteTableId': 'rtb-priv'}},
        ]

        result = vpc_service.provision_vpc(cluster)

        assert isinstance(result, VPCInfrastructure)
        assert result.vpc_id == 'vpc-new123'
        assert result.public_subnet_ids == ['subnet-pub1', 'subnet-pub2']
        assert result.private_subnet_ids == ['subnet-priv1', 'subnet-priv2']
        assert result.internet_gateway_id == 'igw-abc'
        assert result.nat_gateway_id == 'nat-gw-123'
        assert result.is_managed is True
        assert result.cluster == cluster

        # Verify DNS attributes were enabled
        assert ec2.modify_vpc_attribute.call_count == 2

        # Verify public IP auto-assign
        assert ec2.modify_subnet_attribute.call_count == 2

        # Verify route creation for public + private
        assert ec2.create_route.call_count == 2

    # -- provision_vpc: idempotent (existing VPC found) ---------------------

    @pytest.mark.django_db
    def test_provision_vpc_idempotent_existing_vpc(
        self, vpc_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that provision_vpc reuses an existing VPC found via tags."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        mock_ec2_client.describe_vpcs.return_value = {
            'Vpcs': [{'VpcId': 'vpc-existing'}]
        }

        result = vpc_service.provision_vpc(cluster)

        assert isinstance(result, VPCInfrastructure)
        assert result.vpc_id == 'vpc-existing'
        assert result.cluster == cluster
        # create_vpc should NOT have been called
        mock_ec2_client.create_vpc.assert_not_called()

    # -- provision_vpc: error handling --------------------------------------

    @pytest.mark.django_db
    def test_provision_vpc_raises_on_client_error(
        self, vpc_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that a ClientError during VPC creation raises VPCProvisioningError."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        # No existing VPC
        mock_ec2_client.describe_vpcs.return_value = {'Vpcs': []}

        # AZ lookup succeeds
        mock_ec2_client.describe_availability_zones.return_value = {
            'AvailabilityZones': [
                {'ZoneName': 'us-east-1a'},
                {'ZoneName': 'us-east-1b'},
            ]
        }

        mock_ec2_client.create_vpc.side_effect = _make_client_error(
            'VpcLimitExceeded', 'Too many VPCs', 'CreateVpc'
        )

        with pytest.raises(VPCProvisioningError) as exc_info:
            vpc_service.provision_vpc(cluster)

        assert 'test-cluster' in str(exc_info.value)

    # -- teardown_vpc -------------------------------------------------------

    @pytest.mark.django_db
    def test_teardown_vpc_removes_resources_in_reverse_order(
        self, vpc_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that teardown_vpc deletes resources in the correct reverse order."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        vpc_infra = VPCInfrastructure.objects.create(
            cluster=cluster,
            vpc_id='vpc-teardown',
            vpc_cidr='10.0.0.0/16',
            public_subnet_ids=['subnet-pub1', 'subnet-pub2'],
            private_subnet_ids=['subnet-priv1', 'subnet-priv2'],
            internet_gateway_id='igw-del',
            nat_gateway_id='nat-del',
            elastic_ip_allocation_id='eipalloc-del',
            public_route_table_id='rtb-pub-del',
            private_route_table_id='rtb-priv-del',
            is_managed=True,
        )

        # NAT gateway deletion waits
        mock_ec2_client.describe_nat_gateways.return_value = {
            'NatGateways': [{'State': 'deleted'}]
        }

        # Route table associations
        mock_ec2_client.describe_route_tables.return_value = {
            'RouteTables': [{
                'Associations': [
                    {'RouteTableAssociationId': 'assoc-1', 'Main': False},
                ]
            }]
        }

        vpc_service.teardown_vpc(vpc_infra)

        # NAT Gateway deleted
        mock_ec2_client.delete_nat_gateway.assert_called_once_with(
            NatGatewayId='nat-del'
        )

        # EIP released
        mock_ec2_client.release_address.assert_called_once_with(
            AllocationId='eipalloc-del'
        )

        # IGW detached and deleted
        mock_ec2_client.detach_internet_gateway.assert_called_once()
        mock_ec2_client.delete_internet_gateway.assert_called_once()

        # Subnets deleted (4 total)
        assert mock_ec2_client.delete_subnet.call_count == 4

        # VPC deleted
        mock_ec2_client.delete_vpc.assert_called_once_with(VpcId='vpc-teardown')

        # Database record removed
        assert not VPCInfrastructure.objects.filter(vpc_id='vpc-teardown').exists()

    @pytest.mark.django_db
    def test_teardown_vpc_raises_on_vpc_delete_error(
        self, vpc_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that teardown raises VPCProvisioningError when VPC delete fails."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        vpc_infra = VPCInfrastructure.objects.create(
            cluster=cluster,
            vpc_id='vpc-err',
            vpc_cidr='10.0.0.0/16',
            is_managed=True,
        )

        mock_ec2_client.delete_vpc.side_effect = _make_client_error(
            'DependencyViolation', 'VPC has dependencies', 'DeleteVpc'
        )

        with pytest.raises(VPCProvisioningError):
            vpc_service.teardown_vpc(vpc_infra)

    # -- _find_existing_vpc edge case: ClientError returns None -------

    def test_find_existing_vpc_returns_none_on_client_error(
        self, vpc_service, mock_aws_factory, mock_ec2_client,
    ):
        """Test that _find_existing_vpc returns None when describe_vpcs fails."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        mock_ec2_client.describe_vpcs.side_effect = _make_client_error(
            'UnauthorizedOperation', 'Not allowed', 'DescribeVpcs'
        )

        result = vpc_service._find_existing_vpc('test-cluster')
        assert result is None

    # -- _get_availability_zones: too few AZs error -------------------------

    def test_get_availability_zones_raises_on_too_few_azs(
        self, vpc_service, mock_aws_factory, mock_ec2_client,
    ):
        """Test error when fewer than 2 AZs are available."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        mock_ec2_client.describe_availability_zones.return_value = {
            'AvailabilityZones': [{'ZoneName': 'us-east-1a'}]
        }

        with pytest.raises(VPCProvisioningError, match='at least 2'):
            vpc_service._get_availability_zones()


# ===========================================================================
# TestSecurityGroupService
# ===========================================================================

class TestSecurityGroupService:
    """Tests for SecurityGroupService."""

    @pytest.fixture
    def sg_service(self, mock_aws_factory):
        return SecurityGroupService(aws_factory=mock_aws_factory)

    # -- provision_security_groups: creates 5 SGs ---------------------------

    @pytest.mark.django_db
    def test_provision_creates_five_security_groups(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that provision_security_groups creates all 5 purpose-specific SGs."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        # No existing SGs found
        mock_ec2_client.describe_security_groups.return_value = {
            'SecurityGroups': []
        }

        sg_counter = {'n': 0}

        def _create_sg(**kwargs):
            sg_counter['n'] += 1
            return {'GroupId': f'sg-{sg_counter["n"]:03d}'}

        mock_ec2_client.create_security_group.side_effect = _create_sg

        result = sg_service.provision_security_groups(
            cluster, vpc_id='vpc-test123'
        )

        assert len(result) == 5
        assert set(result.keys()) == {'alb', 'ecs_tasks', 'database', 'cache', 'efs'}
        assert mock_ec2_client.create_security_group.call_count == 5

        # Verify DB records created
        assert SecurityGroupConfig.objects.filter(cluster=cluster).count() == 5

    # -- SG rule configuration (correct ports/sources) ----------------------

    @pytest.mark.django_db
    def test_alb_sg_allows_http_and_https(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that ALB SG has inbound rules for ports 80 and 443."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        sg_ids = {'alb': 'sg-alb', 'ecs_tasks': 'sg-ecs', 'database': 'sg-db',
                  'cache': 'sg-cache', 'efs': 'sg-efs'}

        sg_service._configure_rules(
            sg_id='sg-alb', purpose='alb', sg_ids_map=sg_ids,
        )

        call_args = mock_ec2_client.authorize_security_group_ingress.call_args
        ip_permissions = call_args[1]['IpPermissions']

        ports = {rule['FromPort'] for rule in ip_permissions}
        assert 80 in ports
        assert 443 in ports

    @pytest.mark.django_db
    def test_database_sg_allows_postgres_from_ecs(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that database SG allows port 5432 from ECS tasks SG."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        sg_ids = {'alb': 'sg-alb', 'ecs_tasks': 'sg-ecs', 'database': 'sg-db',
                  'cache': 'sg-cache', 'efs': 'sg-efs'}

        # Create a DB record so _configure_rules can update it
        SecurityGroupConfig.objects.create(
            cluster=cluster, security_group_id='sg-db',
            purpose='database', vpc_id='vpc-test',
        )

        sg_service._configure_rules(
            sg_id='sg-db', purpose='database', sg_ids_map=sg_ids,
        )

        call_args = mock_ec2_client.authorize_security_group_ingress.call_args
        ip_permissions = call_args[1]['IpPermissions']

        assert ip_permissions[0]['FromPort'] == 5432
        assert ip_permissions[0]['ToPort'] == 5432
        assert ip_permissions[0]['UserIdGroupPairs'][0]['GroupId'] == 'sg-ecs'

    @pytest.mark.django_db
    def test_cache_sg_allows_redis_from_ecs(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that cache SG allows port 6379 from ECS tasks SG."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        sg_ids = {'alb': 'sg-alb', 'ecs_tasks': 'sg-ecs', 'database': 'sg-db',
                  'cache': 'sg-cache', 'efs': 'sg-efs'}

        SecurityGroupConfig.objects.create(
            cluster=cluster, security_group_id='sg-cache',
            purpose='cache', vpc_id='vpc-test',
        )

        sg_service._configure_rules(
            sg_id='sg-cache', purpose='cache', sg_ids_map=sg_ids,
        )

        call_args = mock_ec2_client.authorize_security_group_ingress.call_args
        ip_permissions = call_args[1]['IpPermissions']

        assert ip_permissions[0]['FromPort'] == 6379
        assert ip_permissions[0]['UserIdGroupPairs'][0]['GroupId'] == 'sg-ecs'

    @pytest.mark.django_db
    def test_efs_sg_allows_nfs_from_ecs(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that EFS SG allows port 2049 from ECS tasks SG."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        sg_ids = {'alb': 'sg-alb', 'ecs_tasks': 'sg-ecs', 'database': 'sg-db',
                  'cache': 'sg-cache', 'efs': 'sg-efs'}

        SecurityGroupConfig.objects.create(
            cluster=cluster, security_group_id='sg-efs',
            purpose='efs', vpc_id='vpc-test',
        )

        sg_service._configure_rules(
            sg_id='sg-efs', purpose='efs', sg_ids_map=sg_ids,
        )

        call_args = mock_ec2_client.authorize_security_group_ingress.call_args
        ip_permissions = call_args[1]['IpPermissions']

        assert ip_permissions[0]['FromPort'] == 2049
        assert ip_permissions[0]['UserIdGroupPairs'][0]['GroupId'] == 'sg-ecs'

    # -- idempotent get-or-create: existing SG found via tags ---------------

    @pytest.mark.django_db
    def test_create_or_get_sg_reuses_existing(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that _create_or_get_sg returns existing SG if found."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        mock_ec2_client.describe_security_groups.return_value = {
            'SecurityGroups': [{'GroupId': 'sg-existing'}]
        }

        sg_id = sg_service._create_or_get_sg(
            name='test-cluster-alb-sg',
            description='ALB SG',
            vpc_id='vpc-test',
            purpose='alb',
            cluster=cluster,
        )

        assert sg_id == 'sg-existing'
        mock_ec2_client.create_security_group.assert_not_called()

    # -- error handling: creation failure -----------------------------------

    @pytest.mark.django_db
    def test_create_sg_raises_on_error(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that SG creation failure raises SecurityGroupProvisioningError."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        mock_ec2_client.describe_security_groups.return_value = {
            'SecurityGroups': []
        }
        mock_ec2_client.create_security_group.side_effect = _make_client_error(
            'VpcLimitExceeded', 'Too many SGs', 'CreateSecurityGroup'
        )

        with pytest.raises(SecurityGroupProvisioningError):
            sg_service._create_or_get_sg(
                name='fail-sg', description='fail',
                vpc_id='vpc-test', purpose='alb', cluster=cluster,
            )

    # -- duplicate rule handling -------------------------------------------

    @pytest.mark.django_db
    def test_configure_rules_handles_duplicate_rules(
        self, sg_service, mock_aws_factory, mock_ec2_client, cluster,
    ):
        """Test that duplicate permission errors are silently ignored."""
        mock_aws_factory.get_client.return_value = mock_ec2_client

        SecurityGroupConfig.objects.create(
            cluster=cluster, security_group_id='sg-alb',
            purpose='alb', vpc_id='vpc-test',
        )

        mock_ec2_client.authorize_security_group_ingress.side_effect = _make_client_error(
            'InvalidPermission.Duplicate', 'Rule already exists',
            'AuthorizeSecurityGroupIngress',
        )

        sg_ids = {'alb': 'sg-alb', 'ecs_tasks': 'sg-ecs', 'database': 'sg-db',
                  'cache': 'sg-cache', 'efs': 'sg-efs'}

        # Should NOT raise
        sg_service._configure_rules(
            sg_id='sg-alb', purpose='alb', sg_ids_map=sg_ids,
        )


# ===========================================================================
# TestALBService
# ===========================================================================

class TestALBService:
    """Tests for ALBService."""

    @pytest.fixture
    def alb_service(self, mock_aws_factory):
        return ALBService(aws_factory=mock_aws_factory)

    @pytest.fixture
    def vpc_infrastructure(self, cluster):
        return VPCInfrastructure.objects.create(
            cluster=cluster,
            vpc_id='vpc-alb-test',
            vpc_cidr='10.0.0.0/16',
            public_subnet_ids=['subnet-pub1', 'subnet-pub2'],
            private_subnet_ids=['subnet-priv1', 'subnet-priv2'],
            is_managed=True,
        )

    # -- provision_alb: full creation path ----------------------------------

    @pytest.mark.django_db
    def test_provision_alb_creates_alb_in_public_subnets(
        self, alb_service, mock_aws_factory, mock_elbv2_client,
        cluster, vpc_infrastructure,
    ):
        """Test that provision_alb creates an internet-facing ALB."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client
        elbv2 = mock_elbv2_client

        # No existing ALB
        elbv2.describe_load_balancers.side_effect = [
            _make_client_error(
                'LoadBalancerNotFound', 'Not found', 'DescribeLoadBalancers'
            ),
            # Second call for _wait_for_alb_active
            {'LoadBalancers': [{'State': {'Code': 'active'}}]},
        ]

        elbv2.create_load_balancer.return_value = {
            'LoadBalancers': [{
                'LoadBalancerArn': 'arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/test/123',
                'DNSName': 'test-cluster-alb-123.us-east-1.elb.amazonaws.com',
                'CanonicalHostedZoneId': 'Z35SXDOTRQ7X7K',
            }]
        }

        elbv2.create_target_group.return_value = {
            'TargetGroups': [{
                'TargetGroupArn': 'arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/default/abc',
            }]
        }

        elbv2.create_listener.return_value = {
            'Listeners': [{
                'ListenerArn': 'arn:aws:elasticloadbalancing:us-east-1:123:listener/app/test/123/http',
            }]
        }

        result = alb_service.provision_alb(
            cluster, vpc_infrastructure, security_group_id='sg-alb',
        )

        assert isinstance(result, LoadBalancerConfig)
        assert result.alb_dns_name == 'test-cluster-alb-123.us-east-1.elb.amazonaws.com'
        assert result.security_group_id == 'sg-alb'

        # ALB created with public subnets
        create_call = elbv2.create_load_balancer.call_args
        assert create_call[1]['Subnets'] == ['subnet-pub1', 'subnet-pub2']
        assert create_call[1]['Scheme'] == 'internet-facing'

    # -- provision_alb: reuses existing ALB ---------------------------------

    @pytest.mark.django_db
    def test_provision_alb_reuses_existing_alb(
        self, alb_service, mock_aws_factory, mock_elbv2_client,
        cluster, vpc_infrastructure,
    ):
        """Test that provision_alb reuses an ALB found by name."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client
        elbv2 = mock_elbv2_client

        elbv2.describe_load_balancers.return_value = {
            'LoadBalancers': [{
                'LoadBalancerArn': 'arn:aws:elasticloadbalancing:existing',
                'DNSName': 'existing.elb.amazonaws.com',
                'CanonicalHostedZoneId': 'ZONE123',
            }]
        }

        elbv2.describe_listeners.return_value = {
            'Listeners': [
                {'Port': 80, 'ListenerArn': 'arn:listener:http'},
                {'Port': 443, 'ListenerArn': 'arn:listener:https'},
            ]
        }

        result = alb_service.provision_alb(
            cluster, vpc_infrastructure, security_group_id='sg-alb',
        )

        assert isinstance(result, LoadBalancerConfig)
        assert result.alb_dns_name == 'existing.elb.amazonaws.com'
        elbv2.create_load_balancer.assert_not_called()

    # -- create_target_group ------------------------------------------------

    @pytest.mark.django_db
    def test_create_target_group_creates_with_correct_settings(
        self, alb_service, mock_aws_factory, mock_elbv2_client, cluster,
    ):
        """Test create_target_group creates TG with correct port, protocol, health check."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client
        elbv2 = mock_elbv2_client

        # Not found
        elbv2.describe_target_groups.side_effect = _make_client_error(
            'TargetGroupNotFound', 'Not found', 'DescribeTargetGroups'
        )

        elbv2.create_target_group.return_value = {
            'TargetGroups': [{
                'TargetGroupArn': 'arn:tg:web',
            }]
        }

        result = alb_service.create_target_group(
            cluster=cluster,
            vpc_id='vpc-test',
            service_name='web',
            port=8080,
            health_check_path='/healthz',
        )

        assert isinstance(result, TargetGroupConfig)
        assert result.port == 8080
        assert result.health_check_path == '/healthz'
        assert result.target_group_arn == 'arn:tg:web'

        create_call = elbv2.create_target_group.call_args
        assert create_call[1]['Port'] == 8080
        assert create_call[1]['TargetType'] == 'ip'
        assert create_call[1]['HealthCheckPath'] == '/healthz'

    @pytest.mark.django_db
    def test_create_target_group_reuses_existing(
        self, alb_service, mock_aws_factory, mock_elbv2_client, cluster,
    ):
        """Test that existing target group is reused."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client

        mock_elbv2_client.describe_target_groups.return_value = {
            'TargetGroups': [{
                'TargetGroupArn': 'arn:tg:existing',
                'Protocol': 'HTTP',
            }]
        }

        result = alb_service.create_target_group(
            cluster=cluster, vpc_id='vpc-test', service_name='web', port=80,
        )

        assert result.target_group_arn == 'arn:tg:existing'
        mock_elbv2_client.create_target_group.assert_not_called()

    # -- create_listener_rule -----------------------------------------------

    @pytest.mark.django_db
    def test_create_listener_rule_creates_rule(
        self, alb_service, mock_aws_factory, mock_elbv2_client,
    ):
        """Test create_listener_rule creates a routing rule."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client

        mock_elbv2_client.create_rule.return_value = {
            'Rules': [{'RuleArn': 'arn:rule:1'}]
        }

        rule_arn = alb_service.create_listener_rule(
            listener_arn='arn:listener:http',
            target_group_arn='arn:tg:web',
            priority=100,
        )

        assert rule_arn == 'arn:rule:1'

        call_args = mock_elbv2_client.create_rule.call_args
        assert call_args[1]['Priority'] == 100
        assert call_args[1]['Actions'][0]['TargetGroupArn'] == 'arn:tg:web'

    @pytest.mark.django_db
    def test_create_listener_rule_with_custom_conditions(
        self, alb_service, mock_aws_factory, mock_elbv2_client,
    ):
        """Test listener rule with path-pattern condition."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client

        mock_elbv2_client.create_rule.return_value = {
            'Rules': [{'RuleArn': 'arn:rule:api'}]
        }

        conditions = [{'Field': 'path-pattern', 'Values': ['/api/*']}]

        rule_arn = alb_service.create_listener_rule(
            listener_arn='arn:listener:http',
            target_group_arn='arn:tg:api',
            priority=50,
            conditions=conditions,
        )

        assert rule_arn == 'arn:rule:api'
        call_args = mock_elbv2_client.create_rule.call_args
        assert call_args[1]['Conditions'] == conditions

    # -- ALB error handling -------------------------------------------------

    @pytest.mark.django_db
    def test_provision_alb_raises_on_insufficient_subnets(
        self, alb_service, mock_aws_factory, mock_elbv2_client, cluster,
    ):
        """Test that provision_alb raises when fewer than 2 public subnets exist."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client

        mock_elbv2_client.describe_load_balancers.side_effect = _make_client_error(
            'LoadBalancerNotFound', 'Not found', 'DescribeLoadBalancers'
        )

        vpc_infra_one_subnet = MagicMock()
        vpc_infra_one_subnet.public_subnet_ids = ['subnet-only-one']
        vpc_infra_one_subnet.vpc_id = 'vpc-test'

        with pytest.raises(ALBProvisioningError, match='2 public subnets'):
            alb_service.provision_alb(
                cluster, vpc_infra_one_subnet, security_group_id='sg-alb',
            )

    def test_create_listener_rule_raises_on_priority_in_use(
        self, alb_service, mock_aws_factory, mock_elbv2_client,
    ):
        """Test that PriorityInUse error raises ALBProvisioningError."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client

        mock_elbv2_client.create_rule.side_effect = _make_client_error(
            'PriorityInUse', 'Priority 100 is already in use', 'CreateRule'
        )

        with pytest.raises(ALBProvisioningError, match='priority'):
            alb_service.create_listener_rule(
                listener_arn='arn:listener:http',
                target_group_arn='arn:tg:web',
                priority=100,
            )

    def test_create_target_group_raises_on_client_error(
        self, alb_service, mock_aws_factory, mock_elbv2_client,
    ):
        """Test that target group creation failure raises TargetGroupError."""
        mock_aws_factory.get_client.return_value = mock_elbv2_client

        mock_elbv2_client.describe_target_groups.side_effect = _make_client_error(
            'TargetGroupNotFound', 'Not found', 'DescribeTargetGroups'
        )
        mock_elbv2_client.create_target_group.side_effect = _make_client_error(
            'TooManyTargetGroups', 'Limit exceeded', 'CreateTargetGroup'
        )

        cluster_mock = MagicMock()
        cluster_mock.name = 'test-cluster'

        with pytest.raises(TargetGroupError):
            alb_service.create_target_group(
                cluster=cluster_mock, vpc_id='vpc-test',
                service_name='web', port=80,
            )


# ===========================================================================
# TestSecretsService
# ===========================================================================

class TestSecretsService:
    """Tests for SecretsService."""

    @pytest.fixture
    def secrets_service(self, mock_aws_factory):
        return SecretsService(aws_factory=mock_aws_factory)

    # -- get_or_create_secret: new secret -----------------------------------

    @pytest.mark.django_db
    def test_get_or_create_secret_creates_new(
        self, secrets_service, mock_aws_factory, mock_sm_client, cluster,
    ):
        """Test that a new secret is created when it does not exist."""
        mock_aws_factory.get_client.return_value = mock_sm_client

        # describe_secret raises ResourceNotFoundException
        mock_sm_client.describe_secret.side_effect = _make_client_error(
            'ResourceNotFoundException', 'Not found', 'DescribeSecret'
        )

        mock_sm_client.create_secret.return_value = {
            'ARN': 'arn:aws:secretsmanager:us-east-1:123:secret:test-cluster/DB_PASSWORD-abc',
        }

        arn = secrets_service.get_or_create_secret(
            cluster=cluster, name='DB_PASSWORD', value='s3cr3t',
        )

        assert arn == 'arn:aws:secretsmanager:us-east-1:123:secret:test-cluster/DB_PASSWORD-abc'
        mock_sm_client.create_secret.assert_called_once()

        # Verify DB record
        sc = SecretConfig.objects.get(cluster=cluster, env_var_name='DB_PASSWORD')
        assert sc.secret_name == 'test-cluster/DB_PASSWORD'

    # -- get_or_create_secret: update existing ------------------------------

    @pytest.mark.django_db
    def test_get_or_create_secret_updates_existing(
        self, secrets_service, mock_aws_factory, mock_sm_client, cluster,
    ):
        """Test that an existing secret value is updated."""
        mock_aws_factory.get_client.return_value = mock_sm_client

        mock_sm_client.describe_secret.return_value = {
            'ARN': 'arn:aws:secretsmanager:us-east-1:123:secret:test-cluster/API_KEY-xyz',
        }

        arn = secrets_service.get_or_create_secret(
            cluster=cluster, name='API_KEY', value='new-value',
        )

        assert arn == 'arn:aws:secretsmanager:us-east-1:123:secret:test-cluster/API_KEY-xyz'
        mock_sm_client.put_secret_value.assert_called_once_with(
            SecretId='test-cluster/API_KEY',
            SecretString='new-value',
        )
        mock_sm_client.create_secret.assert_not_called()

    # -- push_env_file ------------------------------------------------------

    @pytest.mark.django_db
    def test_push_env_file_creates_secrets_for_each_var(
        self, secrets_service, mock_aws_factory, mock_sm_client, cluster, tmp_path,
    ):
        """Test that push_env_file reads an env file and creates one secret per var."""
        mock_aws_factory.get_client.return_value = mock_sm_client

        env_file = tmp_path / '.env'
        env_file.write_text('DB_HOST=localhost\nDB_PORT=5432\n')

        # Each describe_secret will not find the secret
        mock_sm_client.describe_secret.side_effect = _make_client_error(
            'ResourceNotFoundException', 'Not found', 'DescribeSecret'
        )

        call_count = {'n': 0}

        def _create_secret(**kwargs):
            call_count['n'] += 1
            return {'ARN': f'arn:secret:{call_count["n"]}'}

        mock_sm_client.create_secret.side_effect = _create_secret

        result = secrets_service.push_env_file(
            cluster=cluster, env_file_path=str(env_file),
        )

        assert len(result) == 2
        assert 'DB_HOST' in result
        assert 'DB_PORT' in result

    # -- build_ecs_secrets_config -------------------------------------------

    def test_build_ecs_secrets_config_returns_correct_format(
        self, secrets_service,
    ):
        """Test that build_ecs_secrets_config returns sorted name/valueFrom dicts."""
        arns = {
            'DB_PASSWORD': 'arn:secret:db-pw',
            'API_KEY': 'arn:secret:api',
        }

        result = secrets_service.build_ecs_secrets_config(arns)

        assert len(result) == 2
        # Sorted by name: API_KEY first
        assert result[0] == {'name': 'API_KEY', 'valueFrom': 'arn:secret:api'}
        assert result[1] == {'name': 'DB_PASSWORD', 'valueFrom': 'arn:secret:db-pw'}

    # -- _parse_env_file: comments, blank lines, quoted values ---------------

    def test_parse_env_file_handles_comments_and_blanks(
        self, secrets_service, tmp_path,
    ):
        """Test that _parse_env_file skips comments and blank lines."""
        env_file = tmp_path / '.env'
        env_file.write_text(
            '# This is a comment\n'
            '\n'
            'KEY1=value1\n'
            '  # Another comment\n'
            '\n'
            'KEY2=value2\n'
        )

        result = secrets_service._parse_env_file(str(env_file))

        assert result == {'KEY1': 'value1', 'KEY2': 'value2'}

    def test_parse_env_file_strips_quotes(self, secrets_service, tmp_path):
        """Test that surrounding single and double quotes are stripped from values."""
        env_file = tmp_path / '.env'
        env_file.write_text(
            'DOUBLE="hello world"\n'
            "SINGLE='foo bar'\n"
            'NONE=no quotes\n'
        )

        result = secrets_service._parse_env_file(str(env_file))

        assert result['DOUBLE'] == 'hello world'
        assert result['SINGLE'] == 'foo bar'
        assert result['NONE'] == 'no quotes'

    def test_parse_env_file_splits_on_first_equals(self, secrets_service, tmp_path):
        """Test that values with = signs are preserved correctly."""
        env_file = tmp_path / '.env'
        env_file.write_text('DATABASE_URL=postgres://user:pass@host:5432/db?sslmode=require\n')

        result = secrets_service._parse_env_file(str(env_file))

        assert result['DATABASE_URL'] == 'postgres://user:pass@host:5432/db?sslmode=require'

    def test_parse_env_file_raises_on_missing_file(self, secrets_service):
        """Test that a missing file raises SecretProvisioningError."""
        with pytest.raises(SecretProvisioningError, match='not found'):
            secrets_service._parse_env_file('/nonexistent/.env')

    # -- error handling ----------------------------------------------------

    @pytest.mark.django_db
    def test_get_or_create_secret_raises_on_non_rnf_error(
        self, secrets_service, mock_aws_factory, mock_sm_client, cluster,
    ):
        """Test that non-ResourceNotFoundException errors are raised."""
        mock_aws_factory.get_client.return_value = mock_sm_client

        mock_sm_client.describe_secret.side_effect = _make_client_error(
            'AccessDeniedException', 'Access denied', 'DescribeSecret'
        )

        with pytest.raises(SecretProvisioningError):
            secrets_service.get_or_create_secret(
                cluster=cluster, name='FORBIDDEN', value='val',
            )


# ===========================================================================
# TestServiceConnectService
# ===========================================================================

class TestServiceConnectService:
    """Tests for ServiceConnectService."""

    @pytest.fixture
    def sc_service(self, mock_aws_factory):
        return ServiceConnectService(aws_factory=mock_aws_factory)

    # -- get_or_create_namespace: creates new --------------------------------

    @pytest.mark.django_db
    @patch('time.sleep')
    def test_get_or_create_namespace_creates_new(
        self, mock_sleep, sc_service, mock_aws_factory, mock_sd_client, cluster,
    ):
        """Test that a new Cloud Map namespace is created when none exists."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        # No namespace found in AWS
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{'Namespaces': []}]
        mock_sd_client.get_paginator.return_value = paginator_mock

        # create_http_namespace
        mock_sd_client.create_http_namespace.return_value = {
            'OperationId': 'op-123'
        }

        # _wait_for_operation: SUCCESS
        mock_sd_client.get_operation.return_value = {
            'Operation': {
                'Status': 'SUCCESS',
                'Targets': {'NAMESPACE': 'ns-abc'},
            }
        }

        mock_sd_client.get_namespace.return_value = {
            'Namespace': {
                'Arn': 'arn:aws:servicediscovery:us-east-1:123:namespace/ns-abc',
            }
        }

        result = sc_service.get_or_create_namespace(cluster)

        assert isinstance(result, ServiceConnectNamespace)
        assert result.namespace_id == 'ns-abc'
        assert result.namespace_name == 'test-cluster'
        assert result.cluster == cluster
        mock_sd_client.create_http_namespace.assert_called_once()

    # -- get_or_create_namespace: reuses existing DB record ------------------

    @pytest.mark.django_db
    def test_get_or_create_namespace_reuses_existing_db_record(
        self, sc_service, mock_aws_factory, mock_sd_client, cluster,
    ):
        """Test that an existing DB namespace is reused if it still exists in AWS."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        existing = ServiceConnectNamespace.objects.create(
            cluster=cluster,
            namespace_id='ns-existing',
            namespace_name='test-cluster',
            namespace_arn='arn:ns:existing',
            namespace_type='HTTP',
        )

        # Verify it exists in AWS
        mock_sd_client.get_namespace.return_value = {
            'Namespace': {'Id': 'ns-existing'}
        }

        result = sc_service.get_or_create_namespace(cluster)

        assert result.pk == existing.pk
        assert result.namespace_id == 'ns-existing'
        mock_sd_client.create_http_namespace.assert_not_called()

    # -- get_or_create_namespace: reuses existing AWS namespace ----

    @pytest.mark.django_db
    def test_get_or_create_namespace_reuses_existing_in_aws(
        self, sc_service, mock_aws_factory, mock_sd_client, cluster,
    ):
        """Test that an existing AWS namespace is linked when no DB record exists."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [
            {'Namespaces': [
                {'Id': 'ns-found', 'Name': 'test-cluster', 'Arn': 'arn:ns:found'}
            ]}
        ]
        mock_sd_client.get_paginator.return_value = paginator_mock

        result = sc_service.get_or_create_namespace(cluster)

        assert result.namespace_id == 'ns-found'
        mock_sd_client.create_http_namespace.assert_not_called()

    # -- build_service_connect_config ---------------------------------------

    def test_build_service_connect_config_returns_correct_structure(
        self, sc_service,
    ):
        """Test that build_service_connect_config returns the correct dict."""
        config = sc_service.build_service_connect_config(
            namespace_name='my-namespace',
            service_name='web',
            port=8080,
        )

        assert config['enabled'] is True
        assert config['namespace'] == 'my-namespace'
        assert len(config['services']) == 1

        svc = config['services'][0]
        assert svc['portName'] == 'web'
        assert svc['discoveryName'] == 'web'
        assert svc['clientAliases'][0]['port'] == 8080
        assert svc['clientAliases'][0]['dnsName'] == 'web'

    def test_build_service_connect_config_with_port_name(
        self, sc_service,
    ):
        """Test that a custom port_name is used when provided."""
        config = sc_service.build_service_connect_config(
            namespace_name='ns',
            service_name='api',
            port=3000,
            port_name='http-api',
        )

        assert config['services'][0]['portName'] == 'http-api'
        assert config['services'][0]['discoveryName'] == 'api'

    # -- _wait_for_operation: SUCCESS, FAIL, timeout -------------------------

    @patch('time.sleep')
    def test_wait_for_operation_success(
        self, mock_sleep, sc_service, mock_aws_factory, mock_sd_client,
    ):
        """Test _wait_for_operation returns namespace ID on SUCCESS."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        mock_sd_client.get_operation.return_value = {
            'Operation': {
                'Status': 'SUCCESS',
                'Targets': {'NAMESPACE': 'ns-created'},
            }
        }

        result = sc_service._wait_for_operation(mock_sd_client, 'op-1')
        assert result == 'ns-created'

    @patch('time.sleep')
    def test_wait_for_operation_fail(
        self, mock_sleep, sc_service, mock_aws_factory, mock_sd_client,
    ):
        """Test _wait_for_operation raises NamespaceError on FAIL."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        mock_sd_client.get_operation.return_value = {
            'Operation': {
                'Status': 'FAIL',
                'ErrorMessage': 'Something went wrong',
            }
        }

        with pytest.raises(NamespaceError, match='failed'):
            sc_service._wait_for_operation(mock_sd_client, 'op-fail')

    @patch('time.time')
    @patch('time.sleep')
    def test_wait_for_operation_timeout(
        self, mock_sleep, mock_time, sc_service, mock_aws_factory, mock_sd_client,
    ):
        """Test _wait_for_operation raises NamespaceError on timeout."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        # Simulate time passing beyond timeout
        # First call: start_time = 0
        # Second call (while condition): 0 < 300 => True, enters loop
        # Third call (while condition): 301 < 300 => False, exits loop
        mock_time.side_effect = [0, 0, 301]

        mock_sd_client.get_operation.return_value = {
            'Operation': {'Status': 'SUBMITTED'}
        }

        with pytest.raises(NamespaceError, match='Timeout'):
            sc_service._wait_for_operation(mock_sd_client, 'op-slow', timeout=300)

    # -- error handling -----------------------------------------------------

    @pytest.mark.django_db
    def test_get_or_create_namespace_raises_on_client_error(
        self, sc_service, mock_aws_factory, mock_sd_client, cluster,
    ):
        """Test that ClientError during creation raises NamespaceError."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        # No existing namespace found
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{'Namespaces': []}]
        mock_sd_client.get_paginator.return_value = paginator_mock

        mock_sd_client.create_http_namespace.side_effect = _make_client_error(
            'TooManyRequestsException', 'Throttled', 'CreateHttpNamespace'
        )

        with pytest.raises(NamespaceError):
            sc_service.get_or_create_namespace(cluster)

    @pytest.mark.django_db
    def test_get_or_create_namespace_removes_stale_db_record(
        self, sc_service, mock_aws_factory, mock_sd_client, cluster,
    ):
        """Test that a stale DB namespace record is removed when AWS resource is gone."""
        mock_aws_factory.get_client.return_value = mock_sd_client

        ServiceConnectNamespace.objects.create(
            cluster=cluster,
            namespace_id='ns-stale',
            namespace_name='test-cluster',
            namespace_type='HTTP',
        )

        # Namespace no longer exists in AWS
        mock_sd_client.get_namespace.side_effect = _make_client_error(
            'NamespaceNotFound', 'Gone', 'GetNamespace'
        )

        # Will try to find by name in AWS next -- nothing found
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [{'Namespaces': []}]
        mock_sd_client.get_paginator.return_value = paginator_mock

        # Then create a new one
        mock_sd_client.create_http_namespace.return_value = {
            'OperationId': 'op-new'
        }
        mock_sd_client.get_operation.return_value = {
            'Operation': {
                'Status': 'SUCCESS',
                'Targets': {'NAMESPACE': 'ns-new'},
            }
        }
        mock_sd_client.get_namespace.side_effect = [
            # First call: stale check returns error (already set above)
            _make_client_error('NamespaceNotFound', 'Gone', 'GetNamespace'),
            # Second call: after creation
            {'Namespace': {'Arn': 'arn:ns:new'}},
        ]

        # Need to reset the side_effect since we need mixed behaviour
        call_count = {'n': 0}

        def _get_namespace(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise _make_client_error(
                    'NamespaceNotFound', 'Gone', 'GetNamespace'
                )
            return {'Namespace': {'Arn': 'arn:ns:new'}}

        mock_sd_client.get_namespace.side_effect = _get_namespace

        result = sc_service.get_or_create_namespace(cluster)

        assert result.namespace_id == 'ns-new'
        # Stale record was removed
        assert not ServiceConnectNamespace.objects.filter(
            namespace_id='ns-stale'
        ).exists()
