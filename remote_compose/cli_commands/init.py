"""rc init — generate an rc.yml template.

With --from-compose, scaffold from an existing docker-compose.yml. The
v1 template is preserved for legacy projects (rc.yml schema before v2
landed). New projects default to v2.
"""

from __future__ import annotations

from pathlib import Path

import click


_RC_CONFIG_FILE = 'rc.yml'

RC_TEMPLATE_V2 = """\
# rc.yml — Remote Compose configuration (v2 schema)

version: 2
project: my-project
compose_file: docker-compose.yml

provider: ecs

provider_config:
  ecs:
    region: us-west-2
    cluster: my-project-cluster
    # aws_profile: default
    vpc_cidr: 10.42.0.0/16
    default_launch_type: FARGATE

terraform:
  output_dir: ./terraform/${provider}
  backend:
    type: local
    # For shared state across machines, use s3:
    # type: s3
    # bucket: my-project-tf-state
    # key: ecs.tfstate
    # region: us-west-2

# domain: api.example.com               # custom domain — auto-provisions ACM cert + HTTPS
# certificate_arn: arn:aws:acm:...      # or supply an existing cert

# Compose-driven deploy set. Every compose service deploys with defaults
# unless overridden under `services:` below or filtered here.
# compose:
#   exclude:
#     - dev-only-sidecar

services:
  web:
    cpu: 512
    memory: 1024
    type: application
    public: true
    port: 80
    health_check_path: /health/
    default_target: true
  # worker:
  #   cpu: 1024
  #   memory: 2048
  #   type: worker
  # postgres:
  #   cpu: 512
  #   memory: 1024
  #   type: infrastructure

# Env files become Secrets Manager JSON blobs; the task def gets one
# secrets[] entry per KEY using arn:KEY:: selectors.
# secrets:
#   - name: app
#     source: file
#     path: .envs/.production/.app

# Database backup — rc db backup / rc db restore / rc db list
# backup:
#   bucket: my-project-db-dumps
#   service: postgres
#   retention_days: 14
"""


RC_TEMPLATE_V1 = """\
# rc.yml — Remote Compose configuration (legacy v1 schema)
# For new projects prefer the v2 schema (omit --v1).

cluster: my-cluster
region: us-west-2
compose_file: docker-compose.production.yml
project_name: my-project

vpc_cidr: 10.0.0.0/16
# domain: api.example.com  # custom domain — auto-provisions ACM cert + HTTPS
# certificate_arn: arn:aws:acm:us-east-1:XXXX:certificate/XXXX  # or use existing cert

# Env files to push as Secrets Manager secrets
# secrets:
#   - .envs/.production/.django
#   - .envs/.production/.postgres

services:
  web:
    cpu: 512
    memory: 1024
    type: application
    health_check_path: /health/
  # worker:
  #   cpu: 1024
  #   memory: 2048
  #   type: worker
  # nginx:
  #   cpu: 256
  #   memory: 512
  #   type: proxy
  #   public: true
  #   port: 80
  #   health_check_path: /health
  #   default_target: true

# Database backup — rc db backup / rc db restore / rc db list
# backup:
#   bucket: my-project-db-dumps  # S3 bucket for backups
#   service: postgres            # service to exec into (needs pg_dump + aws CLI)
#   retention: 30                # keep last N backups
"""


@click.command(name='init')
@click.option('--v1', 'use_v1', is_flag=True,
              help='Emit the legacy v1 schema (top-level cluster/region/compose_file). Default is v2.')
@click.option('--from-compose', 'from_compose', type=click.Path(exists=True, dir_okay=False),
              default=None,
              help='Read a docker-compose.yml and scaffold a v2 rc.yml from it.')
@click.option('-o', '--output', 'output_path', type=click.Path(dir_okay=False),
              default=None,
              help=f'Write to this path instead of ./{_RC_CONFIG_FILE}.')
@click.option('--public-service', 'public_service', default=None,
              help='Override the auto-detected ALB-fronted service (used with --from-compose).')
@click.option('--region', default='us-west-2',
              help='AWS region in the generated rc.yml (used with --from-compose).')
@click.option('--aws-profile', 'aws_profile', default=None,
              help='aws_profile in the generated rc.yml (used with --from-compose).')
@click.option('--testing-defaults/--no-testing-defaults', 'testing_defaults',
              default=None,
              help='Inject DJANGO_ALLOWED_HOSTS=* / CSRF_TRUSTED_ORIGINS=* / '
                   'DJANGO_DEBUG=False on Django services (used with '
                   '--from-compose). Default: auto-enabled when project '
                   'starts with rc-test-, off otherwise. UNSAFE for '
                   'production stacks. See rc-e5u.46.4.')
def init_cmd(use_v1, from_compose, output_path, public_service, region, aws_profile,
             testing_defaults):
    """Generate an rc.yml template in the current directory.

    With --from-compose, read an existing docker-compose.yml and scaffold
    a v2 rc.yml with per-service entries inferred from images / commands /
    ports. Edit the result before deploying.
    """
    target = Path(output_path) if output_path else Path.cwd() / _RC_CONFIG_FILE
    if target.exists():
        click.echo(f"{target} already exists")
        if not click.confirm("Overwrite?", default=False):
            return

    if from_compose:
        if use_v1:
            raise click.UsageError("--from-compose only generates v2 schema; drop --v1")
        from remote_compose.init_from_compose import generate_v2_rc_yml
        try:
            text = generate_v2_rc_yml(
                Path(from_compose),
                output_path=target,
                public_service=public_service,
                region=region,
                aws_profile=aws_profile,
                testing_defaults=testing_defaults,
            )
        except Exception as exc:
            raise click.ClickException(f"failed to scaffold from {from_compose}: {exc}")
        target.write_text(text)
        click.echo(f"Created {target} from {from_compose}")
        click.echo("Review the generated file, then run `rc plan`.")
        return

    template = RC_TEMPLATE_V1 if use_v1 else RC_TEMPLATE_V2
    target.write_text(template)
    click.echo(f"Created {target}")
    if use_v1:
        click.echo("Legacy v1 schema. Edit cluster/region/services and run `rc deploy`.")
    else:
        click.echo("v2 schema. Edit project/region/services and run `rc plan`.")
