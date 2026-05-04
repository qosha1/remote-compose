output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.dev_host.id
}

output "public_ip" {
  description = "Elastic IP attached to the instance."
  value       = aws_eip.dev_host.public_ip
}

output "public_dns" {
  description = "AWS-assigned public DNS hostname."
  value       = aws_instance.dev_host.public_dns
}

output "instance_arn" {
  description = "EC2 instance ARN — useful for IAM scoping."
  value       = aws_instance.dev_host.arn
}

output "security_group_id" {
  description = "ID of the security group attached to the instance."
  value       = aws_security_group.dev_host.id
}

output "key_pair_name" {
  description = "Name of the EC2 keypair registered for SSH access."
  value       = aws_key_pair.dev_host.key_name
}
