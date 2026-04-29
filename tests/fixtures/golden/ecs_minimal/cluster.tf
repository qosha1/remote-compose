resource "aws_ecs_cluster" "main" {
  name = var.cluster_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = [
    "FARGATE",
    "FARGATE_SPOT",
    aws_ecs_capacity_provider.ec2.name,
  ]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/ecs/${var.project}"
  retention_in_days = 30
}

# ECS Container Insights auto-creates this log group on first task launch
# if it doesn't exist; declaring it here lets terraform manage its
# lifecycle so `rc destroy` actually removes it (otherwise the orphan
# log group keeps reappearing every redeploy).
resource "aws_cloudwatch_log_group" "container_insights" {
  name              = "/aws/ecs/containerinsights/${var.cluster_name}/performance"
  retention_in_days = 14
}
