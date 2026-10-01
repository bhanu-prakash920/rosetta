# ------------------------------------------------------------------- general
variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-west-1"
}

variable "project" {
  description = "Name of the project. Prefix of every resource name."
  type        = string
  default     = "rosetta"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "Use 2 to 21 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "Name of the environment, for example staging or production."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.environment))
    error_message = "Use 2 to 16 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "tags" {
  description = "Tags added to every resource, on top of Project, Environment and ManagedBy."
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------- network
variable "vpc_cidr" {
  description = "Address range of the VPC. A /16 is split into /20 subnets."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones. MSK needs 2 or 3."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "Use 2 or 3 availability zones."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all zones instead of one per zone. Cheaper, and an outage of that zone cuts outbound traffic."
  type        = bool
  default     = false
}

# ------------------------------------------------------------------- cluster
variable "kubernetes_version" {
  description = "Kubernetes version of the EKS cluster."
  type        = string
  default     = "1.34"
}

variable "cluster_endpoint_public_access" {
  description = "Expose the Kubernetes API on the internet. When false, reach it through a VPN or a bastion inside the VPC."
  type        = bool
  default     = false
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "Who may reach the Kubernetes API when it is public. Never leave this open to the world."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.cluster_endpoint_public_access_cidrs, "0.0.0.0/0")
    error_message = "The Kubernetes API may not be open to 0.0.0.0/0."
  }
}

variable "node_instance_types" {
  description = "Instance types of the worker nodes."
  type        = list(string)
  default     = ["m6i.xlarge"]
}

variable "node_min_size" {
  description = "Smallest number of worker nodes."
  type        = number
  default     = 3
}

variable "node_max_size" {
  description = "Largest number of worker nodes."
  type        = number
  default     = 9
}

variable "node_desired_size" {
  description = "Number of worker nodes at creation. The autoscaler owns it afterwards."
  type        = number
  default     = 3
}

variable "kubernetes_namespace" {
  description = "Namespace the Helm release is installed in. Part of the trust policy of the workload role."
  type        = string
  default     = "rosetta"
}

variable "kubernetes_service_account" {
  description = "Name of the ServiceAccount of the Helm release. Part of the trust policy of the workload role."
  type        = string
  default     = "rosetta"
}

# --------------------------------------------------------------------- Kafka
variable "kafka_version" {
  description = "Apache Kafka version of the MSK cluster."
  type        = string
  default     = "3.8.x"
}

variable "kafka_instance_type" {
  description = "Instance type of the Kafka brokers."
  type        = string
  default     = "kafka.m7g.large"
}

variable "kafka_brokers_per_az" {
  description = "Brokers per availability zone."
  type        = number
  default     = 1
}

variable "kafka_volume_size_gb" {
  description = "Storage per broker in GiB."
  type        = number
  default     = 500
}

variable "kafka_client_broker_encryption" {
  description = <<-EOT
    Encryption between clients and brokers: TLS, TLS_PLAINTEXT or PLAINTEXT.
    Rosetta's Kafka client has no TLS settings today (rosetta/adapters/kafka_broker.py
    passes only bootstrap.servers), so the brokers must also offer the plain-text
    listener. Traffic stays inside the VPC and is limited by security groups.
    Switch to TLS once the application can be configured for it.
  EOT
  type        = string
  default     = "TLS_PLAINTEXT"

  validation {
    condition     = contains(["TLS", "TLS_PLAINTEXT", "PLAINTEXT"], var.kafka_client_broker_encryption)
    error_message = "Use TLS, TLS_PLAINTEXT or PLAINTEXT."
  }
}

# ---------------------------------------------------------------- PostgreSQL
variable "postgres_engine_version" {
  description = "PostgreSQL version. The major version alone lets RDS choose the minor version."
  type        = string
  default     = "16"
}

variable "postgres_instance_class" {
  description = "Instance class of the database."
  type        = string
  default     = "db.m7g.large"
}

variable "postgres_allocated_storage_gb" {
  description = "Storage at creation in GiB."
  type        = number
  default     = 100
}

variable "postgres_max_allocated_storage_gb" {
  description = "Upper limit of storage autoscaling in GiB."
  type        = number
  default     = 500
}

variable "postgres_multi_az" {
  description = "Keep a synchronous standby in another availability zone."
  type        = bool
  default     = true
}

variable "postgres_backup_retention_days" {
  description = "Days to keep automated backups."
  type        = number
  default     = 14
}

variable "deletion_protection" {
  description = "Refuse to delete the database and keep a final snapshot. Turn off only for throw-away environments."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------- Redis
variable "redis_engine_version" {
  description = "Redis version of the ElastiCache replication group."
  type        = string
  default     = "7.1"
}

variable "redis_node_type" {
  description = "Node type of the Redis replication group."
  type        = string
  default     = "cache.m7g.large"
}

variable "redis_replicas" {
  description = "Number of nodes, the primary included. 2 or more gives automatic failover."
  type        = number
  default     = 2

  validation {
    condition     = var.redis_replicas >= 2
    error_message = "Automatic failover needs at least 2 nodes."
  }
}

# -------------------------------------------------------------------- bucket
variable "archive_bucket_name" {
  description = "Name of the bucket for the Parquet archive. Empty: <project>-<environment>-telemetry-<account id>."
  type        = string
  default     = ""
}

variable "archive_expiration_days" {
  description = "Delete archived telemetry after this many days. 0 keeps it for ever."
  type        = number
  default     = 730
}

# ---------------------------------------------------------------------- logs
variable "log_retention_days" {
  description = "Days to keep CloudWatch logs (VPC flow logs, EKS control plane, Kafka brokers)."
  type        = number
  default     = 90
}
