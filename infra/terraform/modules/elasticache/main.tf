# Redis on ElastiCache: in the data subnets, encrypted at rest with the
# customer-managed key, TLS in transit, AUTH token required, automatic failover.
#
# The AUTH token is generated here, which means it is part of the Terraform
# state. Keep the state encrypted and access to it restricted (backend.tf).

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids

  tags = var.tags
}

resource "aws_security_group" "this" {
  name        = "${var.name}-redis"
  description = "Redis of ${var.name}"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name}-redis" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "clients" {
  for_each = var.client_security_groups

  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Redis from ${each.key}"

  tags = var.tags
}

resource "aws_elasticache_parameter_group" "this" {
  name        = var.name
  family      = "redis${split(".", var.engine_version)[0]}"
  description = "Rosetta: never evict"

  # The hot state of the fleet is one key. Redis must refuse writes when it is
  # full rather than evict that key.
  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }

  tags = var.tags
}

resource "random_password" "auth" {
  length = 48
  # letters and digits only: the token is placed inside a URL
  special = false
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = var.name
  description          = "Rosetta hot state and de-duplication store"

  engine               = "redis"
  engine_version       = var.engine_version
  node_type            = var.node_type
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.this.name

  num_cache_clusters         = var.node_count
  automatic_failover_enabled = true
  multi_az_enabled           = true

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.this.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  auth_token                 = random_password.auth.result

  snapshot_retention_limit   = 7
  snapshot_window            = "01:00-02:00"
  maintenance_window         = "sun:02:30-sun:03:30"
  auto_minor_version_upgrade = true
  apply_immediately          = false

  tags = var.tags
}

# The complete URL, ready to become ROSETTA_REDIS_URL in the Kubernetes Secret
# (External Secrets Operator or the Secrets Store CSI driver reads it from here).
resource "aws_secretsmanager_secret" "url" {
  name_prefix = "${var.name}/redis-url-"
  description = "ROSETTA_REDIS_URL for ${var.name}"
  kms_key_id  = var.kms_key_arn

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "url" {
  secret_id     = aws_secretsmanager_secret.url.id
  secret_string = "rediss://:${random_password.auth.result}@${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
}
