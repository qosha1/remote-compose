variable "name" {
  description = "Logical name of the dev host (also the EC2 Name tag)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Must match the architecture of ami_id."
  type        = string
}

variable "ami_id" {
  description = "AL2023 AMI ID — must match instance_type architecture (arm64 vs x86_64)."
  type        = string
}

variable "subnet_id" {
  description = "Public subnet to launch the instance into. Auto-detected from default VPC if empty."
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "OpenSSH public key registered as the EC2 keypair."
  type        = string
}

variable "security_group_ports" {
  description = "Inbound TCP ports to open on the instance security group (in addition to 22)."
  type        = list(number)
  default     = []
}

variable "security_group_cidrs" {
  description = "CIDR blocks allowed to reach security_group_ports. Defaults to 0.0.0.0/0 per Phase 1.1."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "user_data_base64" {
  description = "gzip+base64 cloud-config blob (cloud-init inflates it). Compressed because EC2 caps user-data at 16 KiB."
  type        = string
}

variable "ebs_size_gb" {
  description = "Root EBS volume size in GiB (gp3)."
  type        = number
  default     = 100
}

variable "spot" {
  description = <<-EOT
    Request the instance as a persistent Spot Instance instead of on-demand
    (~50-65% cheaper for the t4g family — confirmed via the Pricing API, not
    estimated). Uses spot_instance_type=persistent + interruption_behavior=
    stop, NOT the default one-time/terminate combination, specifically so
    `rc dev stop`/`start` keeps working the same way it does for on-demand:
    a reclaimed Spot instance stops (EBS preserved) instead of terminating.
    The real tradeoff is start-time capacity, not data loss: `rc dev start`
    on a stopped Spot instance needs AWS to have spare capacity at or below
    the current spot price, which on-demand doesn't require.
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every resource. Must include DevHost and ManagedBy."
  type        = map(string)
  default     = {}
}
