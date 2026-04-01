"""
Tests for ComposeToECSConverter.convert_per_service() method.

Verifies that each compose service is converted into its own ECS task
definition with correct naming, resource allocation, secrets handling,
shared images, EFS volumes, and strict mode behavior.
"""

import pytest

from tests.conftest import make_preprocessed_from_tuples as make_preprocessed

from remote_compose.models import ECSCluster, ECSTaskDefinition
from remote_compose.services import ComposeToECSConverter
from remote_compose.services.compose_preprocessor import (
    PreprocessedCompose,
    PreprocessedService,
    BuildInfo,
    VolumeInfo,
    VolumeType,
)
from remote_compose.exceptions import ComposeConversionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def converter():
    return ComposeToECSConverter()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestConvertPerService:
    """Tests for the convert_per_service() multi-task-definition converter."""

    def test_convert_single_service(self, converter, cluster):
        """A single-service compose yields exactly one task definition."""
        preprocessed = make_preprocessed(
            ('nginx', {'image': 'nginx:alpine', 'ports': [{'target': 80, 'published': 80}]}, 'nginx:alpine', None),
        )

        result = converter.convert_per_service(preprocessed, cluster, 'myproject')

        assert len(result) == 1
        assert 'nginx' in result
        td = result['nginx']
        assert isinstance(td, ECSTaskDefinition)
        td.save()  # convert_per_service returns unsaved objects; caller saves
        assert td.pk is not None  # saved to DB
        assert len(td.container_definitions) == 1
        assert td.container_definitions[0]['image'] == 'nginx:alpine'

    def test_convert_multiple_services(self, converter, cluster):
        """Each compose service produces its own separate task definition."""
        preprocessed = make_preprocessed(
            ('web', {'image': 'nginx:alpine'}, 'nginx:alpine', None),
            ('redis', {'image': 'redis:7'}, 'redis:7', None),
            ('worker', {'image': 'myapp:latest'}, 'myapp:latest', None),
        )

        result = converter.convert_per_service(preprocessed, cluster, 'proj')

        assert set(result.keys()) == {'web', 'redis', 'worker'}
        for td in result.values():
            assert len(td.container_definitions) == 1

    def test_task_family_naming(self, converter, cluster):
        """Task family follows the {project_name}-{service_name} convention."""
        preprocessed = make_preprocessed(
            ('api', {'image': 'myapi:v1'}, 'myapi:v1', None),
        )

        result = converter.convert_per_service(preprocessed, cluster, 'acme')

        td = result['api']
        assert td.name == 'acme-api'

    def test_resource_overrides(self, converter, cluster):
        """Per-service CPU/memory overrides from service_resources are applied."""
        preprocessed = make_preprocessed(
            ('heavy', {'image': 'heavy:latest'}, 'heavy:latest', None),
        )

        result = converter.convert_per_service(
            preprocessed,
            cluster,
            'proj',
            service_resources={'heavy': {'cpu': '2048', 'memory': '4096'}},
        )

        td = result['heavy']
        assert td.cpu == '2048'
        assert td.memory == '4096'

    def test_default_fargate_resources(self, converter, cluster):
        """Without overrides, resources snap to valid Fargate combos (>= 256/512)."""
        preprocessed = make_preprocessed(
            ('tiny', {'image': 'alpine:latest'}, 'alpine:latest', None),
        )

        result = converter.convert_per_service(preprocessed, cluster, 'proj')

        td = result['tiny']
        cpu = int(td.cpu)
        mem = int(td.memory)
        # Must be a valid Fargate CPU value
        assert cpu in [256, 512, 1024, 2048, 4096, 8192, 16384]
        # Memory must be in the allowed list for the chosen CPU
        from remote_compose.services.compose_converter import FARGATE_CPU_MEMORY_MAP
        assert mem in FARGATE_CPU_MEMORY_MAP[str(cpu)]

    def test_secrets_arns_replacement(self, converter, cluster):
        """Env vars whose names appear in secrets_arns are moved to ECS secrets list."""
        preprocessed = make_preprocessed(
            ('app', {
                'image': 'myapp:latest',
                'environment': {'DB_PASSWORD': 'placeholder', 'API_KEY': 'placeholder'},
            }, 'myapp:latest', None),
        )

        secrets_arns = {
            'DB_PASSWORD': 'arn:aws:secretsmanager:us-east-1:123456789:secret:db-pass',
            'API_KEY': 'arn:aws:secretsmanager:us-east-1:123456789:secret:api-key',
        }

        result = converter.convert_per_service(
            preprocessed, cluster, 'proj', secrets_arns=secrets_arns,
        )

        container = result['app'].container_definitions[0]
        # All matching env vars should have been moved to secrets
        remaining_env_names = [e['name'] for e in container.get('environment', [])]
        assert 'DB_PASSWORD' not in remaining_env_names
        assert 'API_KEY' not in remaining_env_names

        secret_names = [s['name'] for s in container.get('secrets', [])]
        assert 'DB_PASSWORD' in secret_names
        assert 'API_KEY' in secret_names

        # Verify the ARN values
        secrets_by_name = {s['name']: s for s in container['secrets']}
        assert secrets_by_name['DB_PASSWORD']['valueFrom'] == secrets_arns['DB_PASSWORD']

    def test_secrets_arns_partial_match(self, converter, cluster):
        """Only env vars matching secrets_arns keys become secrets; others stay as env."""
        preprocessed = make_preprocessed(
            ('app', {
                'image': 'myapp:latest',
                'environment': {'DB_PASSWORD': 'secret123', 'DEBUG': 'false', 'PORT': '5000'},
            }, 'myapp:latest', None),
        )

        secrets_arns = {
            'DB_PASSWORD': 'arn:aws:secretsmanager:us-east-1:123456789:secret:db-pass',
        }

        result = converter.convert_per_service(
            preprocessed, cluster, 'proj', secrets_arns=secrets_arns,
        )

        container = result['app'].container_definitions[0]
        remaining_env_names = [e['name'] for e in container.get('environment', [])]

        # DB_PASSWORD should have been pulled out
        assert 'DB_PASSWORD' not in remaining_env_names
        # The others should remain as normal env vars
        assert 'DEBUG' in remaining_env_names
        assert 'PORT' in remaining_env_names

        secret_names = [s['name'] for s in container.get('secrets', [])]
        assert secret_names == ['DB_PASSWORD']

    def test_shared_images_per_service_uri(self, converter, cluster):
        """Each service keeps its own ECR URI even when sharing a build context."""
        build = BuildInfo(context='./app', dockerfile='Dockerfile')
        web_uri = '123456789.dkr.ecr.us-east-1.amazonaws.com/proj/web:latest'
        worker_uri = '123456789.dkr.ecr.us-east-1.amazonaws.com/proj/worker:latest'
        preprocessed = make_preprocessed(
            ('web', {
                'build': {'context': './app', 'dockerfile': 'Dockerfile'},
                'image': web_uri,
            }, web_uri, build),
            ('worker', {
                'build': {'context': './app', 'dockerfile': 'Dockerfile'},
                'image': worker_uri,
            }, worker_uri, BuildInfo(context='./app', dockerfile='Dockerfile')),
        )

        # shared_images stores the primary URI but should NOT override per-service URIs
        shared_images = {
            'app:Dockerfile': web_uri,
        }

        result = converter.convert_per_service(
            preprocessed, cluster, 'proj', shared_images=shared_images,
        )

        assert result['web'].container_definitions[0]['image'] == web_uri
        assert result['worker'].container_definitions[0]['image'] == worker_uri

    def test_shared_images_fallback(self, converter, cluster):
        """Services without image_name fall back to shared_images URI."""
        build = BuildInfo(context='./app', dockerfile='Dockerfile')
        shared_ecr_uri = '123456789.dkr.ecr.us-east-1.amazonaws.com/myapp:latest'
        preprocessed = make_preprocessed(
            ('web', {
                'build': {'context': './app', 'dockerfile': 'Dockerfile'},
                'image': 'placeholder:latest',
            }, '', build),  # empty image_name triggers fallback
        )

        shared_images = {
            'app:Dockerfile': shared_ecr_uri,
        }

        result = converter.convert_per_service(
            preprocessed, cluster, 'proj', shared_images=shared_images,
        )

        assert result['web'].container_definitions[0]['image'] == shared_ecr_uri

    def test_efs_volumes(self, converter, cluster):
        """EFS volume configuration is attached to task definitions when provided."""
        svc = PreprocessedService(
            name='data',
            config={'image': 'myapp:latest'},
            image_name='myapp:latest',
            volumes=[
                VolumeInfo(
                    source='pgdata',
                    target='/var/lib/postgresql/data',
                    volume_type=VolumeType.NAMED,
                ),
            ],
        )
        preprocessed = PreprocessedCompose(
            services={'data': svc},
            named_volumes={'pgdata': {}},
        )

        efs_config = {
            'pgdata': {
                'file_system_id': 'fs-abc123',
                'access_point_id': 'fsap-xyz789',
            },
        }

        result = converter.convert_per_service(
            preprocessed, cluster, 'proj', efs_config=efs_config,
        )

        td = result['data']
        # Find the EFS volume definition
        efs_vols = [v for v in td.volumes if 'efsVolumeConfiguration' in v]
        assert len(efs_vols) >= 1

        efs_vol = efs_vols[0]
        assert efs_vol['efsVolumeConfiguration']['fileSystemId'] == 'fs-abc123'

    def test_strict_mode_with_warnings(self, converter, cluster):
        """strict_mode=True raises ComposeConversionError when warnings exist."""
        preprocessed = make_preprocessed(
            ('web', {'image': 'nginx:alpine'}, 'nginx:alpine', None),
            warnings=['Some warning about incompatibility'],
        )

        with pytest.raises(ComposeConversionError, match='Strict mode'):
            converter.convert_per_service(
                preprocessed, cluster, 'proj', strict_mode=True,
            )

    def test_empty_services_raises(self, converter, cluster):
        """Raises ComposeConversionError when no active services exist."""
        # All services are skipped
        svc = PreprocessedService(
            name='skipped',
            config={'image': 'x:latest'},
            image_name='x:latest',
            skip=True,
            skip_reason='replicas: 0',
        )
        preprocessed = PreprocessedCompose(services={'skipped': svc})

        with pytest.raises(ComposeConversionError, match='No active services'):
            converter.convert_per_service(preprocessed, cluster, 'proj')

    def test_errors_in_preprocessed_raises(self, converter, cluster):
        """Raises ComposeConversionError when preprocessed.errors is non-empty."""
        preprocessed = make_preprocessed(
            ('web', {'image': 'nginx:alpine'}, 'nginx:alpine', None),
            errors=['Invalid YAML syntax: unexpected token'],
        )

        with pytest.raises(ComposeConversionError, match='errors'):
            converter.convert_per_service(preprocessed, cluster, 'proj')

    def test_revision_increments(self, converter, cluster):
        """Second call for the same task family creates revision 2."""
        preprocessed = make_preprocessed(
            ('api', {'image': 'myapi:v1'}, 'myapi:v1', None),
        )

        result1 = converter.convert_per_service(preprocessed, cluster, 'proj')
        assert result1['api'].revision == 1
        result1['api'].save()  # Must save before second call can detect revision

        result2 = converter.convert_per_service(preprocessed, cluster, 'proj')
        assert result2['api'].revision == 2
        result2['api'].save()

        # Verify both exist in the database
        count = ECSTaskDefinition.objects.filter(
            cluster=cluster,
            name='proj-api',
        ).count()
        assert count == 2
