"""SM-native per-service env (rc-7yo).

rc could source secrets two ways, neither fitting "wire an existing SM secret's
keys onto ONE service":
  - source=file: reads a LOCAL env_file and rc CREATES the SM secret.
  - source=aws_sm: references an existing SM arn but dumps the WHOLE blob into a
    single env var, and attaches it to EVERY service (global).

browser-mgr's prod env (~61 keys per service in browser-mgr/prod-env-<svc>) had
to be wired key-by-key, per service, by the out-of-band reconcile_task_env.py.

services.<svc>.env_from_secret lets rc.yml do it natively: for each
{arn, keys:[...]} entry, every key becomes its own task-def secrets[] entry
(valueFrom "<arn>:KEY::") on THAT service only, and the arn is added to the
task-execution role's GetSecretValue grant.

GENERAL + opt-in + strictly ADDITIVE: no env_from_secret -> byte-identical
terraform (test_golden.py). RED until the provider wires it.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.provider import DeployContext, ServiceSpec
from remote_compose.provider.ecs import ECSProvider

_ARN = (
    "arn:aws:secretsmanager:us-east-2:033937118837:secret:"
    "browser-mgr/prod-env-django-AbCdEf"
)
_KEYS = ["DATABASE_URL", "REDIS_URL", "DJANGO_SECRET_KEY"]


def _ctx(tmp_path: Path, django_env_from_secret) -> DeployContext:
    ecs_cfg = {
        "region": "us-east-2",
        "cluster": "browser-mgr-prod",
        "vpc_id": "vpc-0b6967",
        "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
        "security_group_ids": ["sg-013b"],
    }
    django = ServiceSpec(name="django", cpu=512, memory=1024, type="worker")
    if django_env_from_secret is not None:
        django.env_from_secret = django_env_from_secret
    return DeployContext(
        project="browser-mgr",
        compose_path=tmp_path / "docker-compose.yml",
        rc_yml_v2={},
        provider_config={"ecs": ecs_cfg},
        tf_backend_config={"type": "local"},
        working_dir=tmp_path,
        services={
            "django": django,
            # second service WITHOUT env_from_secret — proves per-service scoping
            "worker": ServiceSpec(name="worker", cpu=256, memory=512, type="worker"),
        },
        secrets=[],
    )


def _emit(tmp_path: Path, django_env_from_secret):
    out = tmp_path / "tf"
    ECSProvider().emit_terraform(_ctx(tmp_path, django_env_from_secret), out)
    return out


class TestEnvFromSecretEmission:
    def test_each_key_wired_as_valuefrom(self, tmp_path):
        services = (
            _emit(tmp_path, [{"arn": _ARN, "keys": _KEYS}]) / "services.tf"
        ).read_text()
        for key in _KEYS:
            assert f'valueFrom = "{_ARN}:{key}::"' in services

    def test_scoped_to_declaring_service_only(self, tmp_path):
        services = (
            _emit(tmp_path, [{"arn": _ARN, "keys": _KEYS}]) / "services.tf"
        ).read_text()
        # Each key appears exactly once — on django, NOT broadcast to worker.
        for key in _KEYS:
            assert services.count(f'"{_ARN}:{key}::"') == 1

    def test_exec_role_granted_getsecretvalue_on_arn(self, tmp_path):
        iam = (_emit(tmp_path, [{"arn": _ARN, "keys": _KEYS}]) / "iam.tf").read_text()
        assert "secretsmanager:GetSecretValue" in iam
        assert _ARN in iam


class TestEnvFromSecretDefaultPath:
    def test_no_env_from_secret_emits_nothing(self, tmp_path):
        out = _emit(tmp_path, None)
        services = (out / "services.tf").read_text()
        assert _ARN not in services
