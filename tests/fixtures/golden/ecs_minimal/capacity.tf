
# ------------------------------------------------------------------
# EC2 capacity for EC2-launch services
# ------------------------------------------------------------------

data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id"
}

resource "aws_iam_role" "ec2_instance" {
  name = "${var.project}-ec2-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_instance_ecs" {
  role       = aws_iam_role.ec2_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "ec2_instance_ssm" {
  role       = aws_iam_role.ec2_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2_instance" {
  name = "${var.project}-ec2-instance"
  role = aws_iam_role.ec2_instance.name
}

resource "aws_security_group" "ec2_instances" {
  name        = "${var.project}-ec2-instances"
  description = "ECS EC2 instances - only talk to ALB and within VPC."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 0
    to_port         = 0
    protocol        = "-1"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_launch_template" "ec2" {
  name_prefix   = "${var.project}-ec2-"
  image_id      = data.aws_ssm_parameter.ecs_ami.value
  instance_type = "t3.medium"

  iam_instance_profile {
    name = aws_iam_instance_profile.ec2_instance.name
  }

  # IMDS hardening. http_tokens = "required" makes the instance metadata
  # service IMDSv2-only: a token has to be minted with a PUT before anything
  # can be read, which is what a forged GET from inside a container cannot do.
  # Without it, one SSRF bug in any container on this instance yields the
  # INSTANCE role (ECS + SSM managed policies above), not just the task role.
  #
  # http_put_response_hop_limit is the IP TTL of that token response, and every
  # container network hop decrements it. 1 admits only the instance's own
  # network namespace and silently cuts off every bridge-mode container, so rc
  # defaults to 2 (`ec2_capacity.metadata_hop_limit: 1` if you have confirmed
  # nothing on the instance needs the extra hop). Note that 1 is NOT a reliable
  # cut-off for rc's own tasks: they run awsvpc and reach IMDS over their own
  # ENI. Use `ec2_capacity.block_task_imds: true` for that; it sets the ECS
  # agent's ECS_AWSVPC_BLOCK_IMDS below.
  #
  # http_endpoint is not configurable: disabling IMDS entirely stops the ECS
  # agent from registering the instance with the cluster, so the "hardened"
  # stack would simply never run a task.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  # Placement mirrors Fargate task ENIs (services.tf.j2's network_configuration
  # block): public subnets + a locked-down SG by default, so instances can
  # reach the ECS control plane / ECR / CloudWatch without a NAT gateway
  # (~$0.045/hr saved). aws_security_group.ec2_instances only allows inbound
  # from the ALB SG + self, so a public IP here is an egress path, not an
  # exposure. Follows provider_config.ecs.default_subnet_placement (rc-0cv);
  # set it to "private" for the NAT/no-public-IP variant.
  #
  # associate_public_ip_address only takes effect inside a network_interfaces
  # block, which is why the security group -- vpc_security_group_ids at the
  # launch-template top level -- has to move down here instead: the AWS
  # provider rejects a launch template that sets both.
  #
  # device_index and delete_on_termination are both schema-optional (the AWS
  # provider accepts this block without them) -- but whether the ASG's
  # RunInstances call behaves the same with them unset is a runtime API
  # question `terraform validate` cannot confirm either way (offline, no AWS
  # calls). Set both explicitly to remove the ambiguity rather than lean on
  # unverified defaulting: device_index = 0 marks this the primary (and only)
  # ENI, and delete_on_termination = true keeps a terminated/scaled-in
  # instance from leaking an orphaned ENI that rc's reap path would
  # otherwise miss.
  network_interfaces {
    associate_public_ip_address = true
    device_index                = 0
    delete_on_termination       = true
    security_groups             = [aws_security_group.ec2_instances.id]
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.main.name} >> /etc/ecs/ecs.config
    echo ECS_ENABLE_TASK_IAM_ROLE=true >> /etc/ecs/ecs.config
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags = { Name = "${var.project}-ecs-instance" }
  }
}

resource "aws_autoscaling_group" "ec2" {
  name                = "${var.project}-ec2-asg"
  vpc_zone_identifier = aws_subnet.public[*].id
  min_size            = 1
  max_size            = 2
  desired_capacity    = 1
  launch_template {
    id      = aws_launch_template.ec2.id
    version = "$Latest"
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }

  # aws_autoscaling_group is a known exception to the provider's
  # default_tags (providers.tf.j2) -- ASG tags use this block schema, not
  # the tags = {} map default_tags merges into, so nothing here is tagged
  # automatically. Every other resource in this module (VPC, subnets,
  # SGs, ...) gets Project/ManagedBy/Environment/Ephemeral/ExpiresAt for
  # free; the ASG needs them spelled out explicitly or it's invisible to
  # both scripts/reap_test_region.py (rc-e5u.25.8, confirmed: this is why
  # a real EC2 instance survived two reap passes) and any production `rc
  # reap` / out-of-band tag-scan reaper that relies on Ephemeral/ExpiresAt.
  tag {
    key                 = "Project"
    value               = var.project
    propagate_at_launch = true
  }
  tag {
    key                 = "ManagedBy"
    value               = "remote-compose"
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "ec2" {
  name = "${var.project}-ec2-cp"

  auto_scaling_group_provider {
    auto_scaling_group_arn = aws_autoscaling_group.ec2.arn
    managed_termination_protection = "DISABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 80
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 10
    }
  }
}
