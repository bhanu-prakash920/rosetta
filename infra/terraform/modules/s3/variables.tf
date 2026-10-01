variable "bucket_name" {
  description = "Name of the bucket. Globally unique."
  type        = string
}

variable "kms_key_arn" {
  description = "Key that encrypts the objects."
  type        = string
}

variable "expiration_days" {
  description = "Delete objects after this many days. 0 keeps them for ever."
  type        = number
  default     = 730
}

variable "force_destroy" {
  description = "Allow Terraform to delete a bucket that still holds objects."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags of every resource."
  type        = map(string)
  default     = {}
}
