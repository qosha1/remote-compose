output "deploy_role_arn" {
  description = "ARN of the GitHub OIDC deploy role; set this as the CI role-to-assume."
  value       = aws_iam_role.deploy.arn
}

output "deploy_role_name" {
  value = aws_iam_role.deploy.name
}
