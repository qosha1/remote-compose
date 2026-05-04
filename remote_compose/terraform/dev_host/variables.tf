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

variable "user_data" {
  description = "Cloud-init #cloud-config blob rendered by SourceSpec.render_user_data()."
  type        = string
}

variable "ebs_size_gb" {
  description = "Root EBS volume size in GiB (gp3)."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to every resource. Must include DevHost and ManagedBy."
  type        = map(string)
  default     = {}
}
