variable "name" {
  description = "Identifier of the database instance."
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
  description = "PostgreSQL version."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "Instance class."
  type        = string
}

variable "database_name" {
  description = "Name of the database."
  type        = string
  default     = "rosetta"
}

variable "master_username" {
  description = "Name of the master user."
  type        = string
  default     = "rosetta_admin"
}

variable "allocated_storage_gb" {
  description = "Storage at creation in GiB."
  type        = number
}

variable "max_allocated_storage_gb" {
  description = "Upper limit of storage autoscaling in GiB."
  type        = number
}

variable "multi_az" {
  description = "Keep a synchronous standby in another availability zone."
  type        = bool
  default     = true
}

variable "backup_retention_days" {
  description = "Days to keep automated backups."
  type        = number
  default     = 14
}

variable "deletion_protection" {
  description = "Refuse deletion and keep a final snapshot."
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = "Key that encrypts storage, backups, Performance Insights and the master secret."
  type        = string
}

variable "tags" {
  description = "Tags of every resource."
  type        = map(string)
  default     = {}
}
