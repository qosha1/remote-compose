# Generated migration for tracking models

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('remote_compose', '0004_add_ecr_efs_models'),
    ]

    operations = [
        migrations.CreateModel(
            name='BuildRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('service_name', models.CharField(db_index=True, help_text='Docker Compose service name', max_length=255)),
                ('image_uri', models.CharField(blank=True, help_text='Full image URI with tag', max_length=512)),
                ('image_tag', models.CharField(blank=True, help_text='Image tag', max_length=128)),
                ('context_path', models.CharField(blank=True, help_text='Docker build context path', max_length=1024)),
                ('dockerfile_path', models.CharField(blank=True, help_text='Dockerfile path relative to context', max_length=1024)),
                ('context_hash', models.CharField(blank=True, db_index=True, help_text='SHA256 hash of build context for cache detection', max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('building', 'Building'), ('pushing', 'Pushing to ECR'), ('success', 'Success'), ('failed', 'Failed'), ('skipped', 'Skipped (cached)')], db_index=True, default='pending', max_length=20)),
                ('build_started_at', models.DateTimeField(blank=True, help_text='When the Docker build started', null=True)),
                ('build_completed_at', models.DateTimeField(blank=True, help_text='When the Docker build completed', null=True)),
                ('push_started_at', models.DateTimeField(blank=True, help_text='When ECR push started', null=True)),
                ('push_completed_at', models.DateTimeField(blank=True, help_text='When ECR push completed', null=True)),
                ('error_message', models.TextField(blank=True, help_text='Error message if build failed')),
                ('build_log', models.TextField(blank=True, help_text='Docker build output log')),
                ('image_digest', models.CharField(blank=True, help_text='Image digest from ECR after push', max_length=128)),
                ('image_size_bytes', models.BigIntegerField(blank=True, help_text='Image size in bytes', null=True)),
                ('build_args', models.JSONField(blank=True, default=dict, help_text='Docker build arguments')),
                ('platform', models.CharField(default='linux/amd64', help_text='Target platform for the build', max_length=50)),
                ('cache_hit', models.BooleanField(default=False, help_text='Whether this build used cached layers')),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='Additional build metadata')),
                ('deployment', models.ForeignKey(help_text='Deployment this build is part of', on_delete=django.db.models.deletion.CASCADE, related_name='build_records', to='remote_compose.deployment')),
                ('ecr_repository', models.ForeignKey(blank=True, help_text='ECR repository the image was pushed to', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='build_records', to='remote_compose.ecrrepository')),
                ('previous_build', models.ForeignKey(blank=True, help_text='Previous build record for this service', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='subsequent_builds', to='remote_compose.buildrecord')),
            ],
            options={
                'verbose_name': 'Build Record',
                'verbose_name_plural': 'Build Records',
                'db_table': 'remote_compose_build_records',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='DeploymentEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('event_type', models.CharField(choices=[('deployment_started', 'Deployment Started'), ('deployment_completed', 'Deployment Completed'), ('deployment_failed', 'Deployment Failed'), ('build_started', 'Build Started'), ('build_completed', 'Build Completed'), ('build_failed', 'Build Failed'), ('image_pushed', 'Image Pushed to ECR'), ('efs_created', 'EFS File System Created'), ('efs_configured', 'EFS Configured'), ('access_point_created', 'Access Point Created'), ('task_def_registered', 'Task Definition Registered'), ('service_created', 'ECS Service Created'), ('service_updated', 'ECS Service Updated'), ('service_stable', 'Service Reached Stable State'), ('task_started', 'Task Started'), ('task_stopped', 'Task Stopped'), ('state_change', 'State Change'), ('error', 'Error'), ('warning', 'Warning'), ('rollback_started', 'Rollback Started'), ('rollback_completed', 'Rollback Completed'), ('resource_created', 'Resource Created'), ('resource_deleted', 'Resource Deleted'), ('cleanup_started', 'Cleanup Started'), ('cleanup_completed', 'Cleanup Completed')], db_index=True, help_text='Type of deployment event', max_length=30)),
                ('severity', models.CharField(choices=[('debug', 'Debug'), ('info', 'Info'), ('warning', 'Warning'), ('error', 'Error'), ('critical', 'Critical')], db_index=True, default='info', max_length=10)),
                ('previous_state', models.CharField(blank=True, help_text='Previous state before this event', max_length=50)),
                ('new_state', models.CharField(blank=True, help_text='New state after this event', max_length=50)),
                ('message', models.TextField(help_text='Human-readable event description')),
                ('service_name', models.CharField(blank=True, db_index=True, help_text='Docker Compose service name if applicable', max_length=255)),
                ('resource_type', models.CharField(blank=True, help_text='Type of AWS resource', max_length=50)),
                ('resource_id', models.CharField(blank=True, help_text='AWS resource ID or ARN', max_length=512)),
                ('duration_ms', models.IntegerField(blank=True, help_text='Duration of the operation in milliseconds', null=True)),
                ('error_code', models.CharField(blank=True, help_text='Error code if this is an error event', max_length=100)),
                ('stack_trace', models.TextField(blank=True, help_text='Stack trace for error events')),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='Additional event-specific data')),
                ('deployment', models.ForeignKey(help_text='Deployment this event belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='events', to='remote_compose.deployment')),
            ],
            options={
                'verbose_name': 'Deployment Event',
                'verbose_name_plural': 'Deployment Events',
                'db_table': 'remote_compose_deployment_events',
                'ordering': ['created_at'],
            },
        ),
        migrations.CreateModel(
            name='ResourceMetric',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('metric_type', models.CharField(choices=[('cpu_utilization', 'CPU Utilization (%)'), ('memory_utilization', 'Memory Utilization (%)'), ('memory_used', 'Memory Used (MB)'), ('network_in', 'Network In (bytes)'), ('network_out', 'Network Out (bytes)'), ('running_tasks', 'Running Tasks'), ('pending_tasks', 'Pending Tasks'), ('desired_tasks', 'Desired Tasks'), ('deployment_count', 'Active Deployments'), ('request_count', 'Request Count'), ('error_count', 'Error Count'), ('response_time', 'Response Time (ms)'), ('efs_connections', 'EFS Client Connections'), ('efs_throughput', 'EFS Throughput (bytes/s)'), ('efs_size', 'EFS Size (bytes)')], db_index=True, help_text='Type of metric being recorded', max_length=30)),
                ('aggregation', models.CharField(choices=[('avg', 'Average'), ('sum', 'Sum'), ('min', 'Minimum'), ('max', 'Maximum'), ('count', 'Count'), ('p50', '50th Percentile'), ('p90', '90th Percentile'), ('p99', '99th Percentile')], default='avg', help_text='How this metric was aggregated', max_length=10)),
                ('value', models.FloatField(help_text='Metric value')),
                ('unit', models.CharField(blank=True, help_text='Unit of measurement', max_length=20)),
                ('period_start', models.DateTimeField(db_index=True, help_text='Start of the measurement period')),
                ('period_end', models.DateTimeField(help_text='End of the measurement period')),
                ('period_seconds', models.IntegerField(default=60, help_text='Duration of the measurement period in seconds')),
                ('sample_count', models.IntegerField(default=1, help_text='Number of data points in this aggregation')),
                ('dimensions', models.JSONField(blank=True, default=dict, help_text='Additional dimensions for metric filtering')),
                ('cluster', models.ForeignKey(blank=True, help_text='ECS cluster this metric is for', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='metrics', to='remote_compose.ecscluster')),
                ('ecs_service', models.ForeignKey(blank=True, help_text='ECS service this metric is for', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='metrics', to='remote_compose.ecsservice')),
                ('efs_file_system', models.ForeignKey(blank=True, help_text='EFS file system this metric is for', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='metrics', to='remote_compose.efsfilesystem')),
            ],
            options={
                'verbose_name': 'Resource Metric',
                'verbose_name_plural': 'Resource Metrics',
                'db_table': 'remote_compose_resource_metrics',
                'ordering': ['-period_start'],
            },
        ),
        # Add indexes for BuildRecord
        migrations.AddIndex(
            model_name='buildrecord',
            index=models.Index(fields=['deployment', 'service_name'], name='remote_comp_deploym_d1e534_idx'),
        ),
        migrations.AddIndex(
            model_name='buildrecord',
            index=models.Index(fields=['status', 'created_at'], name='remote_comp_status_b2a3c1_idx'),
        ),
        migrations.AddIndex(
            model_name='buildrecord',
            index=models.Index(fields=['context_hash'], name='remote_comp_context_4f5d2e_idx'),
        ),
        migrations.AddIndex(
            model_name='buildrecord',
            index=models.Index(fields=['ecr_repository', 'image_tag'], name='remote_comp_ecr_rep_5a6b7c_idx'),
        ),
        # Add indexes for DeploymentEvent
        migrations.AddIndex(
            model_name='deploymentevent',
            index=models.Index(fields=['deployment', 'created_at'], name='remote_comp_deploym_e7f8a9_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymentevent',
            index=models.Index(fields=['event_type', 'created_at'], name='remote_comp_event_t_b0c1d2_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymentevent',
            index=models.Index(fields=['severity', 'created_at'], name='remote_comp_severit_e3f4a5_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymentevent',
            index=models.Index(fields=['service_name', 'event_type'], name='remote_comp_service_6b7c8d_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymentevent',
            index=models.Index(fields=['resource_type', 'resource_id'], name='remote_comp_resourc_9a0b1c_idx'),
        ),
        # Add indexes for ResourceMetric
        migrations.AddIndex(
            model_name='resourcemetric',
            index=models.Index(fields=['ecs_service', 'metric_type', 'period_start'], name='remote_comp_ecs_ser_2d3e4f_idx'),
        ),
        migrations.AddIndex(
            model_name='resourcemetric',
            index=models.Index(fields=['efs_file_system', 'metric_type', 'period_start'], name='remote_comp_efs_fil_5g6h7i_idx'),
        ),
        migrations.AddIndex(
            model_name='resourcemetric',
            index=models.Index(fields=['cluster', 'metric_type', 'period_start'], name='remote_comp_cluster_8j9k0l_idx'),
        ),
        migrations.AddIndex(
            model_name='resourcemetric',
            index=models.Index(fields=['metric_type', 'period_start'], name='remote_comp_metric__m1n2o3_idx'),
        ),
    ]
