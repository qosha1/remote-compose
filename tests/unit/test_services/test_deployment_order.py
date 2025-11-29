"""
Tests for ComposePreprocessor.get_deployment_order() method.

Verifies topological sorting of service dependencies, cycle detection,
handling of skipped services, and graceful treatment of missing dependencies.
"""

import pytest

from tests.conftest import make_preprocessed_from_services as make_preprocessed

from remote_compose.services.compose_preprocessor import (
    ComposePreprocessor,
    PreprocessedCompose,
    PreprocessedService,
)
from remote_compose.exceptions import ComposeFileError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service(name, depends_on=None, skip=False):
    """
    Create a PreprocessedService with optional depends_on in its config.

    Args:
        name: Service name.
        depends_on: Value placed into config['depends_on']. Can be a list of
            strings, a dict, or a list of dicts (all formats docker-compose
            supports).
        skip: Whether the service should be skipped in ordering.
    """
    config = {'image': f'{name}:latest'}
    if depends_on is not None:
        config['depends_on'] = depends_on
    return PreprocessedService(
        name=name,
        config=config,
        image_name=f'{name}:latest',
        skip=skip,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def preprocessor():
    return ComposePreprocessor()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetDeploymentOrder:
    """Tests for topological deployment ordering of compose services."""

    def test_no_dependencies(self, preprocessor):
        """All independent services appear in output (order is non-deterministic)."""
        preprocessed = make_preprocessed(
            make_service('alpha'),
            make_service('beta'),
            make_service('gamma'),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert set(order) == {'alpha', 'beta', 'gamma'}
        assert len(order) == 3

    def test_simple_chain(self, preprocessor):
        """A -> B -> C dependency chain yields order C, B, A."""
        preprocessed = make_preprocessed(
            make_service('a', depends_on=['b']),
            make_service('b', depends_on=['c']),
            make_service('c'),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert order.index('c') < order.index('b')
        assert order.index('b') < order.index('a')

    def test_diamond_dependency(self, preprocessor):
        """
        Diamond: D depends on B and C, both depend on A.
        A must come first, D must come last.
        """
        preprocessed = make_preprocessed(
            make_service('a'),
            make_service('b', depends_on=['a']),
            make_service('c', depends_on=['a']),
            make_service('d', depends_on=['b', 'c']),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert order.index('a') < order.index('b')
        assert order.index('a') < order.index('c')
        assert order.index('b') < order.index('d')
        assert order.index('c') < order.index('d')

    def test_complex_depends_on(self, preprocessor):
        """
        Realistic multi-tier application pattern:
          postgres, redis (no deps)
          django depends on postgres, redis
          celery-worker depends on django
          celery-beat depends on django
          landing depends on django
          app depends on django
          nginx depends on django, landing, app
        """
        preprocessed = make_preprocessed(
            make_service('postgres'),
            make_service('redis'),
            make_service('django', depends_on=['postgres', 'redis']),
            make_service('celery-worker', depends_on=['django']),
            make_service('celery-beat', depends_on=['django']),
            make_service('landing', depends_on=['django']),
            make_service('app', depends_on=['django']),
            make_service('nginx', depends_on=['django', 'landing', 'app']),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert len(order) == 8
        # Infrastructure first
        assert order.index('postgres') < order.index('django')
        assert order.index('redis') < order.index('django')
        # Django before its dependents
        assert order.index('django') < order.index('celery-worker')
        assert order.index('django') < order.index('celery-beat')
        assert order.index('django') < order.index('landing')
        assert order.index('django') < order.index('app')
        # nginx after landing and app
        assert order.index('landing') < order.index('nginx')
        assert order.index('app') < order.index('nginx')

    def test_depends_on_dict_format(self, preprocessor):
        """
        depends_on as a dict with condition keys:
            depends_on:
              db:
                condition: service_healthy
        """
        preprocessed = make_preprocessed(
            make_service('db'),
            make_service('web', depends_on={'db': {'condition': 'service_healthy'}}),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert order.index('db') < order.index('web')

    def test_depends_on_list_of_dicts(self, preprocessor):
        """
        Mixed list format where some entries are strings and some are dicts:
            depends_on:
              - redis
              - {db: {condition: service_healthy}}
        """
        preprocessed = make_preprocessed(
            make_service('redis'),
            make_service('db'),
            make_service('web', depends_on=[
                'redis',
                {'db': {'condition': 'service_healthy'}},
            ]),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert order.index('redis') < order.index('web')
        assert order.index('db') < order.index('web')

    def test_cycle_detection(self, preprocessor):
        """A depends on B, B depends on A -> raises ComposeFileError."""
        preprocessed = make_preprocessed(
            make_service('a', depends_on=['b']),
            make_service('b', depends_on=['a']),
        )

        with pytest.raises(ComposeFileError, match='cycle'):
            preprocessor.get_deployment_order(preprocessed)

    def test_self_cycle(self, preprocessor):
        """A service depending on itself is a cycle."""
        preprocessed = make_preprocessed(
            make_service('loop', depends_on=['loop']),
        )

        with pytest.raises(ComposeFileError, match='cycle'):
            preprocessor.get_deployment_order(preprocessed)

    def test_missing_dependency_skipped(self, preprocessor):
        """
        A dependency referencing a non-existent service is gracefully ignored,
        allowing the dependent service to still appear in the output.
        """
        preprocessed = make_preprocessed(
            make_service('web', depends_on=['ghost-service']),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert order == ['web']

    def test_skipped_services_excluded(self, preprocessor):
        """Services with skip=True are excluded from the deployment order."""
        preprocessed = make_preprocessed(
            make_service('db'),
            make_service('debug-tool', skip=True),
            make_service('web', depends_on=['db']),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert 'debug-tool' not in order
        assert set(order) == {'db', 'web'}
        assert order.index('db') < order.index('web')

    def test_single_service(self, preprocessor):
        """A single service with no dependencies returns a list of one."""
        preprocessed = make_preprocessed(
            make_service('solo'),
        )

        order = preprocessor.get_deployment_order(preprocessed)

        assert order == ['solo']
