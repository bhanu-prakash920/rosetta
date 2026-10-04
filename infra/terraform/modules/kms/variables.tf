variable "name" {
  description = "Name of the key alias, without the alias/ prefix."
  type        = string
}

variable "description" {
  description = "What the key is used for."
  type        = string
}

variable "deletion_window_in_days" {
  description = "Days between the request to delete the key and its deletion."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags of the key."
  type        = map(string)
  default     = {}
}
