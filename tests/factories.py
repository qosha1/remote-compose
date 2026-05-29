"""
Factory Boy factories for test data.
"""

import factory
from factory.django import DjangoModelFactory

from remote_compose.models import (
    SecureCredential,
    DeploymentTarget,
    DockerContext,
    Deployment,
    DeploymentLog,
)
from remote_compose.utils.crypto import encrypt_value


class SecureCredentialFactory(DjangoModelFactory):
    class Meta:
        model = SecureCredential

    name = factory.Sequence(lambda n: f"credential-{n}")
    description = factory.Faker("sentence")
    credential_type = SecureCredential.CredentialType.SSH_PRIVATE_KEY
    encrypted_value = factory.LazyFunction(
        lambda: encrypt_value(
            "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        )
    )
    created_by = "test"


class AWSCredentialFactory(SecureCredentialFactory):
    credential_type = SecureCredential.CredentialType.AWS_ACCESS_KEY
    aws_access_key_id = factory.Sequence(lambda n: f"AKIA{n:016d}")
    aws_region = "us-east-1"
    encrypted_value = factory.LazyFunction(lambda: encrypt_value("fake-secret-key"))


class DeploymentTargetFactory(DjangoModelFactory):
    class Meta:
        model = DeploymentTarget

    name = factory.Sequence(lambda n: f"target-{n}")
    description = factory.Faker("sentence")
    target_type = DeploymentTarget.TargetType.SSH
    host = factory.Faker("ipv4_public")
    port = 22
    username = "ubuntu"
    environment = DeploymentTarget.Environment.DEVELOPMENT
    is_active = True
    health_status = DeploymentTarget.HealthStatus.UNKNOWN


class DockerContextFactory(DjangoModelFactory):
    class Meta:
        model = DockerContext

    name = factory.Sequence(lambda n: f"context-{n}")
    description = factory.Faker("sentence")
    target = factory.SubFactory(DeploymentTargetFactory)
    context_type = DockerContext.ContextType.SSH
    endpoint = factory.LazyAttribute(
        lambda o: f"ssh://{o.target.username}@{o.target.host}:{o.target.port}"
    )
    is_default = False
    is_synced = False


class DeploymentFactory(DjangoModelFactory):
    class Meta:
        model = Deployment

    context = factory.SubFactory(DockerContextFactory)
    target = factory.LazyAttribute(lambda o: o.context.target)
    compose_file_path = "/path/to/docker-compose.yml"
    compose_content = """version: '3.8'
services:
  web:
    image: nginx:alpine
"""
    project_name = factory.Sequence(lambda n: f"project-{n}")
    status = Deployment.Status.PENDING
    deployment_type = Deployment.DeploymentType.DEPLOY
    version = factory.Sequence(lambda n: f"v1.0.{n}")
    deployed_by = "test-user"


class DeploymentLogFactory(DjangoModelFactory):
    class Meta:
        model = DeploymentLog

    deployment = factory.SubFactory(DeploymentFactory)
    log_level = DeploymentLog.LogLevel.INFO
    message = factory.Faker("sentence")
    command = ""
    output = ""
