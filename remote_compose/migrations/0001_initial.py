from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='SecureCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(db_index=True, max_length=255, unique=True)),
                ('description', models.TextField(blank=True)),
                ('credential_type', models.CharField(choices=[('ssh_private_key', 'SSH Private Key'), ('aws_access_key', 'AWS Access Key'), ('api_token', 'API Token')], max_length=50)),
                ('encrypted_value', models.TextField(help_text='Fernet encrypted value')),
                ('aws_access_key_id', models.CharField(blank=True, help_text='AWS Access Key ID (for AWS credentials only)', max_length=255)),
                ('aws_region', models.CharField(blank=True, help_text='AWS Region (for AWS credentials only)', max_length=50)),
                ('created_by', models.CharField(blank=True, max_length=255)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('last_rotated_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'remote_compose_secure_credentials',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='DeploymentTarget',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(db_index=True, max_length=255, unique=True)),
                ('description', models.TextField(blank=True)),
                ('target_type', models.CharField(choices=[('ssh', 'SSH Connection'), ('tcp', 'TCP Connection'), ('unix', 'Unix Socket')], default='ssh', max_length=20)),
                ('host', models.CharField(max_length=255)),
                ('port', models.IntegerField(default=22)),
                ('username', models.CharField(default='ubuntu', max_length=255)),
                ('aws_instance_id', models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ('aws_region', models.CharField(blank=True, max_length=50, null=True)),
                ('environment', models.CharField(choices=[('development', 'Development'), ('staging', 'Staging'), ('production', 'Production')], default='development', max_length=20)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('health_status', models.CharField(choices=[('healthy', 'Healthy'), ('unhealthy', 'Unhealthy'), ('unknown', 'Unknown')], default='unknown', max_length=20)),
                ('last_health_check', models.DateTimeField(blank=True, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('ssh_key', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='targets', to='remote_compose.securecredential')),
            ],
            options={
                'db_table': 'remote_compose_deployment_targets',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='DockerContext',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(db_index=True, max_length=255, unique=True)),
                ('description', models.TextField(blank=True)),
                ('context_type', models.CharField(choices=[('ssh', 'SSH'), ('tcp', 'TCP'), ('unix', 'Unix Socket')], max_length=20)),
                ('endpoint', models.CharField(max_length=512)),
                ('tls_verify', models.BooleanField(default=True)),
                ('is_default', models.BooleanField(default=False)),
                ('is_synced', models.BooleanField(default=False, help_text='Whether this context is synced with local Docker daemon')),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('target', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contexts', to='remote_compose.deploymenttarget')),
            ],
            options={
                'verbose_name_plural': 'Docker Contexts',
                'db_table': 'remote_compose_docker_contexts',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='Deployment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('compose_file_path', models.CharField(max_length=1024)),
                ('compose_content', models.TextField(help_text='Snapshot of compose file at deployment time')),
                ('project_name', models.CharField(blank=True, help_text='Docker Compose project name', max_length=255)),
                ('environment', models.JSONField(blank=True, default=dict, help_text='Environment variables passed to deployment')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('success', 'Success'), ('failed', 'Failed'), ('rolled_back', 'Rolled Back'), ('cancelled', 'Cancelled')], db_index=True, default='pending', max_length=20)),
                ('deployment_type', models.CharField(choices=[('deploy', 'Deploy'), ('rollback', 'Rollback'), ('update', 'Update'), ('restart', 'Restart')], default='deploy', max_length=20)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('error_message', models.TextField(blank=True)),
                ('exit_code', models.IntegerField(blank=True, null=True)),
                ('deployed_by', models.CharField(blank=True, max_length=255)),
                ('version', models.CharField(blank=True, help_text='Version tag (git commit, release tag, etc.)', max_length=100)),
                ('container_ids', models.JSONField(blank=True, default=list, help_text='List of container IDs created by this deployment')),
                ('service_status', models.JSONField(blank=True, default=dict, help_text='Per-service status information')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('context', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='deployments', to='remote_compose.dockercontext')),
                ('target', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='deployments', to='remote_compose.deploymenttarget')),
                ('parent_deployment', models.ForeignKey(blank=True, help_text='Original deployment if this is a rollback', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rollbacks', to='remote_compose.deployment')),
            ],
            options={
                'db_table': 'remote_compose_deployments',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='DeploymentLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('log_level', models.CharField(choices=[('debug', 'Debug'), ('info', 'Info'), ('warning', 'Warning'), ('error', 'Error')], db_index=True, default='info', max_length=20)),
                ('message', models.TextField()),
                ('command', models.TextField(blank=True, help_text='Command that was executed')),
                ('output', models.TextField(blank=True, help_text='Command output')),
                ('service_name', models.CharField(blank=True, help_text='Specific service if applicable', max_length=255)),
                ('timestamp', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('deployment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='logs', to='remote_compose.deployment')),
            ],
            options={
                'db_table': 'remote_compose_deployment_logs',
                'ordering': ['timestamp'],
            },
        ),
        migrations.AddIndex(
            model_name='deploymenttarget',
            index=models.Index(fields=['target_type', 'is_active'], name='remote_comp_target__9f6e5e_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymenttarget',
            index=models.Index(fields=['environment', 'is_active'], name='remote_comp_environ_a3a0a4_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymenttarget',
            index=models.Index(fields=['aws_instance_id'], name='remote_comp_aws_ins_64b02e_idx'),
        ),
        migrations.AddIndex(
            model_name='deployment',
            index=models.Index(fields=['status', 'created_at'], name='remote_comp_status_b8e7c6_idx'),
        ),
        migrations.AddIndex(
            model_name='deployment',
            index=models.Index(fields=['target', 'status'], name='remote_comp_target__c5aee6_idx'),
        ),
        migrations.AddIndex(
            model_name='deployment',
            index=models.Index(fields=['context', 'created_at'], name='remote_comp_context_de06b9_idx'),
        ),
        migrations.AddIndex(
            model_name='deployment',
            index=models.Index(fields=['project_name', 'target'], name='remote_comp_project_98b08d_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymentlog',
            index=models.Index(fields=['deployment', 'timestamp'], name='remote_comp_deploym_7b4e08_idx'),
        ),
        migrations.AddIndex(
            model_name='deploymentlog',
            index=models.Index(fields=['log_level', 'timestamp'], name='remote_comp_log_lev_4c88a5_idx'),
        ),
    ]
