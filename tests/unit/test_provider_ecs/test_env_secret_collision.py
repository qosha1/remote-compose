"""rc-z30: when rc.yml services.<svc>.env declares a key that's also
sourced from SM (env_file_auto / source: file), the per-service task
def must put that key ONLY in environment[], not in secrets[].

ECS RegisterTaskDefinition rejects task defs with the same name in
both 'environment' and 'secrets':
  "The secret name must be unique and not shared with any new or
   existing environment variables set on the container, such as <KEY>"

The plaintext env wins on collision because the user set it
explicitly in rc.yml.
"""

from __future__ import annotations


from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.base import SecretRef
from remote_compose.provider.ecs import ECSProvider


def _ctx(tmp_path, services, secrets):
    # Write a minimal env_file matching the secrets so the file-source path
    # actually has keys to enumerate.
    env_file = tmp_path / "secrets.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=from-env-file\n"
        "DJANGO_ALLOWED_HOSTS=from-env-file\n"
        "OTHER_KEY=from-env-file\n"
    )
    return DeployContext(
        project="rc-test-z30",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={
            "ecs": {
                "region": "us-west-2",
                "cluster": "rc-test-z30",
                "vpc_cidr": "10.0.0.0/16",
            }
        },
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services=services,
        secrets=secrets,
    )


def test_env_override_drops_collision_from_per_service_secrets(tmp_path):
    # Service 'django' has rc.yml env override for DJANGO_ALLOWED_HOSTS.
    # The same key is also in the source=file secret.
    # Service 'celery' has NO override — gets the full secrets list.
    services = {
        "django": ServiceSpec(
            name="django",
            cpu=512,
            memory=1024,
            type="application",
            env={"DJANGO_ALLOWED_HOSTS": "api.example.com,localhost"},
        ),
        "celery": ServiceSpec(
            name="celery",
            cpu=512,
            memory=1024,
            type="worker",
        ),
    }
    secrets = [
        SecretRef(name="env", source="file", path=str(tmp_path / "secrets.env")),
    ]
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, secrets), out)
    services_tf = (out / "services.tf").read_text()

    # The env override key appears as a plaintext environment entry on django.
    # Tolerate the template's quote-escaping of values.
    assert 'name = "DJANGO_ALLOWED_HOSTS"' in services_tf

    # Find the django task-def block and verify its secrets[] does NOT
    # include DJANGO_ALLOWED_HOSTS — but DOES still include the others.
    django_td_idx = services_tf.find('aws_ecs_task_definition" "django"')
    assert django_td_idx >= 0, (
        "django task def not emitted. Got:\n" + services_tf[:1500]
    )
    # Take the django task def slice up to the next aws_ecs_task_definition.
    next_td = services_tf.find("aws_ecs_task_definition", django_td_idx + 50)
    django_block = services_tf[django_td_idx : next_td if next_td > 0 else None]

    # Suppression: DJANGO_ALLOWED_HOSTS must NOT appear inside a
    # `name = "..."` line within a secrets[] entry. Cheapest check:
    # find the secrets[] block and assert it doesn't list the suppressed key.
    secrets_start = django_block.find("secrets = [")
    assert secrets_start > 0, (
        "expected secrets[] block in django task def; got:\n" + django_block
    )
    secrets_end = django_block.find("]", secrets_start)
    secrets_block = django_block[secrets_start:secrets_end]
    assert "DJANGO_ALLOWED_HOSTS" not in secrets_block, (
        "DJANGO_ALLOWED_HOSTS should be suppressed from django's secrets[] "
        "(rc.yml env override wins). Found:\n" + secrets_block
    )
    # Other keys still in the secrets block (sanity check).
    assert "POSTGRES_PASSWORD" in secrets_block
    assert "OTHER_KEY" in secrets_block

    # Celery task def: NO override → full secrets list (collision-free).
    celery_td_idx = services_tf.find('aws_ecs_task_definition" "celery"')
    next_td = services_tf.find("aws_ecs_task_definition", celery_td_idx + 50)
    celery_block = services_tf[celery_td_idx : next_td if next_td > 0 else None]
    celery_secrets_start = celery_block.find("secrets = [")
    celery_secrets_block = celery_block[
        celery_secrets_start : celery_block.find("]", celery_secrets_start)
    ]
    assert "DJANGO_ALLOWED_HOSTS" in celery_secrets_block
    assert "POSTGRES_PASSWORD" in celery_secrets_block


def test_no_override_means_full_secrets_list_emitted(tmp_path):
    # No env overrides anywhere → both services get all 3 keys in secrets[].
    services = {
        "django": ServiceSpec(name="django", cpu=512, memory=1024, type="application"),
    }
    secrets = [
        SecretRef(name="env", source="file", path=str(tmp_path / "secrets.env")),
    ]
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, services, secrets), out)
    services_tf = (out / "services.tf").read_text()
    assert services_tf.count('name      = "DJANGO_ALLOWED_HOSTS"') == 1
    assert services_tf.count('name      = "POSTGRES_PASSWORD"') == 1
    assert services_tf.count('name      = "OTHER_KEY"') == 1
