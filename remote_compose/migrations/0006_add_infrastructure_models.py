"""Migration for infrastructure models and ECSService Service Connect fields."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('remote_compose', '0005_add_tracking_models'),
    ]

    operations = [
        # Add Service Connect fields to ECSService
        migrations.AddField(
            model_name='ecsservice',
            name='service_connect_enabled',
            field=models.BooleanField(default=False, help_text='Whether Service Connect is enabled for this service'),
        ),
        migrations.AddField(
            model_name='ecsservice',
            name='service_connect_namespace',
            field=models.CharField(blank=True, help_text='Service Connect namespace name', max_length=255),
        ),
        migrations.AddField(
            model_name='ecsservice',
            name='service_connect_port_name',
            field=models.CharField(blank=True, help_text='Port name for Service Connect discovery', max_length=255),
        ),
        migrations.AddField(
            model_name='ecsservice',
            name='service_type',
            field=models.CharField(
                choices=[
                    ('infrastructure', 'Infrastructure (DB, Cache)'),
                    ('application', 'Application (Backend API)'),
                    ('frontend', 'Frontend (Web UI)'),
                    ('worker', 'Worker (Background Jobs)'),
                    ('proxy', 'Proxy (Nginx, Load Balancer)'),
                ],
                default='application',
                help_text="Classification of this service's role",
                max_length=20,
            ),
        ),

        # VPCInfrastructure
        migrations.CreateModel(
            name='VPCInfrastructure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('vpc_id', models.CharField(db_index=True, max_length=50, unique=True)),
                ('vpc_cidr', models.CharField(default='10.0.0.0/16', max_length=20)),
                ('public_subnet_ids', models.JSONField(default=list, help_text='Public subnet IDs (for ALB, NAT gateway)')),
                ('private_subnet_ids', models.JSONField(default=list, help_text='Private subnet IDs (for ECS tasks)')),
                ('internet_gateway_id', models.CharField(blank=True, max_length=50)),
                ('nat_gateway_id', models.CharField(blank=True, max_length=50)),
                ('elastic_ip_allocation_id', models.CharField(blank=True, max_length=50)),
                ('public_route_table_id', models.CharField(blank=True, max_length=50)),
                ('private_route_table_id', models.CharField(blank=True, max_length=50)),
                ('is_managed', models.BooleanField(default=True, help_text='Whether this VPC was created by remote-compose')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('cluster', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='vpc_infrastructure', to='remote_compose.ecscluster')),
            ],
            options={
                'verbose_name': 'VPC Infrastructure',
                'verbose_name_plural': 'VPC Infrastructures',
                'db_table': 'remote_compose_vpc_infrastructure',
            },
        ),

        # SecurityGroupConfig
        migrations.CreateModel(
            name='SecurityGroupConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('security_group_id', models.CharField(db_index=True, max_length=50, unique=True)),
                ('purpose', models.CharField(choices=[('alb', 'Application Load Balancer'), ('ecs_tasks', 'ECS Tasks'), ('database', 'Database (PostgreSQL)'), ('cache', 'Cache (Redis)'), ('efs', 'Elastic File System')], max_length=20)),
                ('vpc_id', models.CharField(max_length=50)),
                ('inbound_rules', models.JSONField(default=list, help_text='Inbound security group rules')),
                ('outbound_rules', models.JSONField(default=list, help_text='Outbound security group rules')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='security_groups', to='remote_compose.ecscluster')),
            ],
            options={
                'verbose_name': 'Security Group',
                'verbose_name_plural': 'Security Groups',
                'db_table': 'remote_compose_security_groups',
                'unique_together': {('cluster', 'purpose')},
            },
        ),

        # LoadBalancerConfig
        migrations.CreateModel(
            name='LoadBalancerConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('alb_arn', models.CharField(db_index=True, max_length=512, unique=True)),
                ('alb_dns_name', models.CharField(blank=True, max_length=512)),
                ('alb_hosted_zone_id', models.CharField(blank=True, max_length=50)),
                ('http_listener_arn', models.CharField(blank=True, max_length=512)),
                ('https_listener_arn', models.CharField(blank=True, max_length=512)),
                ('certificate_arn', models.CharField(blank=True, max_length=512)),
                ('security_group_id', models.CharField(blank=True, max_length=50)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('cluster', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='load_balancer', to='remote_compose.ecscluster')),
            ],
            options={
                'verbose_name': 'Load Balancer',
                'verbose_name_plural': 'Load Balancers',
                'db_table': 'remote_compose_load_balancers',
            },
        ),

        # TargetGroupConfig
        migrations.CreateModel(
            name='TargetGroupConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('target_group_arn', models.CharField(db_index=True, max_length=512, unique=True)),
                ('target_group_name', models.CharField(max_length=32)),
                ('port', models.IntegerField()),
                ('protocol', models.CharField(default='HTTP', max_length=10)),
                ('health_check_path', models.CharField(default='/health', max_length=255)),
                ('health_check_interval', models.IntegerField(default=30)),
                ('healthy_threshold', models.IntegerField(default=3)),
                ('unhealthy_threshold', models.IntegerField(default=3)),
                ('is_default', models.BooleanField(default=False, help_text='Whether this is the default target group for the ALB listener')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='target_groups', to='remote_compose.ecscluster')),
                ('ecs_service', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='target_group', to='remote_compose.ecsservice')),
            ],
            options={
                'verbose_name': 'Target Group',
                'verbose_name_plural': 'Target Groups',
                'db_table': 'remote_compose_target_groups',
            },
        ),

        # SecretConfig
        migrations.CreateModel(
            name='SecretConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('secret_arn', models.CharField(db_index=True, max_length=512, unique=True)),
                ('secret_name', models.CharField(max_length=512)),
                ('env_var_name', models.CharField(help_text='Environment variable name this secret maps to', max_length=255)),
                ('source_file', models.CharField(blank=True, help_text='Source env file this secret was read from', max_length=512)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='managed_secrets', to='remote_compose.ecscluster')),
            ],
            options={
                'verbose_name': 'Secret Config',
                'verbose_name_plural': 'Secret Configs',
                'db_table': 'remote_compose_secret_configs',
                'unique_together': {('cluster', 'env_var_name')},
            },
        ),

        # ServiceConnectNamespace
        migrations.CreateModel(
            name='ServiceConnectNamespace',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('namespace_id', models.CharField(db_index=True, max_length=100, unique=True)),
                ('namespace_name', models.CharField(max_length=255)),
                ('namespace_arn', models.CharField(blank=True, max_length=512)),
                ('namespace_type', models.CharField(default='HTTP', help_text='Namespace type: HTTP or DNS_PRIVATE', max_length=20)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('cluster', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='service_connect_namespace', to='remote_compose.ecscluster')),
            ],
            options={
                'verbose_name': 'Service Connect Namespace',
                'verbose_name_plural': 'Service Connect Namespaces',
                'db_table': 'remote_compose_service_connect_namespaces',
            },
        ),
    ]
