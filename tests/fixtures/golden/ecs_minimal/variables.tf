variable "project" {
  type        = string
  description = "Project name; tags and resource prefixes derive from this."
  default     = "golden"
}

variable "region" {
  type        = string
  description = "AWS region."
  default     = "us-west-2"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR for the project VPC."
  default     = "10.0.0.0/16"
}

variable "cluster_name" {
  type        = string
  description = "ECS cluster name."
  default     = "golden-cluster"
}
