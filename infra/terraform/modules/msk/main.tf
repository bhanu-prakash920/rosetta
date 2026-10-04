# Kafka on MSK: brokers in the data subnets, no public access, storage
# encrypted with the customer-managed key, traffic between brokers encrypted.

resource "aws_security_group" "this" {
  name        = "${var.name}-kafka"
  description = "Kafka brokers of ${var.name}"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name}-kafka" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "plaintext" {
  for_each = var.client_broker_encryption == "TLS" ? {} : var.client_security_groups

  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9092
  description                  = "Kafka plain text from ${each.key}"

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "tls" {
  for_each = var.client_broker_encryption == "PLAINTEXT" ? {} : var.client_security_groups

  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 9094
  to_port                      = 9094
  description                  = "Kafka TLS from ${each.key}"

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "brokers" {
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = aws_security_group.this.id
  ip_protocol                  = "-1"
  description                  = "Between brokers"

  tags = var.tags
}

resource "aws_vpc_security_group_egress_rule" "brokers" {
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = aws_security_group.this.id
  ip_protocol                  = "-1"
  description                  = "Between brokers"

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/msk/${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = var.tags
}

resource "aws_msk_configuration" "this" {
  name           = var.name
  kafka_versions = [var.kafka_version]

  # Rosetta creates its topics explicitly (the Helm chart has a job for it), so
  # a typo in a topic name must fail instead of creating a topic.
  server_properties = <<-EOT
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=6
    unclean.leader.election.enable=false
    log.retention.hours=48
  EOT

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.broker_count
  enhanced_monitoring    = "PER_TOPIC_PER_BROKER"

  broker_node_group_info {
    instance_type   = var.instance_type
    client_subnets  = var.subnet_ids
    security_groups = [aws_security_group.this.id]

    storage_info {
      ebs_storage_info {
        volume_size = var.volume_size_gb
      }
    }

    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn

    encryption_in_transit {
      # Accepted risk: the Kafka client has no TLS settings yet, so the plain-text
      # listener stays on (see var.kafka_client_broker_encryption). VPC-only, security groups.
      #trivy:ignore:AWS-0073
      client_broker = var.client_broker_encryption
      in_cluster    = true
    }
  }

  # Rosetta's Kafka client cannot authenticate today. Access is limited by the
  # security group rules above.
  client_authentication {
    unauthenticated = true
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.this.name
      }
    }
  }

  tags = var.tags
}
