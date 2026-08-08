terraform {
  required_version = ">= 1.4"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

variable "region" {
  description = "AWS region — passed via tfvars per call."
  type        = string
}

provider "aws" {
  region = var.region
  default_tags {
    tags = var.tags
  }
}

# Default VPC + first public subnet are used when subnet_id is empty.
# Avoids forcing the user to manage VPC for `rc dev` v1.
data "aws_vpc" "default" {
  count   = var.subnet_id == "" ? 1 : 0
  default = true
}

data "aws_subnets" "default_public" {
  count = var.subnet_id == "" ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

locals {
  resolved_subnet_id = var.subnet_id != "" ? var.subnet_id : data.aws_subnets.default_public[0].ids[0]
}

resource "aws_key_pair" "dev_host" {
  key_name   = "rc-dev-${var.name}"
  public_key = var.ssh_public_key
  tags       = var.tags
}

resource "aws_security_group" "dev_host" {
  name        = "rc-dev-${var.name}"
  description = "Security group for rc dev host ${var.name}"

  # SSH always open (per Phase 1.1: dev URLs public, source IP not enforced in v1)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = var.security_group_ports
    content {
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = var.security_group_cidrs
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

resource "aws_instance" "dev_host" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.resolved_subnet_id
  associate_public_ip_address = true
  key_name                    = aws_key_pair.dev_host.key_name
  vpc_security_group_ids      = [aws_security_group.dev_host.id]
  user_data_base64            = var.user_data_base64
  user_data_replace_on_change = false

  # spot_instance_type=persistent + interruption_behavior=stop (NOT the
  # default one-time/terminate) so a Spot reclamation stops the instance —
  # same as a deliberate `rc dev stop` — instead of destroying it. Without
  # this, `rc dev start` would have nothing to start: a terminated instance
  # is gone, EBS and all.
  dynamic "instance_market_options" {
    for_each = var.spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        spot_instance_type             = "persistent"
        instance_interruption_behavior = "stop"
      }
    }
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.ebs_size_gb
    encrypted   = true
    tags        = var.tags
  }

  tags = merge(var.tags, { Name = var.name })
}

resource "aws_eip" "dev_host" {
  instance = aws_instance.dev_host.id
  domain   = "vpc"
  tags     = merge(var.tags, { Name = "rc-dev-${var.name}-eip" })
}
