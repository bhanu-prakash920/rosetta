variable "name" {
  description = "Prefix of every resource name."
  type        = string
}

variable "cidr" {
  description = "Address range of the VPC."
  type        = string
}

variable "azs" {
  description = "Availability zones to use."
  type        = list(string)
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all zones instead of one per zone."
  type        = bool
  default     = false
}

variable "cluster_name" {
  description = "Name of the EKS cluster, for the subnet tags that load balancer controllers read."
  type        = string
}

variable "kms_key_arn" {
  description = "Key that encrypts the flow logs."
  type        = string
}

variable "log_retention_days" {
  description = "Days to keep the flow logs."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags of every resource."
  type        = map(string)
  default     = {}
}
