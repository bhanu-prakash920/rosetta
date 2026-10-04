output "key_arn" {
  description = "ARN of the key."
  value       = aws_kms_key.this.arn
}

output "key_id" {
  description = "Id of the key."
  value       = aws_kms_key.this.key_id
}

output "alias_name" {
  description = "Alias of the key."
  value       = aws_kms_alias.this.name
}
