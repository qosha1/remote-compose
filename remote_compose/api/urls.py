"""
URL configuration for remote_compose REST API.

Provides a router-based URL configuration for all API endpoints.
Include this in your project's urls.py with:

    path('api/v1/', include('remote_compose.api.urls')),
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .viewsets import (
    SecureCredentialViewSet,
    DeploymentTargetViewSet,
    DockerContextViewSet,
    ECSClusterViewSet,
    ECSTaskDefinitionViewSet,
    ECSServiceViewSet,
    ECRRepositoryViewSet,
    EFSFileSystemViewSet,
    DeploymentViewSet,
    BuildRecordViewSet,
    DeploymentEventViewSet,
    ResourceMetricViewSet,
    AuditLogViewSet,
)


# Create router and register viewsets
router = DefaultRouter()

# Core resources
router.register(r'credentials', SecureCredentialViewSet, basename='credential')
router.register(r'targets', DeploymentTargetViewSet, basename='target')
router.register(r'contexts', DockerContextViewSet, basename='context')
router.register(r'deployments', DeploymentViewSet, basename='deployment')

# ECS resources
router.register(r'ecs/clusters', ECSClusterViewSet, basename='ecs-cluster')
router.register(r'ecs/task-definitions', ECSTaskDefinitionViewSet, basename='ecs-task-definition')
router.register(r'ecs/services', ECSServiceViewSet, basename='ecs-service')

# ECR resources
router.register(r'ecr/repositories', ECRRepositoryViewSet, basename='ecr-repository')

# EFS resources
router.register(r'efs/file-systems', EFSFileSystemViewSet, basename='efs-file-system')

# Tracking and monitoring
router.register(r'build-records', BuildRecordViewSet, basename='build-record')
router.register(r'deployment-events', DeploymentEventViewSet, basename='deployment-event')
router.register(r'metrics', ResourceMetricViewSet, basename='metric')

# Audit
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')


app_name = 'remote_compose_api'

urlpatterns = [
    path('', include(router.urls)),
]
