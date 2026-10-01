variable "name" {
  description = "Name of the cluster and prefix of its resources."
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes version."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the control plane interfaces and the nodes."
  type        = list(string)
}

variable "endpoint_public_access" {
  description = "Expose the Kubernetes API on the internet."
  type        = bool
  default     = false
}

variable "endpoint_public_access_cidrs" {
  description = "Who may reach the public endpoint."
  type        = list(string)
  default     = []
}

variable "kms_key_arn" {
  description = "Key that encrypts Kubernetes secrets and the control plane logs."
  type        = string
}

variable "log_retention_days" {
  description = "Days to keep the control plane logs."
  type        = number
  default     = 90
}

variable "node_instance_types" {
  description = "Instance types of the nodes."
  type        = list(string)
}

variable "node_min_size" {
  description = "Smallest number of nodes."
  type        = number
}

variable "node_max_size" {
  description = "Largest number of nodes."
  type        = number
}

variable "node_desired_size" {
  description = "Number of nodes at creation."
  type        = number
}

variable "node_volume_size_gb" {
  description = "Root volume of a node in GiB."
  type        = number
  default     = 80
}

variable "workload_namespace" {
  description = "Namespace of the ServiceAccount that may assume the workload role."
  type        = string
}

variable "workload_service_account" {
  description = "Name of the ServiceAccount that may assume the workload role."
  type        = string
}

variable "workload_policy_arns" {
  description = "Policies of the workload role. The keys are labels and must be known at plan time."
  type        = map(string)
  default     = {}
}

variable "tags" {
  description = "Tags of every resource."
  type        = map(string)
  default     = {}
}
