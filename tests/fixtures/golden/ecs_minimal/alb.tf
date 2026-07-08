
resource "aws_lb" "main" {
  # AWS caps ALB names at 32 chars. "${project}-alb" overflows for long projects
  # (e.g. foundry-tenant-marketing-agents-alb = 35). Keep the readable name when it
  # fits (so existing ALBs never churn) and fall back to a deterministic
  # truncate+md5 name only when it would exceed the limit.
  name               = length("${var.project}-alb") <= 32 ? "${var.project}-alb" : "${substr(var.project, 0, 19)}-${substr(md5(var.project), 0, 8)}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}
resource "aws_lb_target_group" "default" {
  # rc-0zx: name_prefix + create_before_destroy so adding a service.domain
  # to an existing stack doesn't hit "Target group is currently in use by
  # a listener" during the listener-default-action move. AWS auto-suffixes
  # name_prefix with a unique 6-char id so two TGs can briefly coexist
  # while the listener cuts over.
  name_prefix = substr("${var.project}-", 0, 6)
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/health"
    matcher             = "200-499"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
  }

  lifecycle {
    create_before_destroy = true
  }
}

# When the default_target service has its own domain, its per-service TG
# acts as the default. Otherwise we emit a dedicated default TG above.
locals {
  default_target_group_arn = aws_lb_target_group.default.arn
}
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = local.default_target_group_arn
  }
}
