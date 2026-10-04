variable "name" {
  description = "Id of the replication group."
  type        = string
}

variable "vpc_id" {
  description = "Id of the VPC."
  type        = string
}

variable "subnet_ids" {
  description = "Data subnets."
  type        = list(string)
}

variable "client_security_groups" {
  description = "Security groups that may connect, as label => id. The labels must be known at plan time."
  type        = map(string)
}

variable "engine_version" {
  description = "Redis version."
  type        = string
  default     = "7.1"
}

variable "node_type" {
  description = "Node type."
  type        = string
}

variable "node_count" {
  description = "Number of nodes, the primary included."
  type        = number
  default     = 2
}

variable "kms_key_arn" {
  description = "Key that encrypts the data at rest and the secret."
  type        = string
}

variable "tags" {
  description = "Tags of every resource."
  type        = map(string)
  default     = {}
}
