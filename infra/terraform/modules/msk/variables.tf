variable "name" {
  description = "Name of the cluster."
  type        = string
}

variable "vpc_id" {
  description = "Id of the VPC."
  type        = string
}

variable "subnet_ids" {
  description = "Data subnets, one per availability zone."
  type        = list(string)
}

variable "client_security_groups" {
  description = "Security groups that may connect, as label => id. The labels must be known at plan time."
  type        = map(string)
}

variable "kafka_version" {
  description = "Apache Kafka version."
  type        = string
}

variable "instance_type" {
  description = "Instance type of a broker."
  type        = string
}

variable "broker_count" {
  description = "Number of brokers. A multiple of the number of subnets."
  type        = number
}

variable "volume_size_gb" {
  description = "Storage per broker in GiB."
  type        = number
}

variable "client_broker_encryption" {
  description = "TLS, TLS_PLAINTEXT or PLAINTEXT."
  type        = string
  default     = "TLS_PLAINTEXT"
}

variable "kms_key_arn" {
  description = "Key that encrypts the broker storage and the logs."
  type        = string
}

variable "log_retention_days" {
  description = "Days to keep the broker logs."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags of every resource."
  type        = map(string)
  default     = {}
}
