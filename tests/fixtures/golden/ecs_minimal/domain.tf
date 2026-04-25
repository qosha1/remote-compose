
# ------------------------------------------------------------------
# Custom domain(s), ACM certificate (DNS-validated), Route 53 A-records
#
# Single-domain stacks: domain_name + one A-record. Multi-domain stacks:
# primary domain on cert subject, alt domains on cert SANs, one A-record
# per name. Single ACM cert covers all hostnames; ALB host-header
# listener rules in alb.tf route per service.
# ------------------------------------------------------------------

data "aws_route53_zone" "main" {
  name         = "example.com"
  private_zone = false
}
resource "aws_acm_certificate" "main" {
  domain_name       = "api.example.com"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = data.aws_route53_zone.main.zone_id
}

resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# A-record per hostname pointing at the shared ALB.
resource "aws_route53_record" "app_1" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = "api.example.com"
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
