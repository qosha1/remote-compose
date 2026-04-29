
# ------------------------------------------------------------------
# Cloud Map private DNS namespace for service-to-service discovery.
# Each service registers at {service_name}.{project}.local. DHCP
# options add the namespace to the resolver's search path so compose
# hostnames ("db", "cache") resolve without the FQDN.
# ------------------------------------------------------------------

resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${var.project}.local"
  vpc  = aws_vpc.main.id
}

resource "aws_vpc_dhcp_options" "main" {
  domain_name         = "${var.project}.local"
  domain_name_servers = ["AmazonProvidedDNS"]
}

resource "aws_vpc_dhcp_options_association" "main" {
  vpc_id          = aws_vpc.main.id
  dhcp_options_id = aws_vpc_dhcp_options.main.id
}
resource "aws_service_discovery_service" "api" {
  name = "api"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}
resource "aws_service_discovery_service" "db" {
  name = "db"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}
resource "aws_service_discovery_service" "web" {
  name = "web"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}
resource "aws_service_discovery_service" "worker" {
  name = "worker"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}
