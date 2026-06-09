
resource "aws_ecr_repository" "api" {
  name                 = "${var.project}/api"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project}-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.api.repository_url}:latest"
    essential = true
    secrets = [
      {
        name      = "SECRET_KEY"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:SECRET_KEY::"
      },
      {
        name      = "DATABASE_URL"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:DATABASE_URL::"
      },
      {
        name      = "DB_PASSWORD"
        valueFrom = "arn:aws:secretsmanager:us-west-2:111122223333:secret:golden/db-AbCdEf"
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 2
  # Required for `aws ecs execute-command` (rc exec / db backup / restore).
  # Task role carries ssmmessages:* perms (see iam.tf).
  enable_execute_command = true
  # Zero-downtime rolling deploy: keep 100% of old tasks running until the
  # NEW tasks pass their health check (or reach RUNNING if no healthCheck),
  # allowing up to 200% during the roll. Pair with a container healthCheck
  # (health_check:) so "healthy" means actually-ready -- otherwise ECS drains
  # old tasks the instant new ones reach RUNNING, before a worker can accept
  # work. Circuit breaker auto-rolls-back a deploy whose new tasks never go
  # healthy, instead of leaving a degraded/empty pool.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  launch_type     = "FARGATE"

  network_configuration {
    # Fargate in public subnets with public IPs so tasks can pull images from
    # ECR without requiring a NAT gateway (~$0.045/hr saved). The tasks SG
    # only permits inbound from the ALB SG + self, so the public IP is not
    # an exposure - it is just an egress path. Private-subnet + NAT variant
    # is tracked in rc-e5u.25.
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
  service_registries {
    registry_arn = aws_service_discovery_service.api.arn
  }
}

resource "aws_ecr_repository" "db" {
  name                 = "${var.project}/db"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_task_definition" "db" {
  family                   = "${var.project}-db"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "db"
    image     = "${aws_ecr_repository.db.repository_url}:latest"
    essential = true
    mountPoints = [
      {
        sourceVolume  = "pgdata"
        containerPath = "/var/lib/postgresql/data"
        readOnly      = false
      }
    ]
    secrets = [
      {
        name      = "SECRET_KEY"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:SECRET_KEY::"
      },
      {
        name      = "DATABASE_URL"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:DATABASE_URL::"
      },
      {
        name      = "DB_PASSWORD"
        valueFrom = "arn:aws:secretsmanager:us-west-2:111122223333:secret:golden/db-AbCdEf"
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "db"
      }
    }
  }])
  volume {
    name = "pgdata"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.pgdata.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.db__pgdata.id
        iam             = "DISABLED"
      }
    }
  }
}

resource "aws_ecs_service" "db" {
  name            = "db"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.db.arn
  desired_count   = 1
  # Required for `aws ecs execute-command` (rc exec / db backup / restore).
  # Task role carries ssmmessages:* perms (see iam.tf).
  enable_execute_command = true
  # Service mounts EFS: stop the old task before starting the replacement,
  # otherwise two containers briefly share the same data directory and
  # stateful engines (postgres initdb) can wipe each other.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  # Newer ECS APIs default availability_zone_rebalancing=ENABLED, which
  # actively redistributes tasks across AZs - the OPPOSITE of what a
  # stateful EFS-mounting workload wants (it deliberately keeps a single
  # task at a time on the data dir). The combo also gets rejected at
  # deploy time: "availability_zone_rebalancing does not support
  # maximumPercent <= 100". Pin it OFF for stateful services.
  availability_zone_rebalancing = "DISABLED"
  launch_type     = "FARGATE"

  network_configuration {
    # Fargate in public subnets with public IPs so tasks can pull images from
    # ECR without requiring a NAT gateway (~$0.045/hr saved). The tasks SG
    # only permits inbound from the ALB SG + self, so the public IP is not
    # an exposure - it is just an egress path. Private-subnet + NAT variant
    # is tracked in rc-e5u.25.
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
  service_registries {
    registry_arn = aws_service_discovery_service.db.arn
  }
}

resource "aws_ecr_repository" "web" {
  name                 = "${var.project}/web"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.project}-web"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "web"
    image     = "${aws_ecr_repository.web.repository_url}:latest"
    essential = true
    portMappings = [
      { containerPort = 80, protocol = "tcp" }
    ]
    secrets = [
      {
        name      = "SECRET_KEY"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:SECRET_KEY::"
      },
      {
        name      = "DATABASE_URL"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:DATABASE_URL::"
      },
      {
        name      = "DB_PASSWORD"
        valueFrom = "arn:aws:secretsmanager:us-west-2:111122223333:secret:golden/db-AbCdEf"
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "web"
      }
    }
  }])
}

resource "aws_ecs_service" "web" {
  name            = "web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = 1
  # Required for `aws ecs execute-command` (rc exec / db backup / restore).
  # Task role carries ssmmessages:* perms (see iam.tf).
  enable_execute_command = true
  # Zero-downtime rolling deploy: keep 100% of old tasks running until the
  # NEW tasks pass their health check (or reach RUNNING if no healthCheck),
  # allowing up to 200% during the roll. Pair with a container healthCheck
  # (health_check:) so "healthy" means actually-ready -- otherwise ECS drains
  # old tasks the instant new ones reach RUNNING, before a worker can accept
  # work. Circuit breaker auto-rolls-back a deploy whose new tasks never go
  # healthy, instead of leaving a degraded/empty pool.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  launch_type     = "FARGATE"

  network_configuration {
    # Fargate in public subnets with public IPs so tasks can pull images from
    # ECR without requiring a NAT gateway (~$0.045/hr saved). The tasks SG
    # only permits inbound from the ALB SG + self, so the public IP is not
    # an exposure - it is just an egress path. Private-subnet + NAT variant
    # is tracked in rc-e5u.25.
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
  service_registries {
    registry_arn = aws_service_discovery_service.web.arn
  }
  # rc-05q: AWS default is 0s, which kills tasks before slow-booting
  # services (Django migrate + collectstatic + uvicorn) can bind. 60s
  # base; 180s when an auto_on_deploy lifecycle hook is declared.
  health_check_grace_period_seconds = 60
  load_balancer {
    target_group_arn = aws_lb_target_group.default.arn
    container_name   = "web"
    container_port   = 80
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecr_repository" "worker" {
  name                 = "${var.project}/worker"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["EC2"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.worker.repository_url}:latest"
    essential = true
    secrets = [
      {
        name      = "SECRET_KEY"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:SECRET_KEY::"
      },
      {
        name      = "DATABASE_URL"
        valueFrom = "${aws_secretsmanager_secret.django.arn}:DATABASE_URL::"
      },
      {
        name      = "DB_PASSWORD"
        valueFrom = "arn:aws:secretsmanager:us-west-2:111122223333:secret:golden/db-AbCdEf"
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  # Required for `aws ecs execute-command` (rc exec / db backup / restore).
  # Task role carries ssmmessages:* perms (see iam.tf).
  enable_execute_command = true
  # Zero-downtime rolling deploy: keep 100% of old tasks running until the
  # NEW tasks pass their health check (or reach RUNNING if no healthCheck),
  # allowing up to 200% during the roll. Pair with a container healthCheck
  # (health_check:) so "healthy" means actually-ready -- otherwise ECS drains
  # old tasks the instant new ones reach RUNNING, before a worker can accept
  # work. Circuit breaker auto-rolls-back a deploy whose new tasks never go
  # healthy, instead of leaving a degraded/empty pool.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.ec2.name
    weight            = 1
    base              = 1
  }

  network_configuration {
    # Fargate in public subnets with public IPs so tasks can pull images from
    # ECR without requiring a NAT gateway (~$0.045/hr saved). The tasks SG
    # only permits inbound from the ALB SG + self, so the public IP is not
    # an exposure - it is just an egress path. Private-subnet + NAT variant
    # is tracked in rc-e5u.25.
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
  service_registries {
    registry_arn = aws_service_discovery_service.worker.arn
  }
}

