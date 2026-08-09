output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.main.arn
}
output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repositories" {
  value = {
    "api" = aws_ecr_repository.api.repository_url
    "db" = aws_ecr_repository.db.repository_url
    "web" = aws_ecr_repository.web.repository_url
    "worker" = aws_ecr_repository.worker.repository_url
  }
}

# The VPC every resource below lives in. Always emitted so an out-of-band
# consumer never has to hardcode it.
output "vpc_id" {
  value = aws_vpc.main.id
}
